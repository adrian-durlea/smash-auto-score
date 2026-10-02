"""Read-only local integration discovery and local setup persistence."""

import asyncio
import json
import os
import re
import socket
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import Settings
from .integrations import OBSVideoSource, TSHWebAdapter
from .vision import FrameDetector
from .worker import load_calibration

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
TSH_PORTS = (5500, 5000, 5001, 8080, 8000)
OBS_PORTS = (4455, 4444)
PERSISTED = {"tsh_url", "tsh_scoreboard", "obs_host", "obs_port", "obs_source",
             "obs_password", "startgg_token", "startgg_tournament_slug",
             "startgg_stream_name", "recorded_video", "demo"}
STREAM_QUERY = """query($slug:String!){tournament(slug:$slug){id name streamQueue{
stream{streamSource streamName} sets{id slots{entrant{name}}}}}}"""


def tournament_slug(value: str) -> str:
    value = value.strip()
    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        if parsed.hostname not in {"start.gg", "www.start.gg", "smash.gg", "www.smash.gg"}:
            raise ValueError("Use a start.gg tournament URL")
        value = parsed.path.strip("/")
    value = value.split("?", 1)[0].strip("/")
    parts = value.split("/")
    if len(parts) >= 2 and parts[0] == "tournament":
        slug = parts[1]
    elif len(parts) == 1:
        slug = parts[0]
    else:
        raise ValueError("Could not read tournament slug")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{1,100}", slug):
        raise ValueError("Invalid tournament slug")
    return f"tournament/{slug}"


def save_local_settings(path: Path, values: Mapping[str, object]) -> None:
    """Replace only allowed SAS settings in ignored .env, preserving other lines."""
    if not values or not set(values).issubset(PERSISTED):
        raise ValueError("Unknown setup setting")
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    replacements = {f"SAS_{key.upper()}": json.dumps(value) if isinstance(value, str)
                    else str(value).lower() if isinstance(value, bool) else str(value)
                    for key, value in values.items()}
    kept = [line for line in existing if line.split("=", 1)[0].strip() not in replacements]
    content = "\n".join([*kept, *(f"{key}={value}" for key, value in replacements.items())]) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=".autoscore-", delete=False) as stream:
        temp = Path(stream.name)
        stream.write(content)
    try:
        if os.name != "nt":
            temp.chmod(0o600)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


async def probe_tsh(url: str, scoreboard: int) -> dict:
    started = time.perf_counter()
    adapter = TSHWebAdapter(url, scoreboard)
    adapter.client.timeout = httpx.Timeout(1.5)
    try:
        match = await adapter.get_current_set()
        valid_set = match.set_id not in {"", "0", "None"}
        valid_players = match.left.display_name != "?" and match.right.display_name != "?"
        capabilities = {"current_set": "PASS" if valid_set else "NO_SET_LOADED",
                        "players": "PASS" if valid_players else "NO_PLAYERS_LOADED",
                        "score_read": "PASS", "colors": "PASS" if match.left_color and match.right_color else "MISSING",
                        "best_of": "PASS" if valid_set else "NOT_VERIFIED", "score_write": "NOT_TESTED",
                        "player_swap": "NOT_TESTED", "set_load": "NOT_TESTED"}
        result: dict = {"status": "PASS", "url": url, "scoreboard": scoreboard,
                  "version": "unknown",
                  "set": {"id": match.set_id, "players": [match.left.display_name,
                          match.right.display_name], "score": [match.left_score, match.right_score],
                          "colors": [match.left_color, match.right_color], "best_of": match.best_of,
                          "event": match.event},
                  "capabilities": capabilities}
        if match.event:
            try:
                result["tournament_suggestion"] = tournament_slug(match.event)
            except ValueError:
                pass
        try:
            candidates = await adapter.get_possible_sets()
            result["candidates"] = [{"id": item.set_id, "players": [item.left.display_name,
                                     item.right.display_name]} for item in candidates]
            result["capabilities"]["candidate_sets"] = "PASS"
        except Exception as exc:  # noqa: BLE001 - capability can differ by version
            result["candidates"] = []
            result["capabilities"]["candidate_sets"] = "FAIL"
            result["candidate_error"] = str(exc)[:160] or type(exc).__name__
        result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return result
    except Exception as exc:  # noqa: BLE001 - malformed or absent TSH
        return {"status": "FAIL", "url": url, "error": str(exc)[:160]}
    finally:
        await adapter.client.aclose()


