import asyncio
import logging
import re
import time
from collections.abc import Mapping
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
    VideoSource,
)
from .setup_wizard import (
    discover_obs,
    discover_tsh,
    probe_startgg,
    readiness,
    save_local_settings,
    test_obs_frame,
    tournament_slug,
)
from .store import Store
from .telemetry import ProcessSampler
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
video_task: asyncio.Task | None = None
setup_preview: bytes | None = None
setup_frame_result: dict | None = None
process_sampler = ProcessSampler()
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
    global video_task
    try:
        await controller.refresh()
    except Exception as exc:  # noqa: BLE001 - external service may fail in many ways
        logging.getLogger(__name__).warning("Initial TSH read failed: %s", exc)
    video_task = asyncio.create_task(video_worker.run()) if video_worker else None
    next_task = asyncio.create_task(next_set_loop())
    yield
    next_task.cancel()
    try:
        await next_task
    except asyncio.CancelledError:
        pass
    if video_task:
        video_task.cancel()
        try:
            await video_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Smash AutoScore", lifespan=lifespan)


async def _reconfigure_video() -> None:
    global video_worker, video_task
    if video_task:
        video_task.cancel()
        try:
            await video_task
        except asyncio.CancelledError:
            pass
        video_task = None
    if settings.recorded_video:
        source: VideoSource = RecordedVideoSource(settings.recorded_video)
    elif settings.obs_source:
        source = OBSVideoSource(settings.obs_host, settings.obs_port, settings.obs_password,
                                settings.obs_source, settings.frame_width, settings.frame_height)
    else:
        video_worker = None
        return
    video_worker = VideoWorker(source, controller, Path(settings.calibration_path),
                               settings.frame_fps, settings.ocr_interval, settings.color_interval)
    video_task = asyncio.create_task(video_worker.run())


def _persist(values: Mapping[str, object]) -> None:
    save_local_settings(Path(".env"), values)
    for key, value in values.items():
        setattr(settings, key, value)


class TSHSelection(BaseModel):
    url: str
    scoreboard: int = 1


class OBSSelection(BaseModel):
    host: str = "127.0.0.1"
    port: int = 4455
    source: str = ""
    password: str = ""


class StartGGSelection(BaseModel):
    token: str = ""
    tournament: str = ""
    stream: str = ""


@app.get("/setup", response_class=HTMLResponse)
def setup_page():
    return Path(__file__).with_name("setup.html").read_text(encoding="utf-8")


@app.get("/api/setup/discover")
async def setup_discover():
    tsh, obs = await asyncio.gather(discover_tsh(settings), discover_obs(settings))
    return {"tsh": tsh, "obs": obs, "configured": {
        "demo": settings.demo, "tsh_url": settings.tsh_url, "obs_source": settings.obs_source,
        "startgg_token_masked": "••••" + settings.startgg_token[-4:] if settings.startgg_token else None,
        "startgg_tournament_slug": settings.startgg_tournament_slug,
        "startgg_stream_name": settings.startgg_stream_name},
        "tsh_launch_hint": "If TSH reports missing ./user_data/settings.json, launch TSH.exe with its extracted folder as the working directory."}


@app.post("/api/setup/tsh")
async def select_tsh(body: TSHSelection):
    from urllib.parse import urlparse
    parsed = urlparse(body.url)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise HTTPException(400, "TSH setup accepts local HTTP addresses only")
    if not 1 <= body.scoreboard <= 8:
        raise HTTPException(400, "Invalid scoreboard number")
    result = await probe_selected_tsh(body.url, body.scoreboard)
    if result["status"] != "PASS":
        raise HTTPException(409, result.get("error", "TSH unavailable"))
    _persist({"tsh_url": body.url, "tsh_scoreboard": body.scoreboard, "demo": False})
    controller.tsh = TSHWebAdapter(body.url, body.scoreboard)
    controller.armed = False
    controller.match = None
    controller.score_snapshot = None
    return result


async def probe_selected_tsh(url: str, scoreboard: int) -> dict:
    from .setup_wizard import probe_tsh
    return await probe_tsh(url, scoreboard)


@app.post("/api/setup/obs")
async def select_obs(body: OBSSelection):
    if body.host not in {"localhost", "127.0.0.1", "::1"} or not 1 <= body.port <= 65535:
        raise HTTPException(400, "OBS setup accepts local WebSocket addresses only")
    if not body.source.strip():
        raise HTTPException(400, "Choose an OBS source")
    result, preview = await test_obs_frame(body.host, body.port, body.password, body.source)
    if result["status"] != "PASS":
        raise HTTPException(409, result.get("error", "OBS frame unavailable"))
    global setup_preview, setup_frame_result
    setup_preview, setup_frame_result = preview, result
    _persist({"obs_host": body.host, "obs_port": body.port, "obs_source": body.source,
              "obs_password": body.password, "recorded_video": ""})
    controller.armed = False
    await _reconfigure_video()
    return result


