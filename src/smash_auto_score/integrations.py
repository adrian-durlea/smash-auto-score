"""External boundaries. TSH routes reflect joaorb64/TSH 5.x source, not a stable public API."""
import asyncio
import base64
import logging
import time
from pathlib import Path
from typing import Protocol

import cv2
import httpx
import numpy as np

from .domain import PlayerIdentity, SetInfo

log = logging.getLogger(__name__)


class TSHAdapter(Protocol):
    async def get_current_set(self) -> SetInfo: ...
    async def get_possible_sets(self) -> list[SetInfo]: ...
    async def set_score(self, left: int, right: int) -> None: ...
    async def load_set(self, set_id: str) -> None: ...
    async def swap_players(self) -> None: ...

    async def get_players(self) -> tuple[PlayerIdentity, PlayerIdentity]: ...
    async def get_scores(self) -> tuple[int, int]: ...
    async def increment_left(self) -> None: ...
    async def increment_right(self) -> None: ...


def _player(raw: object) -> PlayerIdentity:
    if isinstance(raw, dict):
        return PlayerIdentity(str(raw.get("name") or raw.get("tag") or raw.get("display_name") or "?"),
                              startgg_id=str(raw["id"]) if raw.get("id") else None)
    return PlayerIdentity(str(raw or "?"))


def parse_tsh_scoreboard(raw: dict, set_id: str) -> SetInfo:
    teams = raw.get("team") or raw.get("teams") or []
    if isinstance(teams, dict):
        teams = [teams.get("1", {}), teams.get("2", {})]
    if len(teams) < 2:
        raise ValueError("TSH scoreboard has fewer than two teams")
    def player(team: dict) -> PlayerIdentity:
        players = team.get("player") or team.get("players") or []
        if isinstance(players, dict):
            players = list(players.values())
        return _player(players[0] if players else team.get("name"))
    return SetInfo(set_id, player(teams[0]), player(teams[1]),
                   int(teams[0].get("score", 0)), int(teams[1].get("score", 0)),
                   int(raw.get("best_of") or raw.get("bestOf") or 3), str(raw.get("match") or ""),
                   str(raw.get("tournament") or ""),
                   teams[0].get("color"), teams[1].get("color"))


class TSHWebAdapter:
    def __init__(self, base_url: str, scoreboard: int = 1):
        self.base = base_url.rstrip("/")
        self.number = scoreboard
        self.client = httpx.AsyncClient(base_url=self.base, timeout=4)

    async def _get(self, path: str, **params: str) -> object:
        response = await self.client.get(path, params=params)
        response.raise_for_status()
        try:
            return response.json()
        except ValueError:
            return response.text

    async def get_current_set(self) -> SetInfo:
        raw = await self._get(f"/scoreboard{self.number}-get")
        set_id = await self._get(f"/scoreboard{self.number}-get-set")
        if not isinstance(raw, dict):
            raise TypeError("Unexpected TSH scoreboard response")
        return parse_tsh_scoreboard(raw, str(set_id))

    async def get_players(self) -> tuple[PlayerIdentity, PlayerIdentity]:
        match = await self.get_current_set()
        return match.left, match.right

    async def get_scores(self) -> tuple[int, int]:
        match = await self.get_current_set()
        return match.left_score, match.right_score

    async def increment_left(self) -> None:
        match = await self.get_current_set()
        await self.set_score(match.left_score + 1, match.right_score)

    async def increment_right(self) -> None:
        match = await self.get_current_set()
        await self.set_score(match.left_score, match.right_score + 1)

    async def get_possible_sets(self) -> list[SetInfo]:
        raw = await self._get("/get-sets")
        result: list[SetInfo] = []
        if not isinstance(raw, list):
            return result
        for item in raw:
            if not isinstance(item, dict):
                continue
            players = item.get("players") or item.get("entrants") or []
            if len(players) >= 2:
                left, right = _player(players[0]), _player(players[1])
            elif item.get("p1_name") and item.get("p2_name"):
                left, right = _player(item["p1_name"]), _player(item["p2_name"])
            else:
                continue
            result.append(SetInfo(str(item.get("id")), left, right,
                                  round=str(item.get("round_name") or item.get("round") or ""),
                                  assigned_stream=item.get("stream")))
        return result

    async def set_score(self, left: int, right: int) -> None:
        current = await self.get_current_set()
        delta_left, delta_right = left - current.left_score, right - current.right_score
        if abs(delta_left) + abs(delta_right) != 1:
            raise ValueError("Only one-point TSH score changes are supported")
        team, delta = (1, delta_left) if delta_left else (2, delta_right)
        route = "scoreup" if delta > 0 else "scoredown"
        result = await self._get(f"/scoreboard{self.number}-team{team}-{route}")
        if result != "OK":
            raise RuntimeError(f"TSH rejected score change: {str(result)[:100]}")

    async def load_set(self, set_id: str) -> None:
        if await self._get(f"/scoreboard{self.number}-load-set", set=set_id) != "OK":
            raise RuntimeError("TSH rejected set load")

    async def swap_players(self) -> None:
        if await self._get(f"/scoreboard{self.number}-swap-teams") != "OK":
            raise RuntimeError("TSH rejected player swap")


