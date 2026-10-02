"""Calibrated, deliberately conservative frame observations."""
import re
from dataclasses import dataclass, field
from typing import Protocol

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
        data = pytesseract.image_to_data(gray, config="--psm 7", output_type=pytesseract.Output.DICT)
        values = [(word.strip(), float(conf)) for word, conf in zip(data["text"], data["conf"])
                  if word.strip() and float(conf) > 0]
        return " ".join(word for word, _ in values), max((conf / 100 for _, conf in values), default=0.0)


class CharacterDetector(Protocol):
    def detect(self, image: np.ndarray, slot: Slot) -> tuple[str | None, float]: ...


class NoCharacterDetector:
    def detect(self, image: np.ndarray, slot: Slot) -> tuple[str | None, float]:
        return None, 0.0


class WinnerDetector:
    """Accept only explicit, unambiguous slot text from a calibrated winner region."""

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

    def observe(self, frame: np.ndarray, *, do_ocr: bool = True, do_color: bool = True) -> Observation:
        obs = Observation()
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
            if do_ocr and character_roi:
                character, confidence = self.characters.detect(character_roi.crop(frame), slot)
                if character and confidence >= .8:
                    obs.characters[slot] = character
        active_roi = self.calibration.rois.get("gameplay")
        if active_roi:
            crop = active_roi.crop(frame)
            obs.game_active = bool(crop.size and np.std(crop) > 22)
        game_set_roi = self.calibration.rois.get("game_set")
        if game_set_roi and do_ocr:
            text, conf = self.ocr.read(game_set_roi.crop(frame))
            obs.game_set = "GAME SET" in text.upper() and conf >= 0.65
        result_roi = self.calibration.rois.get("result")
        if result_roi and do_ocr:
            text, conf = self.ocr.read(result_roi.crop(frame))
            obs.result_screen = ("RESULT" in text.upper() or "WINNER" in text.upper()) and conf >= 0.7
        winner_roi = self.calibration.rois.get("winner")
        if winner_roi and do_ocr:
            obs.winner, obs.winner_confidence = WinnerDetector(self.ocr).detect(winner_roi.crop(frame))
        return obs