async def discover_tsh(settings: Settings) -> list[dict]:
    parsed = urlparse(settings.tsh_url)
    urls = [settings.tsh_url] if parsed.hostname in LOCAL_HOSTS else []
    urls.extend(f"http://127.0.0.1:{port}" for port in TSH_PORTS)
    urls = list(dict.fromkeys(urls))
    results = await asyncio.gather(*(probe_tsh(url, settings.tsh_scoreboard) for url in urls))
    return [result for result in results if result["status"] == "PASS"]


def _obs_probe(host: str, port: int, password: str) -> dict:
    import obsws_python as obs

    started = time.perf_counter()
    client = None
    try:
        with socket.create_connection((host, port), timeout=.4):
            pass
        client = obs.ReqClient(host=host, port=port, password=password, timeout=2)
        version = client.get_version()
        scenes = client.get_scene_list().scenes
        inputs = client.get_input_list().inputs
        sources = []
        for item in inputs:
            name = str(item.get("inputName", ""))
            kind = str(item.get("inputKind", ""))
            hint = (name + " " + kind).casefold()
            likely = any(word in hint for word in ("capture", "elgato", "avermedia",
                                                   "switch", "hd60", "game"))
            sources.append({"name": name, "kind": kind, "likely_gameplay": likely})
        sources.sort(key=lambda row: not row["likely_gameplay"])
        return {"status": "PASS", "host": host, "port": port,
                "version": getattr(version, "obs_version", "unknown"),
                "websocket_version": getattr(version, "obs_web_socket_version", "unknown"),
                "scenes": [str(item.get("sceneName", "")) for item in scenes],
                "sources": sources, "recommended_source": sources[0]["name"] if sources and
                sources[0]["likely_gameplay"] else None,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
    except Exception as exc:  # noqa: BLE001 - SDK errors vary by OBS version
        reason = str(exc).lower()
        return {"status": "AUTH_REQUIRED" if "auth" in reason or "password" in reason else "FAIL",
                "host": host, "port": port, "error": str(exc)[:160]}
    finally:
        if client is not None:
            client.disconnect()


async def discover_obs(settings: Settings) -> list[dict]:
    if settings.obs_host not in LOCAL_HOSTS:
        hosts = ["127.0.0.1"]
    else:
        hosts = [settings.obs_host, "127.0.0.1"]
    pairs = list(dict.fromkeys([(hosts[0], settings.obs_port),
                                *(("127.0.0.1", port) for port in OBS_PORTS)]))
    results = await asyncio.gather(*(asyncio.to_thread(_obs_probe, host, port,
                                settings.obs_password) for host, port in pairs))
    return [result for result in results if result["status"] != "FAIL"]


async def test_obs_frame(host: str, port: int, password: str, source: str,
                         width: int = 960, height: int = 540,
                         calibration_path: Path = Path("config/calibration.json")) -> tuple[dict, bytes | None]:
    import cv2

    frames = []
    source_adapter = OBSVideoSource(host, port, password, source, width, height)
    try:
        for _ in range(3):
            image = await source_adapter.frame()
            frames.append((image, source_adapter.last_roundtrip_ms))
        image = frames[-1][0]
        detector = FrameDetector(load_calibration(calibration_path))
        observation = await asyncio.to_thread(detector.observe, image, do_ocr=False,
                                               do_color=True, do_character=False)
        ok, encoded = cv2.imencode(".jpg", image)
        return ({"status": "PASS", "source": source, "width": image.shape[1],
                 "height": image.shape[0], "frames": len(frames),
                 "average_latency_ms": round(sum(item[1] for item in frames) / len(frames), 1),
                 "analysis": "PASS", "analysis_ms": round(detector.last_timings["total_ms"], 2),
                 "gameplay_observed": observation.game_active,
                 "result_observed": observation.result_screen}, encoded.tobytes() if ok else None)
    except Exception as exc:  # noqa: BLE001 - source/connection errors vary
        return {"status": "FAIL", "source": source, "error": str(exc)[:160]}, None


async def probe_startgg(token: str, tournament: str = "", stream: str = "") -> dict:
    if not token:
        return {"status": "NOT_CONFIGURED", "authentication": "NOT_TESTED"}
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=8) as client:
        try:
            response = await client.post("https://api.start.gg/gql/alpha", headers=headers,
                                         json={"query": "query{currentUser{id}}"})
            if response.status_code in (401, 403):
                return {"status": "FAIL", "authentication": "FAIL", "error": "Token rejected"}
            response.raise_for_status()
            body = response.json()
            if body.get("errors") or not body.get("data", {}).get("currentUser"):
                return {"status": "FAIL", "authentication": "FAIL",
                        "error": "Token did not authenticate current user"}
            result: dict = {"status": "PASS", "authentication": "PASS", "tournament": "NOT_CONFIGURED",
                      "stream": "NOT_CONFIGURED", "streams": []}
            if tournament:
                slug = tournament_slug(tournament)
                response = await client.post("https://api.start.gg/gql/alpha", headers=headers,
                                             json={"query": STREAM_QUERY, "variables": {"slug": slug}})
                response.raise_for_status()
                body = response.json()
                if body.get("errors"):
                    raise ValueError("Tournament query failed")
                event = body.get("data", {}).get("tournament")
                if not event:
                    result.update(status="FAIL", tournament="FAIL", error="Tournament not found")
                    return result
                result["tournament"] = "PASS"
                result["tournament_name"] = event.get("name")
                for row in event.get("streamQueue") or []:
                    info = row.get("stream") or {}
                    name = info.get("streamName")
                    if not name:
                        continue
                    sets = [{"id": str(item.get("id")), "entrants": [slot.get("entrant", {}).get("name")
                             for slot in item.get("slots") or [] if slot.get("entrant")]}
                            for item in row.get("sets") or []]
                    result["streams"].append({"name": name, "source": info.get("streamSource"),
                                              "sets": sets})
                selected = next((row for row in result["streams"] if row["name"].casefold() == stream.casefold()), None)
                result["stream"] = "PASS" if selected else "FAIL" if stream else "NOT_CONFIGURED"
                result["selected_queue"] = selected["sets"] if selected else []
            return result
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            return {"status": "FAIL", "authentication": "UNKNOWN", "error": str(exc)[:160]}


