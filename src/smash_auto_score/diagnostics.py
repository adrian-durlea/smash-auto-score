"""Opt-in local winner evidence captures for operator review."""

import json
import time
from dataclasses import asdict
from pathlib import Path

import cv2
import numpy as np

from .controller import Controller
from .domain import Side, Slot
from .vision import ROI, WinnerDetector


class WinnerDiagnostics:
    def __init__(self, root: Path, controller: Controller, rois: dict[str, ROI] | None = None):
        self.root, self.controller = root, controller
        self.rois = rois or {}
        self._last: dict[str, float] = {}

    def record(self, kind: str, frame: np.ndarray, *, actual: Slot | None = None,
               actual_side: Side | None = None) -> Path | None:
        if kind not in {"candidate", "confirmed", "result", "unknown", "manual-p1",
                        "manual-p2", "correction"}:
            raise ValueError("Unknown diagnostic category")
        now = time.time()
        event = f"{self.controller.match.set_id if self.controller.match else 'none'}:{self.controller.machine.generation}"
        key = f"{event}:{kind}"
        if now - self._last.get(key, 0) < 1:
            return None
        self._last[key] = now
        directory = self.root / kind
        directory.mkdir(parents=True, exist_ok=True)
        stem = f"{int(now * 1000)}-{self.controller.machine.generation}"
        image_path = directory / f"{stem}.jpg"
        if not cv2.imwrite(str(image_path), frame):
            return None
        for name, default in WinnerDetector.DEFAULT_ROIS.items():
            roi = self.rois.get(name, default)
            crop = roi.crop(frame)
            if crop.size:
                cv2.imwrite(str(directory / f"{stem}-{name}.jpg"), crop)
        machine = self.controller.machine
        metadata = {"timestamp": now, "set_id": self.controller.match.set_id if self.controller.match else None,
                    "game_generation": machine.generation, "kind": kind,
                    "predicted_winner": machine.winner.value if machine.winner else None,
                    "winner_confidence": machine.winner_confidence,
                    "end_detection": asdict(machine.end_detection),
                    "winner_evidence": machine.winner_evidence,
                    "actual_winner": actual.value if actual else None}
        metadata["actual_side"] = actual_side.value if actual_side else None
        image_path.with_suffix(".json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        return image_path
