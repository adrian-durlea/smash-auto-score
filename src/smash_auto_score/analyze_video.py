"""Replay calibrated character detection against a bounded recording interval."""

import argparse
import csv
from pathlib import Path

import cv2

from .characters import TemplateCharacterDetector
from .domain import Slot
from .worker import load_calibration


def analyze(video: Path, calibration: Path, templates: Path, start: float,
            end: float, step: float, output: Path) -> None:
    if start < 0 or end <= start or step <= 0:
        raise ValueError("Require 0 <= start < end and step > 0")
    rois = load_calibration(calibration).rois
    for slot in Slot:
        if f"{slot.value.lower()}_character" not in rois:
            raise ValueError(f"Missing {slot.value} character ROI")
    detector = TemplateCharacterDetector(templates)
    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        raise ValueError(f"Cannot open {video}")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(("second", "slot", "character", "confidence"))
            second = start
            while second <= end:
                capture.set(cv2.CAP_PROP_POS_MSEC, second * 1000)
                ok, frame = capture.read()
                if not ok:
                    break
                for slot in Slot:
                    crop = rois[f"{slot.value.lower()}_character"].crop(frame)
                    name, confidence = detector.detect(crop, slot)
                    writer.writerow((f"{second:.2f}", slot.value, name or "unknown",
                                     f"{confidence:.3f}"))
                second += step
    finally:
        capture.release()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--calibration", type=Path, default=Path("config/calibration.json"))
    parser.add_argument("--templates", type=Path, default=Path("config/character_templates"))
    parser.add_argument("--start", type=float, required=True)
    parser.add_argument("--end", type=float, required=True)
    parser.add_argument("--step", type=float, default=5)
    parser.add_argument("--output", type=Path, default=Path("diagnostics/characters.csv"))
    args = parser.parse_args()
    analyze(args.video, args.calibration, args.templates, args.start, args.end,
            args.step, args.output)
    print(args.output)


if __name__ == "__main__":
    main()
