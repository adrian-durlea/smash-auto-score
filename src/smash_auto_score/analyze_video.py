"""Replay calibrated character detection against a bounded recording interval."""

import argparse
import csv
import json
import time
from pathlib import Path

import cv2

from .characters import TemplateCharacterDetector
from .domain import Slot
from .state import GameStateMachine
from .telemetry import process_rss_bytes
from .vision import FrameDetector
from .worker import load_calibration


class _NullOCR:
    def read(self, _crop):
        return "", 0.0


def analyze_games(video: Path, calibration: Path, templates: Path, start: float,
                  end: float, step: float, output: Path) -> list[dict]:
    """Analyze one recording window without making any score writes."""
    detector = FrameDetector(load_calibration(calibration), _NullOCR(),
                             TemplateCharacterDetector(templates))
    machine = GameStateMachine()
    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        raise ValueError(f"Cannot open {video}")
    games: list[dict] = []
    current: dict | None = None
    second = start
    wall_start, cpu_start = time.perf_counter(), time.process_time()
    frames = 0
    frame_ms = winner_ms = character_ms = 0.0
    try:
        while second <= end:
            capture.set(cv2.CAP_PROP_POS_MSEC, second * 1000)
            ok, frame = capture.read()
            if not ok:
                break
            obs = detector.observe(frame, do_ocr=False, do_color=False)
            frames += 1
            frame_ms += detector.last_timings.get("total_ms", 0)
            winner_ms += detector.last_timings.get("winner_ms", 0)
            character_ms += detector.last_timings.get("character_ms", 0)
            obs.timestamp = second if second else .001
            previous = machine.generation
            ended = machine.observe(obs)
            if machine.generation != previous:
                detector.characters.reset()
                current = {"game": machine.generation, "start_second": round(second, 2),
                           "end_second": None, "end_confidence": 0.0,
                           "winner": None, "winner_confidence": 0.0, "signals": []}
                games.append(current)
            if ended and current:
                current["end_second"] = round(second, 2)
                current["end_confidence"] = machine.end_detection.confidence
                current["signals"] = machine.end_detection.evidence.copy()
            if current and machine.winner and current["end_second"] is not None:
                current["winner"] = machine.winner.value
                current["winner_confidence"] = machine.winner_confidence
                current["signals"] = (machine.end_detection.evidence +
                                      machine.winner_evidence)
            second += step
    finally:
        capture.release()
    output.parent.mkdir(parents=True, exist_ok=True)
    elapsed = max(.001, time.perf_counter() - wall_start)
    rss = process_rss_bytes()
    metrics = {"frames": frames, "analysis_fps": round(frames / elapsed, 2),
               "cpu_percent_one_core": round((time.process_time() - cpu_start) / elapsed * 100, 1),
               "ram_mb": round(rss / 1048576, 1) if rss is not None else None,
               "average_frame_ms": round(frame_ms / max(1, frames), 2),
               "average_winner_ms": round(winner_ms / max(1, frames), 2),
               "average_character_ms": round(character_ms / max(1, frames), 2)}
    output.write_text(json.dumps({"video": str(video), "start": start, "end": end,
                                  "step": step, "games": games, "metrics": metrics}, indent=2),
                      encoding="utf-8")
    with output.with_suffix(".csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=("game", "start_second", "end_second",
                                                  "end_confidence", "winner",
                                                  "winner_confidence", "signals"))
        writer.writeheader()
        writer.writerows(games)
    return games


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
    parser.add_argument("--start", type=_seconds, required=True)
    parser.add_argument("--end", type=_seconds, required=True)
    parser.add_argument("--step", type=float, default=5)
    parser.add_argument("--mode", choices=("characters", "games"), default="characters")
    parser.add_argument("--output", type=Path, default=Path("diagnostics/characters.csv"))
    args = parser.parse_args()
    if args.mode == "games":
        output = args.output if args.output.suffix == ".json" else args.output.with_suffix(".json")
        analyze_games(args.video, args.calibration, args.templates, args.start, args.end,
                      args.step, output)
    else:
        analyze(args.video, args.calibration, args.templates, args.start, args.end,
                args.step, args.output)
    print(args.output)


def _seconds(value: str) -> float:
    parts = value.split(":")
    if len(parts) == 1:
        return float(value)
    if len(parts) > 3:
        raise argparse.ArgumentTypeError("Use seconds or HH:MM:SS")
    result = 0.0
    for part in parts:
        result = result * 60 + float(part)
    return result


if __name__ == "__main__":
    main()
