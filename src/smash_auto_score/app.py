import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from .config import Settings
from .controller import Controller
from .domain import Mode, Observation, PlayerIdentity, Side, Slot
from .integrations import (
    DemoTSHAdapter,
    OBSVideoSource,
    RecordedVideoSource,
    StartGGProvider,
    TSHWebAdapter,
)
from .store import Store
from .vision import ROI, Calibration, TesseractOCR, dominant_color
from .worker import (
    VideoWorker,
    activate_profile,
    calibration_profiles,
    load_calibration,
    save_calibration,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
settings = Settings()
controller = Controller(DemoTSHAdapter() if settings.demo else TSHWebAdapter(settings.tsh_url, settings.tsh_scoreboard),
                        Store(settings.database_path), settings)
video_worker: VideoWorker | None = None
if settings.recorded_video:
    video_worker = VideoWorker(RecordedVideoSource(settings.recorded_video), controller,
                               Path(settings.calibration_path), settings.frame_fps,
                               settings.ocr_interval, settings.color_interval)
elif settings.obs_source:
    video_worker = VideoWorker(OBSVideoSource(settings.obs_host, settings.obs_port,
                               settings.obs_password, settings.obs_source,
                               settings.frame_width, settings.frame_height), controller,
                               Path(settings.calibration_path), settings.frame_fps,
                               settings.ocr_interval, settings.color_interval)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        await controller.refresh()
    except Exception as exc:  # noqa: BLE001 - external service may fail in many ways
        logging.getLogger(__name__).warning("Initial TSH read failed: %s", exc)
    task = asyncio.create_task(video_worker.run()) if video_worker else None
    next_task = asyncio.create_task(next_set_loop())
    yield
    next_task.cancel()
    try:
        await next_task
    except asyncio.CancelledError:
        pass
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Smash AutoScore", lifespan=lifespan)


async def next_set_loop():
    assigned_cache: set[str] | None = None
    assigned_fetched_at = 0.0
    last_candidate_check = 0.0
    while True:
        await asyncio.sleep(2)
        try:
            match = await controller.refresh()
            if (match.complete and controller.set_mode != Mode.OFF
                    and not controller.next_dismissed and controller.set_complete_at is not None
                    and time.time() - controller.set_complete_at >= settings.post_set_delay_seconds
                    and (not controller.next_candidate or
                         (controller.set_mode == Mode.AUTO and controller.observation.game_active))
                    and time.time() - last_candidate_check >= 10):
                last_candidate_check = time.time()
                if (settings.startgg_token and settings.startgg_tournament_slug and settings.startgg_stream_name
                        and time.time() - assigned_fetched_at >= 60):
                    assigned_cache = await StartGGProvider(settings.startgg_token).stream_set_ids(
                        settings.startgg_tournament_slug, settings.startgg_stream_name)
                    assigned_fetched_at = time.time()
                await controller.suggest_next_set(assigned_cache)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - polling must survive external failures
            logging.getLogger(__name__).warning("Next-set poll failed: %s", exc)


class MappingRequest(BaseModel):
    p1_is_left: bool


class ModeRequest(BaseModel):
    mode: Mode


@app.post("/api/swap-mode")
async def swap_mode(body: ModeRequest):
    controller.swap_mode = body.mode
    controller.store.log("swap_mode", {"mode": body.mode.value})
    return {"mode": body.mode.value}


class ReplayObservation(BaseModel):
    tags: dict[Slot, str] = {}
    colors: dict[Slot, str] = {}
    characters: dict[Slot, str] = {}
    game_active: bool = False
    game_set: bool = False
    result_screen: bool = False
    winner: Slot | None = None
    winner_confidence: float = 0


async def _do(coro):
    try:
        return await coro
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        logging.getLogger(__name__).exception("Integration failure")
        raise HTTPException(503, str(exc)) from exc


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")


@app.get("/api/status")
async def status():
    result = await controller.status()
    result["video"] = {"connected": video_worker.connected if video_worker else False,
                       "source": settings.recorded_video or settings.obs_source or None,
                       "fps": settings.frame_fps, "error": video_worker.error if video_worker else "No source configured"}
    return result


@app.get("/api/frame")
async def frame():
    if not video_worker or not video_worker.latest_frame:
        raise HTTPException(404, "No video frame available")
    return Response(video_worker.latest_frame, media_type="image/jpeg")


@app.get("/api/calibration")
async def calibration():
    return load_calibration(Path(settings.calibration_path))


@app.get("/api/calibration/profiles")
async def profiles():
    active, names = calibration_profiles(Path(settings.calibration_path))
    return {"active": active, "profiles": names}


@app.post("/api/calibration/profile/{name}")
async def profile(name: str):
    try:
        activate_profile(Path(settings.calibration_path), name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if video_worker:
        video_worker.reload_calibration()
    return {"active": name}


class PlayerOverrideRequest(BaseModel):
    side: Side
    aliases: list[str] = []
    character_distribution: dict[str, float] = {}
    supermajor_id: str | None = None


@app.post("/api/player-override")
async def player_override(body: PlayerOverrideRequest):
    match = await _do(controller.refresh())
    player: PlayerIdentity = match.left if body.side == Side.LEFT else match.right
    if any(value < 0 or value > 1 for value in body.character_distribution.values()):
        raise HTTPException(400, "Character probabilities must be between 0 and 1")
    player.aliases = body.aliases
    player.character_distribution = {key.casefold(): value for key, value in body.character_distribution.items()}
    if body.supermajor_id is not None:
        proposed = body.supermajor_id.strip().upper()
        if proposed and not re.fullmatch(r"S?[0-9]+", proposed):
            raise HTTPException(400, "Supermajor ID must be S followed by digits")
        player.supermajor_id = proposed or None
    controller.player_data.save(player)
    return {"ok": True}


@app.post("/api/supermajor/refresh/{side}")
async def refresh_supermajor(side: Side):
    match = await _do(controller.refresh())
    player = match.left if side == Side.LEFT else match.right
    result = await controller.supermajor.lookup(player, force=True)
    return result


class SupermajorIdRequest(BaseModel):
    side: Side
    supermajor_id: str


@app.post("/api/supermajor/id")
async def attach_supermajor_id(body: SupermajorIdRequest):
    proposed = body.supermajor_id.strip().upper()
    if proposed and not re.fullmatch(r"S?[0-9]+", proposed):
        raise HTTPException(400, "Supermajor ID must be S followed by digits")
    match = await _do(controller.refresh())
    player = match.left if body.side == Side.LEFT else match.right
    local = controller.store.cache_get(controller.player_data.key(player)) or {}
    player.aliases = local.get("aliases", [])
    player.character_distribution = local.get("character_distribution", {})
    player.supermajor_id = proposed or None
    controller.player_data.save(player)
    return {"ok": True, "supermajor_id": player.supermajor_id}


class CharacterTemplateRequest(BaseModel):
    slot: Slot
    character: str


@app.post("/api/characters/template")
async def save_character_template(body: CharacterTemplateRequest):
    if not video_worker or not video_worker.latest_frame:
        raise HTTPException(404, "No video frame available")
    roi = video_worker.detector.calibration.rois.get(f"{body.slot.value.lower()}_character")
    if not roi:
        raise HTTPException(400, "Calibrate the character region first")
    image = cv2.imdecode(np.frombuffer(video_worker.latest_frame, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise HTTPException(503, "Cannot decode current frame")
    try:
        path = video_worker.detector.characters.save(roi.crop(image), body.slot, body.character)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"saved": path.name, "slot": body.slot.value}


class CalibrationRequest(BaseModel):
    rois: dict[str, ROI]


@app.put("/api/calibration")
async def update_calibration(body: CalibrationRequest):
    allowed = {"p1_tag", "p2_tag", "p1_hud", "p2_hud", "p1_character", "p2_character",
               "gameplay", "game_set", "result", "winner"}
    if not set(body.rois).issubset(allowed):
        raise HTTPException(400, "Unknown ROI name")
    for roi in body.rois.values():
        if min(roi.x, roi.y, roi.w, roi.h) < 0 or roi.x + roi.w > 1 or roi.y + roi.h > 1:
            raise HTTPException(400, "ROI coordinates must fit within the frame")
    save_calibration(Path(settings.calibration_path), Calibration(body.rois))
    if video_worker:
        video_worker.reload_calibration()
    return {"ok": True}


@app.get("/api/calibration/test/{name}")
async def test_region(name: str):
    calibration = load_calibration(Path(settings.calibration_path))
    roi = calibration.rois.get(name)
    if not roi or not video_worker or not video_worker.latest_frame:
        raise HTTPException(404, "Region or frame unavailable")
    frame = cv2.imdecode(np.frombuffer(video_worker.latest_frame, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise HTTPException(503, "Cannot decode current frame")
    crop = roi.crop(frame)
    if name.endswith("hud"):
        return {"color": dominant_color(crop)}
    if name.endswith("character") and video_worker:
        slot = Slot.P1 if name.startswith("p1") else Slot.P2
        character, confidence = video_worker.detector.characters.detect(crop, slot)
        return {"character": character, "confidence": confidence}
    try:
        value, confidence = await asyncio.to_thread(TesseractOCR().read, crop)
        return {"text": value, "confidence": confidence}
    except Exception as exc:
        raise HTTPException(503, f"OCR unavailable: {exc}") from exc


@app.post("/api/arm/{armed}")
async def arm(armed: bool):
    controller.armed = armed
    controller.store.log("auto_score_arm", {"armed": armed})
    return {"armed": armed}


@app.post("/api/score/{side}")
async def manual_score(side: Side):
    return {"applied": await _do(controller.manual_score(side))}


@app.post("/api/undo")
async def undo():
    return {"undone": await _do(controller.undo())}


@app.post("/api/mapping")
async def mapping(body: MappingRequest):
    controller.override_mapping(body.p1_is_left)
    return {"ok": True}


@app.post("/api/mode")
async def mode(body: ModeRequest):
    controller.set_mode = body.mode
    controller.store.log("set_mode", {"mode": body.mode.value})
    return {"mode": body.mode.value}


@app.post("/api/swap")
async def swap():
    await _do(controller.tsh.swap_players())
    await _do(controller.refresh())
    controller.previous_mapping = controller.manual_mapping = None
    controller.store.log("players_swapped", {})
    return {"ok": True}


@app.post("/api/reset-game")
async def reset_game():
    controller.machine.reset()
    controller.store.log("game_reset", {})
    return {"ok": True}


@app.post("/api/reset-set")
async def reset_set():
    controller.machine.reset()
    controller.previous_mapping = controller.manual_mapping = None
    controller.store.log("set_reset", {})
    return {"ok": True}


@app.post("/api/next/suggest")
async def suggest():
    assigned = None
    if settings.startgg_token and settings.startgg_tournament_slug and settings.startgg_stream_name:
        assigned = await _do(StartGGProvider(settings.startgg_token).stream_set_ids(
            settings.startgg_tournament_slug, settings.startgg_stream_name))
    return await _do(controller.suggest_next_set(assigned))


@app.post("/api/next/load")
async def load():
    await _do(controller.load_next_set())
    return {"ok": True}


@app.post("/api/next/ignore")
async def ignore():
    controller.next_candidate = None
    controller.next_dismissed = True
    return {"ok": True}


@app.post("/api/replay/observation")
async def replay(body: ReplayObservation):
    if not settings.demo:
        raise HTTPException(403, "Replay endpoint only enabled in demo mode")
    await _do(controller.ingest(Observation(**body.model_dump())))
    return await controller.status()


def main():
    import uvicorn
    uvicorn.run("smash_auto_score.app:app", host="127.0.0.1", port=8765, reload=False)
