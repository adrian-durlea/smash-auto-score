import asyncio
import json
import logging
import time
from pathlib import Path

import cv2

from .characters import TemplateCharacterDetector
from .controller import Controller
from .domain import Slot
from .integrations import VideoSource
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

    def reload_calibration(self) -> None:
        self.detector.calibration = load_calibration(self.calibration_path)
        self.detector.characters.reset()
        self._last_characters.clear()

    async def run(self) -> None:
        while True:
            started = time.monotonic()
            try:
                frame = await self.source.frame()
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
                if not do_ocr:
                    obs.game_set = None
                    obs.result_screen = None
                obs.timestamp = time.time()
                if do_ocr:
                    self._last_ocr = now
                if do_color:
                    self._last_color = now
                if do_character:
                    self._last_character = now
                await self.controller.ingest(obs)
            except EOFError:
                self.connected = False
                self.error = "Recording ended"
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - video source must reconnect
                self.connected = False
                self.error = str(exc)
                log.warning("Video cycle failed: %s", exc)
            await asyncio.sleep(max(0.05, 1 / self.fps - (time.monotonic() - started)))
