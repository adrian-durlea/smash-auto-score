"""Calibrated, deliberately conservative frame observations."""
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Protocol

import cv2
import numpy as np

from .domain import Observation, Slot


@dataclass
class ROI:
    x: float
    y: float
    w: float
    h: float

    def crop(self, image: np.ndarray) -> np.ndarray:
        height, width = image.shape[:2]
        x1, y1 = round(self.x * width), round(self.y * height)
        x2, y2 = round((self.x + self.w) * width), round((self.y + self.h) * height)
        return image[max(0, y1):min(height, y2), max(0, x1):min(width, x2)]


@dataclass
class Calibration:
    rois: dict[str, ROI] = field(default_factory=dict)


class OCRProvider(Protocol):
    def read(self, crop: np.ndarray) -> tuple[str, float]: ...


class TesseractOCR:
    def read(self, crop: np.ndarray) -> tuple[str, float]:
        import pytesseract
        if not crop.size:
            return "", 0.0
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, None, fx=3, fy=3)
        try:
            data = pytesseract.image_to_data(gray, config="--psm 7", output_type=pytesseract.Output.DICT)
        except pytesseract.TesseractNotFoundError:
            return "", 0.0
        values = [(word.strip(), float(conf)) for word, conf in zip(data["text"], data["conf"])
                  if word.strip() and float(conf) > 0]
        return " ".join(word for word, _ in values), max((conf / 100 for _, conf in values), default=0.0)


class CharacterDetector(Protocol):
    def detect(self, image: np.ndarray, slot: Slot) -> tuple[str | None, float]: ...
    def reset(self) -> None: ...
    def save(self, crop: np.ndarray, slot: Slot, character: str) -> Path: ...


class NoCharacterDetector:
    def detect(self, image: np.ndarray, slot: Slot) -> tuple[str | None, float]:
        return None, 0.0

    def reset(self) -> None:
        pass

    def save(self, crop: np.ndarray, slot: Slot, character: str) -> Path:
        raise ValueError("Character templates are not configured")


class WinnerDetector:
    """Read the first-place badge and opposite second-place badge on results."""

    DEFAULT_ROIS: ClassVar[dict[str, ROI]] = {
        "placement": ROI(350 / 1920, 115 / 1080, 300 / 1920, 220 / 1080),
        "winner_badge": ROI(90 / 1920, 130 / 1080, 220 / 1920, 140 / 1080),
        "loser_badge": ROI(670 / 1920, 950 / 1080, 140 / 1920, 110 / 1080),
    }

    def __init__(self, ocr: OCRProvider):
        self.ocr = ocr

    def detect(self, crop: np.ndarray) -> tuple[Slot | None, float]:
        text, confidence = self.ocr.read(crop)
        upper = text.upper()
        p1 = bool(re.search(r"\b(?:P1|PLAYER\s*1)\s+(?:WIN|WINS|WINNER)\b", upper))
        p2 = bool(re.search(r"\b(?:P2|PLAYER\s*2)\s+(?:WIN|WINS|WINNER)\b", upper))
        if confidence < .97 or p1 == p2:
            return None, 0.0
        return (Slot.P1 if p1 else Slot.P2), confidence

    @staticmethod
    def _badge(crop: np.ndarray, margin: float = .25) -> tuple[str | None, float]:
        if not crop.size:
            return None, 0.0
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        h, s, v = cv2.split(hsv)
        vivid = (s > 105) & (v > 65)
        red = float(np.mean(vivid & ((h < 12) | (h > 170))))
        blue = float(np.mean(vivid & (h > 95) & (h < 130)))
        if max(red, blue) < .28 or abs(red - blue) < margin:
            return None, 0.0
        return ("red", red) if red > blue else ("blue", blue)

    @staticmethod
    def _placement(crop: np.ndarray) -> float:
        if not crop.size:
            return 0.0
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        h, s, v = cv2.split(hsv)
        return float(np.mean((h > 15) & (h < 45) & (s > 120) & (v > 100)))

    def detect_visual(self, frame: np.ndarray, calibration: Calibration
                      ) -> tuple[bool, Slot | None, float, list[str]]:
        height, width = frame.shape[:2]
        if height == 0 or abs(width / height - 16 / 9) > .06:
            return False, None, 0.0, []
        regions = {key: calibration.rois.get(key, roi).crop(frame)
                   for key, roi in self.DEFAULT_ROIS.items()}
        gold = self._placement(regions["placement"])
        if gold < .16 or gold > .35:
            return False, None, 0.0, []
        first, first_strength = self._badge(regions["winner_badge"], .10)
        second, second_strength = self._badge(regions["loser_badge"])
        evidence = [f"first-place numeral region: gold {gold:.0%}"]
        if first:
            evidence.append(f"first-place badge: {first} {first_strength:.0%}")
        if second:
            evidence.append(f"second-place badge: {second} {second_strength:.0%}")
        if first and second and first != second:
            slot = Slot.P1 if first == "red" else Slot.P2
            return True, slot, .98, evidence
        return bool(first or second), None, 0.0, evidence


