import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path

import cv2

from .characters import TemplateCharacterDetector
from .controller import Controller
from .diagnostics import WinnerDiagnostics
from .domain import Slot
from .integrations import VideoSource
from .state import GamePhase
from .vision import ROI, Calibration, FrameDetector

log = logging.getLogger(__name__)


def load_calibration(path: Path) -> Calibration:
    if not path.exists():
        return Calibration()
    data = json.loads(path.read_text(encoding="utf-8"))
    if "profiles" in data:
        data = data["profiles"].get(data.get("active", "Default"), {})
    return Calibration({key: ROI(**value) for key, value in data.get("rois", {}).items()})


def save_calibration(path: Path, calibration: Calibration) -> None:
    from dataclasses import asdict
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if "profiles" not in data:
        data = {"active": "Default", "profiles": {"Default": data or {"rois": {}}}}
    data["profiles"][data["active"]] = asdict(calibration)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def calibration_profiles(path: Path) -> tuple[str, list[str]]:
    if not path.exists():
        return "Default", ["Default"]
    data = json.loads(path.read_text(encoding="utf-8"))
    return (data.get("active", "Default"), list(data.get("profiles", {"Default": {}})))


def activate_profile(path: Path, name: str) -> None:
    if not name or len(name) > 60 or any(c in name for c in "<>/\\:"):
        raise ValueError("Invalid profile name")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if "profiles" not in data:
        data = {"active": "Default", "profiles": {"Default": data or {"rois": {}}}}
    data["profiles"].setdefault(name, {"rois": {}})
    data["active"] = name
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


class VideoWorker:
    def __init__(self, source: VideoSource, controller: Controller, calibration_path: Path,
                 fps: float = 5, ocr_interval: float = 1, color_interval: float = .5):
        self.source, self.controller, self.calibration_path = source, controller, calibration_path
        self.fps, self.ocr_interval, self.color_interval = fps, ocr_interval, color_interval
        self.detector = FrameDetector(load_calibration(calibration_path), characters=
                                      TemplateCharacterDetector(Path(controller.settings.character_template_path)))
        self.latest_frame: bytes | None = None
        self.connected = False
        self.error: str | None = None
        self._last_ocr = 0.0
        self._last_color = 0.0
        self._last_character = 0.0
        self._generation = controller.machine.generation
        self._last_characters: dict[Slot, str] = {}
        self._captures: deque[float] = deque(maxlen=200)
        self._analyses: deque[float] = deque(maxlen=200)
        self._processing: deque[float] = deque(maxlen=200)
        self.dropped_analysis_frames = 0
        self.winner_diagnostics = (WinnerDiagnostics(Path(controller.settings.winner_diagnostics_path),
                                                     controller, self.detector.calibration.rois)
                                   if controller.settings.winner_diagnostics_enabled else None)
        self._diagnosed_end: set[tuple[str, int]] = set()

    def telemetry(self) -> dict:
        now = time.monotonic()
        captures = sum(t >= now - 5 for t in self._captures)
        analyses = sum(t >= now - 5 for t in self._analyses)
        return {"capture_fps": round(captures / 5, 1),
                "analysis_fps": round(analyses / 5, 1),
                "average_frame_ms": round(sum(self._processing) / len(self._processing), 1)
                if self._processing else 0.0,
                "character_ms": round(self.detector.last_timings.get("character_ms", 0), 1),
                "winner_ms": round(self.detector.last_timings.get("winner_ms", 0), 1),
                "game_state_ms": round(self.controller.last_state_ms, 1),
                "tsh_roundtrip_ms": round(self.controller.last_tsh_ms, 1),
                "obs_roundtrip_ms": round(getattr(self.source, "last_roundtrip_ms", 0), 1)
                if hasattr(self.source, "last_roundtrip_ms") else None,
                "dropped_analysis_frames": self.dropped_analysis_frames}

    def reload_calibration(self) -> None:
        self.detector.calibration = load_calibration(self.calibration_path)
        self.detector.characters.reset()
        self._last_characters.clear()
        if self.winner_diagnostics:
            self.winner_diagnostics.rois = self.detector.calibration.rois

    async def run(self) -> None:
        while True:
            started = time.monotonic()
            had_error = False
            captured = False
            try:
                frame = await self.source.frame()
                captured = True
                self._captures.append(time.monotonic())
                if not self.connected:
                    self.controller.store.log("video_connected", {})
                self.connected = True
                self.error = None
                ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if ok:
                    self.latest_frame = encoded.tobytes()
                now = time.monotonic()
                if self._generation != self.controller.machine.generation:
                    self.detector.characters.reset()
                    self._last_characters.clear()
                    self._generation = self.controller.machine.generation
                do_ocr = now - self._last_ocr >= self.ocr_interval
                do_color = now - self._last_color >= self.color_interval
                do_character = now - self._last_character >= self.controller.settings.character_interval
                obs = await asyncio.to_thread(self.detector.observe, frame,
                                              do_ocr=do_ocr, do_color=do_color,
                                              do_character=do_character)
                if do_character:
                    self._last_characters = obs.characters.copy()
                else:
                    obs.characters = self._last_characters.copy()
                obs.hud_visible = len(obs.characters) == 2 and obs.result_screen is not True
                obs.game_active = obs.game_active or obs.hud_visible
                if not do_ocr:
                    obs.game_set = None
                obs.timestamp = time.time()
                if do_ocr:
                    self._last_ocr = now
                if do_color:
                    self._last_color = now
                if do_character:
                    self._last_character = now
                await self.controller.ingest(obs)
                if self.winner_diagnostics:
                    try:
                        phase = self.controller.machine.phase
                        if phase == GamePhase.END_CANDIDATE:
                            self.winner_diagnostics.record("candidate", frame)
                        elif phase == GamePhase.RESULT:
                            kind = "result" if self.controller.machine.winner else "unknown"
                            self.winner_diagnostics.record(kind, frame)
                        end_key = (self.controller.match.set_id if self.controller.match else "",
                                   self.controller.machine.generation)
                        if (self.controller.machine.end_detection.ended and
                                end_key not in self._diagnosed_end):
                            self.winner_diagnostics.record("confirmed", frame)
                            self._diagnosed_end.add(end_key)
                    except OSError as exc:
                        log.warning("Winner diagnostic capture failed: %s", exc)
                self._analyses.append(time.monotonic())
                self._processing.append(self.detector.last_timings.get("total_ms", 0))
            except EOFError:
                self.connected = False
                self.error = "Recording ended"
                self.controller.store.log("video_ended", {})
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - video source must reconnect
                if self.connected:
                    self.controller.store.log("video_disconnected", {"error": str(exc)[:160]})
                self.connected = captured
                self.error = str(exc)
                self.controller.armed = False
                log.warning("Video cycle failed: %s", exc)
                had_error = True
                self.dropped_analysis_frames += 1
                await asyncio.sleep(1)
            if not had_error and time.monotonic() - started > 1 / self.fps:
                self.dropped_analysis_frames += 1
            await asyncio.sleep(max(0.05, 1 / self.fps - (time.monotonic() - started)))