class DemoTSHAdapter:
    def __init__(self):
        self.current = SetInfo("demo-1", PlayerIdentity("DjDickCheese", ["DjDC"],
                               character_distribution={"terry": .8, "kazuya": .17}),
                               PlayerIdentity("Snackz", character_distribution={"fox": .8, "wolf": .17}),
                               left_color="#e33737", right_color="#3075ef")
        self.candidates = [SetInfo("demo-2", PlayerIdentity("MkLeo"), PlayerIdentity("Dabuz"))]

    async def get_current_set(self) -> SetInfo:
        return self.current

    async def get_players(self) -> tuple[PlayerIdentity, PlayerIdentity]:
        return self.current.left, self.current.right

    async def get_scores(self) -> tuple[int, int]:
        return self.current.left_score, self.current.right_score

    async def increment_left(self) -> None:
        await self.set_score(self.current.left_score + 1, self.current.right_score)

    async def increment_right(self) -> None:
        await self.set_score(self.current.left_score, self.current.right_score + 1)

    async def get_possible_sets(self) -> list[SetInfo]:
        return self.candidates

    async def set_score(self, left: int, right: int) -> None:
        self.current.left_score, self.current.right_score = left, right

    async def load_set(self, set_id: str) -> None:
        match = next(s for s in self.candidates if s.set_id == set_id)
        self.current = match

    async def swap_players(self) -> None:
        m = self.current
        m.left, m.right = m.right, m.left
        m.left_score, m.right_score = m.right_score, m.left_score
        m.left_color, m.right_color = m.right_color, m.left_color


class VideoSource(Protocol):
    async def frame(self) -> np.ndarray: ...


class OBSVideoSource:
    def __init__(self, host: str, port: int, password: str, source: str, width: int = 960,
                 height: int = 540):
        self.host, self.port, self.password, self.source = host, port, password, source
        self.width, self.height = width, height
        self.last_roundtrip_ms = 0.0

    async def frame(self) -> np.ndarray:
        import obsws_python as obs
        def capture() -> bytes:
            client = obs.ReqClient(host=self.host, port=self.port, password=self.password, timeout=4)
            try:
                data = client.get_source_screenshot(self.source, "png", self.width, self.height, 100)
                return base64.b64decode(data.image_data.split(",", 1)[-1])
            finally:
                client.disconnect()
        started = time.perf_counter()
        buffer = await asyncio.to_thread(capture)
        self.last_roundtrip_ms = (time.perf_counter() - started) * 1000
        image = cv2.imdecode(np.frombuffer(buffer, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("OBS returned an undecodable image")
        return image


class RecordedVideoSource:
    def __init__(self, path: str):
        self.path = Path(path)
        self.capture = cv2.VideoCapture(str(self.path))
        if not self.capture.isOpened():
            raise ValueError(f"Cannot open recording {path}")

    async def frame(self) -> np.ndarray:
        ok, image = await asyncio.to_thread(self.capture.read)
        if not ok:
            raise EOFError("Recording ended")
        return image


class StartGGProvider:
    def __init__(self, token: str):
        self.token = token

    async def stream_set_ids(self, tournament_slug: str, stream_name: str) -> set[str]:
        query = "query($slug:String!){tournament(slug:$slug){streamQueue{stream{streamName}sets{id}}}}"
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.post("https://api.start.gg/gql/alpha",
                                         headers={"Authorization": f"Bearer {self.token}"},
                                         json={"query": query, "variables": {"slug": tournament_slug}})
            response.raise_for_status()
            body = response.json()
        if body.get("errors"):
            raise RuntimeError("Start.gg GraphQL returned errors")
        queue = body.get("data", {}).get("tournament", {}).get("streamQueue") or []
        return {str(item["id"]) for row in queue if row.get("stream", {}).get("streamName", "").casefold() == stream_name.casefold()
                for item in row.get("sets", [])}