@app.get("/api/setup/preview")
def setup_preview_frame():
    if not setup_preview:
        raise HTTPException(404, "No tested OBS frame")
    return Response(setup_preview, media_type="image/jpeg")


@app.post("/api/setup/startgg")
async def select_startgg(body: StartGGSelection):
    try:
        slug = tournament_slug(body.tournament) if body.tournament else ""
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    token = body.token.strip() or settings.startgg_token
    result = await probe_startgg(token, slug, body.stream)
    if result["authentication"] != "PASS":
        raise HTTPException(409, result.get("error", "Start.gg token invalid"))
    if slug and result.get("tournament") != "PASS":
        _persist({"startgg_token": token})
        return result
    if body.stream and result.get("stream") != "PASS":
        _persist({"startgg_token": token, "startgg_tournament_slug": slug})
        return result
    values = {"startgg_token": token, "startgg_tournament_slug": slug,
              "startgg_stream_name": body.stream}
    _persist(values)
    return result


@app.post("/api/setup/validate")
async def setup_validate():
    tsh, obs = await asyncio.gather(discover_tsh(settings), discover_obs(settings))
    startgg = await probe_startgg(settings.startgg_token, settings.startgg_tournament_slug,
                                   settings.startgg_stream_name)
    frame = setup_frame_result
    if settings.obs_source and any(item["status"] == "PASS" for item in obs):
        frame, _ = await test_obs_frame(settings.obs_host, settings.obs_port,
                                        settings.obs_password, settings.obs_source)
    ready = readiness(tsh, obs, frame, startgg, Path(settings.calibration_path), settings)
    if controller.external_score_change:
        ready["blocking"].append("Review the external TSH score change")
        ready["status"] = "NOT_READY"
    performance = ({**process_sampler.sample(), **video_worker.telemetry()}
                   if video_worker else process_sampler.sample())
    performance["status"] = ("NOT_MEASURED" if not video_worker or
                             performance.get("analysis_fps", 0) == 0 else
                             "PASS" if performance["analysis_fps"] >= settings.frame_fps * .8
                             and performance.get("dropped_analysis_frames", 0) == 0 else "REVIEW")
    active_profile, profiles = calibration_profiles(Path(settings.calibration_path))
    return {"tsh": tsh, "obs": obs, "frame": frame, "startgg": startgg,
            "calibration": {"profiles": profiles, "active": active_profile,
                            "suggested": active_profile if load_calibration(
                                Path(settings.calibration_path)).rois else None,
                            "needs_review": True},
            "performance": performance, "readiness": ready,
            "external_score_change": controller.external_score_change}


class DisposableScoreTest(BaseModel):
    side: Side
    disposable: bool


@app.post("/api/setup/tsh-score-test")
async def setup_score_test(body: DisposableScoreTest):
    if not body.disposable or controller.shadow_mode:
        raise HTTPException(409, "Select a disposable scoreboard and leave shadow mode first")
    async with controller.lock:
        before = await _do(controller.tsh.get_current_set())
        if before.set_id in ("", "0", "None") or before.left.display_name == "?" or before.right.display_name == "?":
            raise HTTPException(409, "Load a valid disposable two-player set first")
        if before.complete:
            raise HTTPException(409, "Use an incomplete disposable set")
        original_set_id = before.set_id
        original_left, original_right = before.left_score, before.right_score
        left = original_left + (body.side == Side.LEFT)
        right = original_right + (body.side == Side.RIGHT)
        controller.armed = False
        try:
            await controller.tsh.set_score(int(left), int(right))
            changed = await controller.tsh.get_current_set()
            if (changed.set_id, changed.left_score, changed.right_score) != (original_set_id, left, right):
                raise RuntimeError("Score increment readback failed; review TSH manually")
            await controller.tsh.set_score(original_left, original_right)
            restored = await controller.tsh.get_current_set()
            if (restored.set_id, restored.left_score, restored.right_score) != (
                    original_set_id, original_left, original_right):
                raise RuntimeError("Score restore readback failed; review TSH manually")
        except Exception as exc:
            controller.armed = False
            controller.store.log("setup_score_test_uncertain", {"error": str(exc)})
            raise HTTPException(409, str(exc)) from exc
        controller.score_snapshot = (original_set_id, original_left, original_right)
        controller.store.log("setup_score_test_pass", {"set_id": original_set_id, "side": body.side.value})
        return {"status": "PASS", "original_score": [original_left, original_right],
                "restored_score": [restored.left_score, restored.right_score]}


