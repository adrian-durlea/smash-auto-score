"""Optional reader for structured data embedded in public Supermajor player pages.

This is an undocumented page format, not an official Supermajor API. Any format
change yields no evidence. Requests are limited to players with a stable ID.
"""

import asyncio
import json
import logging
import re
import time
from html.parser import HTMLParser
from urllib.parse import urlencode

import httpx

from .domain import PlayerIdentity
from .identity import tag_score
from .store import Store

log = logging.getLogger(__name__)
BASE_URL = "https://www.supermajor.gg/ultimate/player/_"
USER_AGENT = "SmashAutoScore/0.1 (+https://github.com/adrian-durlea/smash-auto-score; on-demand player lookup)"
SOURCE = "supermajor_public_player_page"


class _PageScripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[str] = []
        self.title = ""
        self._script: list[str] | None = None
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self._script = []
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script is not None:
            self.scripts.append("".join(self._script))
            self._script = None
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            self._script.append(data)
        elif self._in_title:
            self.title += data


def _embedded_objects(html: str) -> tuple[str, dict, dict]:
    page = _PageScripts()
    page.feed(html)
    overview: dict = {}
    character_lookup: dict = {}
    for script in page.scripts:
        if not script.startswith("self.__next_f.push(") or not script.endswith(")"):
            continue
        try:
            frame = json.loads(script[len("self.__next_f.push("):-1])
            if len(frame) < 2 or not isinstance(frame[1], str):
                continue
            payload = frame[1]
            for key, destination in (("\"initialData\":", overview),
                                     ("\"character_info_lookup\":", character_lookup)):
                index = payload.find(key)
                if index < 0:
                    continue
                parsed, _ = json.JSONDecoder().raw_decode(payload[index + len(key):])
                if isinstance(parsed, dict):
                    if key == "\"initialData\":" and "characters" not in parsed:
                        continue
                    destination.update(parsed)
        except (ValueError, TypeError, IndexError):
            continue
    return page.title, overview, character_lookup


def _character_counts(overview: dict, lookup: dict, context: str) -> dict[str, int]:
    raw = overview.get("characters", {}).get("contexts", {}).get(context, {}).get("characters", {})
    if not isinstance(raw, dict):
        return {}
    counts: dict[str, int] = {}
    for character_id, value in raw.items():
        info = lookup.get(character_id, {})
        if not isinstance(value, dict) or not isinstance(info, dict) or info.get("sport") != "ultimate":
            continue
        name, games = info.get("name"), value.get("num_games")
        if isinstance(name, str) and isinstance(games, int) and games > 0:
            counts[name] = counts.get(name, 0) + games
    return counts


def parse_supermajor_page(html: str, source_player_id: str, min_games: int = 20) -> dict:
    """Extract reported game counts only; never invent percentages from main labels."""
    if not re.fullmatch(r"S[0-9]+", source_player_id):
        raise ValueError("Invalid Supermajor player ID")
    title, overview, lookup = _embedded_objects(html)
    if f'\\"playerId\\":\\"{source_player_id}\\"' not in html:
        raise ValueError("Player ID missing from public page")
    if not title.startswith("Supermajor - ") or not overview:
        raise ValueError("Public player data missing or changed")
    canonical_name = title.removeprefix("Supermajor - ").strip()
    if not canonical_name or canonical_name == "Supermajor":
        raise ValueError("Canonical player name missing")
    all_time = _character_counts(overview, lookup, "All Time")
    recent = _character_counts(overview, lookup, "Last 6 Mo")
    selected = recent if sum(recent.values()) >= min_games else all_time
    total = sum(selected.values())
    distribution = ({name.casefold(): count / total for name, count in selected.items()}
                    if total >= min_games else {})
    return {"status": "MATCHED", "source": SOURCE, "source_player_id": source_player_id,
            "canonical_name": canonical_name, "aliases": [],
            "character_distribution": distribution,
            "character_counts_all_time": all_time, "character_counts_recent": recent,
            "distribution_context": "Last 6 Mo" if selected is recent else "All Time",
            "sample_games": total,
            "url": f"{BASE_URL}?{urlencode({'id': source_player_id})}"}