def readiness(tsh: list[dict], obs: list[dict], frame: dict | None,
              startgg: dict, calibration_path: Path, settings: Settings | None = None) -> dict:
    blocking = []
    warnings = []
    if not any(item["status"] == "PASS" for item in tsh):
        blocking.append("TSH scoreboard is not reachable")
    elif not any(item.get("capabilities", {}).get("current_set") == "PASS" and
                 item.get("capabilities", {}).get("players") == "PASS" for item in tsh):
        blocking.append("TSH is running but no valid two-player set is loaded")
    if settings and (settings.demo or not any(item["url"] == settings.tsh_url for item in tsh)):
        blocking.append("Select a discovered TSH instance for AutoScore")
    if any(item.get("capabilities", {}).get("candidate_sets") == "FAIL" for item in tsh):
        warnings.append("TSH candidate sets are unavailable; next-set suggestion needs review")
    if not any(item["status"] == "PASS" for item in obs):
        blocking.append("OBS WebSocket is not connected")
    if not frame or frame["status"] != "PASS":
        blocking.append("OBS gameplay frame has not passed capture test")
    rois = load_calibration(calibration_path).rois
    if not all(key in rois for key in ("placement", "winner_badge", "loser_badge", "gameplay")):
        warnings.append("Calibrate result and gameplay regions before scoring")
    if startgg["status"] != "PASS":
        warnings.append("Start.gg is optional and has not passed validation")
    return {"status": "READY_FOR_REHEARSAL" if not blocking else "NOT_READY",
            "blocking": blocking, "warnings": warnings,
            "optional": ["Supermajor player data"]}