class ShadowRequest(BaseModel):
    enabled: bool


@app.post("/api/setup/shadow")
def set_shadow(body: ShadowRequest):
    controller.set_shadow_mode(body.enabled)
    return {"shadow_mode": controller.shadow_mode, "armed": controller.armed}


@app.get("/api/setup/rehearsal-report")
def rehearsal_report():
    import json
    from collections import Counter
    events = [event for event in reversed(controller.store.recent(1000))
              if controller.shadow_started_at and event["created"] >= controller.shadow_started_at]
    parsed = [{"created": event["created"], "kind": event["kind"],
               "detail": json.loads(event["detail"])} for event in events]
    ends = [event["detail"]["event_id"] for event in parsed if event["kind"] == "game_end"]
    predictions = [event["detail"] for event in parsed if event["kind"] == "would_score"]
    counts = Counter(item["event_id"] for item in predictions)
    summary = {"games_observed": len(ends), "game_ends": len(ends),
               "winner_predictions": [item["winner"] for item in predictions],
               "mapping_confident": sum(item["mapping_confidence"] >=
                                        settings.min_mapping_confidence for item in predictions),
               "would_be_tsh_updates": len(predictions),
               "duplicate_events": sum(count - 1 for count in counts.values()),
               "unknown_or_unscored": len(set(ends) - set(counts)),
               "obs_disconnects": sum(event["kind"] == "video_disconnected" for event in parsed),
               "tsh_errors": sum(event["kind"] in {"score_uncertain", "tsh_disconnected"}
                                 for event in parsed),
               "external_score_changes": sum(event["kind"] == "external_score_change" for event in parsed)}
    path = Path("diagnostics/rehearsal-live.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"summary": summary, "events": parsed}, indent=2), encoding="utf-8")
    path.with_suffix(".md").write_text("# Shadow rehearsal\n\n" + "\n".join(
        f"- {key.replace('_', ' ').title()}: {value}" for key, value in summary.items()) + "\n",
        encoding="utf-8")
    return {"summary": summary, "json": str(path), "markdown": str(path.with_suffix('.md'))}


@app.post("/api/setup/accept-external-score")
def accept_external_score():
    try:
        controller.accept_external_score()
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"status": "ACCEPTED", "armed": controller.armed}


@app.post("/api/setup/reconcile-score")
async def reconcile_score():
    return await _do(controller.reconcile_uncertain_score())


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
    result_confidence: float = 0
    hud_visible: bool = False
    winner_evidence: list[str] = []
    end_evidence: list[str] = []


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
    result["performance"] = ({**process_sampler.sample(), **video_worker.telemetry()}
                             if settings.performance_telemetry_enabled and video_worker else
                             process_sampler.sample() if settings.performance_telemetry_enabled else {})
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
               "gameplay", "game_set", "result", "winner", "placement", "winner_badge",
               "loser_badge"}
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
    if name in {"placement", "winner_badge", "loser_badge"}:
        found, visual_slot, confidence, evidence = video_worker.detector.winner.detect_visual(frame, calibration)
        return {"result_screen": found, "winner": visual_slot.value if visual_slot else None,
                "confidence": confidence, "evidence": evidence}
    try:
        value, confidence = await asyncio.to_thread(TesseractOCR().read, crop)
        return {"text": value, "confidence": confidence}
    except Exception as exc:
        raise HTTPException(503, f"OCR unavailable: {exc}") from exc


@app.post("/api/arm/{armed}")
async def arm(armed: bool):
    if armed and (controller.shadow_mode or controller.external_score_change):
        raise HTTPException(409, "Resolve shadow mode or external score review before arming")
    controller.armed = armed
    controller.store.log("auto_score_arm", {"armed": armed})
    return {"armed": armed}


@app.post("/api/score/{side}")
async def manual_score(side: Side):
    applied = await _do(controller.manual_score(side))
    if applied and video_worker and video_worker.winner_diagnostics and video_worker.latest_frame:
        image = cv2.imdecode(np.frombuffer(video_worker.latest_frame, np.uint8), cv2.IMREAD_COLOR)
        if image is not None:
            mapping = controller.mapping.p1_is_left
            actual: Slot | None = None
            if mapping is not None:
                actual = Slot.P1 if ((side == Side.LEFT) == mapping) else Slot.P2
            kind = f"manual-{actual.value.lower()}" if actual else "correction"
            try:
                video_worker.winner_diagnostics.record(kind, image, actual=actual, actual_side=side)
            except OSError as exc:
                logging.getLogger(__name__).warning("Winner diagnostic capture failed: %s", exc)
    return {"applied": applied}


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
    if controller.shadow_mode:
        raise HTTPException(409, "Shadow mode blocks TSH player swaps")
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
