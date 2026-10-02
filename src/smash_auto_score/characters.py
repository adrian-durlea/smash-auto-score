"""Operator-owned HUD portrait templates and conservative temporal recognition."""

import re
from collections import Counter, deque
from pathlib import Path

import cv2
import numpy as np

from .domain import Slot


def _features(image: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(cv2.resize(image, (96, 96)), cv2.COLOR_BGR2GRAY)
    return cv2.equalizeHist(gray)


class TemplateCharacterDetector:
    def __init__(self, root: Path, *, threshold: float = .72, margin: float = .06,
                 window: int = 5, votes: int = 3):
        self.root, self.threshold, self.margin = root, threshold, margin
        self.window, self.votes = window, votes
        self.history: dict[Slot, deque[tuple[str | None, float]]] = {
            slot: deque(maxlen=window) for slot in Slot}
        self.templates: dict[Slot, list[tuple[str, np.ndarray]]] = {slot: [] for slot in Slot}
        self.reload()

    def reload(self) -> None:
        self.templates = {slot: [] for slot in Slot}
        if not self.root.exists():
            return
        for slot in Slot:
            for path in sorted((self.root / slot.value).glob("*.png")):
                image = cv2.imread(str(path))
                if image is not None and image.size:
                    self.templates[slot].append((path.stem.rsplit("__", 1)[0], _features(image)))
        self.reset()

    def reset(self) -> None:
        for history in self.history.values():
            history.clear()

    def save(self, crop: np.ndarray, slot: Slot, character: str) -> Path:
        name = character.strip().casefold()
        if not re.fullmatch(r"[a-z0-9][a-z0-9 _-]{1,39}", name) or not crop.size:
            raise ValueError("Character name or crop is invalid")
        directory = self.root / slot.value
        directory.mkdir(parents=True, exist_ok=True)
        number = 1 + sum(path.stem.startswith(f"{name}__") for path in directory.glob("*.png"))
        path = directory / f"{name}__{number:03}.png"
        if not cv2.imwrite(str(path), crop):
            raise ValueError("Could not save template")
        self.reload()
        return path

    def detect(self, image: np.ndarray, slot: Slot) -> tuple[str | None, float]:
        if not image.size or not self.templates[slot]:
            self.history[slot].append((None, 0.0))
            return None, 0.0
        sample = _features(image)
        scores: dict[str, float] = {}
        for name, template in self.templates[slot]:
            score = float(cv2.matchTemplate(sample, template, cv2.TM_CCOEFF_NORMED)[0, 0])
            scores[name] = max(scores.get(name, -1.0), score)
        ranked = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
        name, score = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else -1.0
        accepted = name if score >= self.threshold and score - runner_up >= self.margin else None
        self.history[slot].append((accepted, score))
        count = Counter(label for label, _ in self.history[slot] if label)
        if not accepted or count[accepted] < self.votes:
            return None, 0.0
        return accepted, min(score, 0.99)