class SupermajorPlayerDataProvider:
    def __init__(self, store: Store, *, enabled: bool = False, cache_ttl_days: int = 7,
                 timeout_seconds: float = 10, min_games: int = 20,
                 client: httpx.AsyncClient | None = None):
        self.store = store
        self.enabled = enabled
        self.ttl = cache_ttl_days * 86400
        self.timeout = timeout_seconds
        self.min_games = min_games
        self.client = client
        self._semaphore = asyncio.Semaphore(2)
        self._pending: dict[str, asyncio.Task[dict]] = {}
        self._live: dict[str, dict] = {}

    @staticmethod
    def player_id(player: PlayerIdentity) -> str | None:
        value = player.supermajor_id
        if not value:
            return None
        value = str(value).upper().removeprefix("S")
        return f"S{value}" if value.isdecimal() else None

    @staticmethod
    def _key(player_id: str) -> str:
        return f"supermajor:{player_id}"

    def status(self, player: PlayerIdentity) -> dict:
        player_id = self.player_id(player)
        if not self.enabled:
            return {"status": "DISABLED"}
        if not player_id:
            return {"status": "NOT_LOOKED_UP", "reason": "Stable player ID required"}
        entry = self.store.cache_entry(self._key(player_id))
        if entry and entry[1] > time.time():
            return {**entry[0], "cached": True, "expires_at": entry[1]}
        if player_id in self._pending:
            return {"status": "LOADING", "source_player_id": player_id}
        return self._live.get(player_id, {"status": "NOT_LOOKED_UP", "source_player_id": player_id})

    async def enrich(self, player: PlayerIdentity) -> PlayerIdentity:
        if not self.enabled:
            return player
        player_id = self.player_id(player)
        if not player_id:
            return player
        entry = self.store.cache_entry(self._key(player_id))
        if entry and entry[1] > time.time():
            self._apply(player, entry[0])
        elif player_id not in self._pending:
            self._pending[player_id] = asyncio.create_task(self._lookup(player, player_id))
            self._pending[player_id].add_done_callback(self._finished_lookup)
        return player

    def _finished_lookup(self, task: asyncio.Task[dict]) -> None:
        for key, pending in list(self._pending.items()):
            if pending is task:
                self._pending.pop(key, None)

    def _apply(self, player: PlayerIdentity, result: dict) -> None:
        if result.get("status") != "MATCHED":
            return
        player.supermajor_id = str(result["source_player_id"])
        player.aliases = sorted(set(player.aliases) | set(result.get("aliases", [])))
        for character, probability in result.get("character_distribution", {}).items():
            player.character_distribution.setdefault(character, probability)

    async def lookup(self, player: PlayerIdentity, *, force: bool = False) -> dict:
        if not self.enabled:
            return {"status": "DISABLED"}
        player_id = self.player_id(player)
        if not player_id:
            return {"status": "NOT_LOOKED_UP", "reason": "Stable player ID required"}
        entry = self.store.cache_entry(self._key(player_id))
        if not force and entry and entry[1] > time.time():
            self._apply(player, entry[0])
            return {**entry[0], "cached": True, "expires_at": entry[1]}
        if player_id in self._pending:
            result = await self._pending[player_id]
        else:
            result = await self._lookup(player, player_id)
        self._apply(player, result)
        return result

    async def _lookup(self, player: PlayerIdentity, player_id: str) -> dict:
        log.info("Supermajor lookup started: %s (%s)", player.display_name, player_id)
        result: dict
        async with self._semaphore:
            try:
                if self.client:
                    response = await self.client.get(BASE_URL, params={"id": player_id},
                                                     headers={"User-Agent": USER_AGENT}, timeout=self.timeout)
                else:
                    async with httpx.AsyncClient(follow_redirects=True) as client:
                        response = await client.get(BASE_URL, params={"id": player_id},
                                                    headers={"User-Agent": USER_AGENT}, timeout=self.timeout)
                if response.status_code == 404:
                    result = {"status": "NOT_FOUND", "source_player_id": player_id}
                else:
                    response.raise_for_status()
                    if len(response.content) > 4_000_000:
                        raise ValueError("Supermajor page exceeds size limit")
                    result = parse_supermajor_page(response.text, player_id, self.min_games)
                    if not player.supermajor_id and tag_score(result["canonical_name"], player) < .8:
                        result["status"] = "AMBIGUOUS"
                        result["reason"] = "Stable ID returned a different display name; confirm it manually"
                result["fetched_at"] = time.time()
                ttl = self.ttl if result["status"] == "MATCHED" else min(self.ttl, 6 * 3600)
                self.store.cache_put(self._key(player_id), result, ttl)
                self.store.log("supermajor_lookup", {"player": player.display_name,
                               "source_player_id": player_id, "status": result["status"]})
                log.info("Supermajor lookup %s: %s", result["status"], player_id)
                return result
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                result = {"status": "ERROR", "source_player_id": player_id,
                          "reason": str(exc), "fetched_at": time.time()}
                self._live[player_id] = result
                self.store.cache_put(self._key(player_id), result, min(self.ttl, 15 * 60))
                log.warning("Supermajor lookup failed for %s: %s", player_id, exc)
                return result