def dominant_color(crop: np.ndarray) -> str | None:
    if not crop.size:
        return None
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mask = (hsv[:, :, 1] > 85) & (hsv[:, :, 2] > 60)
    if mask.sum() < crop.shape[0] * crop.shape[1] * 0.08:
        return None
    hue = np.median(hsv[:, :, 0][mask])
    selected = crop[mask & (np.abs(hsv[:, :, 0].astype(float) - hue) < 12)]
    if len(selected) == 0:
        return None
    b, g, r = np.median(selected, axis=0).astype(int)
    return f"#{r:02x}{g:02x}{b:02x}"


class FrameDetector:
    def __init__(self, calibration: Calibration, ocr: OCRProvider | None = None,
                 characters: CharacterDetector | None = None):
        self.calibration = calibration
        self.ocr = ocr or TesseractOCR()
        self.characters = characters or NoCharacterDetector()
        self.winner = WinnerDetector(self.ocr)
        self.last_timings: dict[str, float] = {}

    def observe(self, frame: np.ndarray, *, do_ocr: bool = True, do_color: bool = True,
                do_character: bool = True) -> Observation:
        obs = Observation()
        started = time.perf_counter()
        self.last_timings = {}
        for slot in Slot:
            tag_roi = self.calibration.rois.get(f"{slot.value.lower()}_tag")
            color_roi = self.calibration.rois.get(f"{slot.value.lower()}_hud")
            if do_ocr and tag_roi:
                text, conf = self.ocr.read(tag_roi.crop(frame))
                if conf >= 0.5:
                    obs.tags[slot] = text
            if do_color and color_roi:
                color = dominant_color(color_roi.crop(frame))
                if color:
                    obs.colors[slot] = color
            character_roi = self.calibration.rois.get(f"{slot.value.lower()}_character")
            if do_character and character_roi:
                character_started = time.perf_counter()
                character, confidence = self.characters.detect(character_roi.crop(frame), slot)
                self.last_timings["character_ms"] = self.last_timings.get("character_ms", 0.0) + (
                    time.perf_counter() - character_started) * 1000
                if character and confidence >= .72:
                    obs.characters[slot] = character
        active_roi = self.calibration.rois.get("gameplay")
        if active_roi:
            crop = active_roi.crop(frame)
            obs.game_active = bool(crop.size and np.std(crop) > 22)
        obs.hud_visible = len(obs.characters) == 2
        obs.game_active = obs.game_active or obs.hud_visible
        game_set_roi = self.calibration.rois.get("game_set")
        if game_set_roi and do_ocr:
            text, conf = self.ocr.read(game_set_roi.crop(frame))
            obs.game_set = "GAME SET" in text.upper() and conf >= 0.65
        result_roi = self.calibration.rois.get("result")
        if result_roi and do_ocr:
            text, conf = self.ocr.read(result_roi.crop(frame))
            obs.result_screen = ("RESULT" in text.upper() or "WINNER" in text.upper()) and conf >= 0.7
        winner_started = time.perf_counter()
        visual_result, visual_winner, visual_confidence, visual_evidence = (
            self.winner.detect_visual(frame, self.calibration))
        if visual_result and (visual_winner or not obs.hud_visible):
            obs.result_screen = True
            obs.result_confidence = .98
            obs.end_evidence.append("first-place results layout")
            obs.winner = visual_winner
            obs.winner_confidence = visual_confidence
            obs.winner_evidence = visual_evidence
            obs.characters.clear()
            obs.hud_visible = False
        winner_roi = self.calibration.rois.get("winner")
        if winner_roi and do_ocr and not visual_winner:
            obs.winner, obs.winner_confidence = self.winner.detect(winner_roi.crop(frame))
            if obs.winner:
                obs.winner_evidence.append("explicit slot win text")
        if obs.game_set:
            obs.end_evidence.append("GAME SET text")
        self.last_timings["winner_ms"] = (time.perf_counter() - winner_started) * 1000
        self.last_timings["total_ms"] = (time.perf_counter() - started) * 1000
        return obs
