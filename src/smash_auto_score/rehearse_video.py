"""Run recorded gameplay through vision, state, mapping, and a disposable TSH."""

import argparse
import asyncio
import json
from itertools import pairwise
from pathlib import Path

import cv2

from .analyze_video import _NullOCR, _seconds
from .characters import TemplateCharacterDetector
from .config import Settings
from .controller import Controller
from .integrations import DemoTSHAdapter
from .store import Store
from .vision import FrameDetector
from .worker import load_calibration


def _window(value: str) -> tuple[float, float]:
    try:
        start, end = (_seconds(part) for part in value.split("-", 1))
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError("Use START-END, e.g. 14:50-15:15") from exc
    if start < 0 or end <= start:
        raise argparse.ArgumentTypeError("Window end must follow its start")
    return start, end


async def rehearse(video: Path, windows: list[tuple[float, float]], *, step: float,
                   calibration: Path, templates: Path, p1_is_left: bool | None,
                   output: Path) -> dict:
    """No network or live scoreboard writes; only the in-memory TSH adapter is used."""
    if step <= 0:
        raise ValueError("Step must be positive")
    if not windows or any(start < 0 or end <= start for start, end in windows):
        raise ValueError("At least one valid recording window is required")
    if any(later[0] <= earlier[1] for earlier, later in pairwise(windows)):
        raise ValueError("Recording windows must be in order and must not overlap")
    controller = Controller(DemoTSHAdapter(), Store(":memory:"),
                            Settings(demo=True, supermajor_enabled=False))
    if p1_is_left is not None:
        controller.override_mapping(p1_is_left)
        controller.armed = True
    detector = FrameDetector(load_calibration(calibration), _NullOCR(),
                             TemplateCharacterDetector(templates))
    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        raise ValueError(f"Cannot open {video}")
    sampled = 0
    try:
        for start, end in windows:
            second = start
            while second <= end:
                capture.set(cv2.CAP_PROP_POS_MSEC, second * 1000)
                ok, frame = capture.read()
                if not ok:
                    raise ValueError(f"Cannot read {video} at {second:.1f} seconds")
                obs = detector.observe(frame, do_ocr=False, do_color=False)
                obs.timestamp = second if second else .001
                previous = controller.machine.generation
                await controller.ingest(obs)
                if controller.machine.generation != previous:
                    detector.characters.reset()
                sampled += 1
                second += step
    finally:
        capture.release()
    match = await controller.refresh()
    events = [{"kind": event["kind"], "detail": json.loads(event["detail"])}
              for event in reversed(controller.store.recent(1000))
              if event["kind"] in {"game_end", "score_applied", "confirmation_required",
                                   "score_uncertain"}]
    report = {"source": str(video), "windows": windows, "step": step,
              "sampled_frames": sampled, "mapping_override": p1_is_left,
              "score": {"left": match.left_score, "right": match.right_score},
              "phase": controller.machine.phase.value, "events": events}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--window", type=_window, action="append", required=True,
                        help="Recording time range START-END; repeat for consecutive games")
    parser.add_argument("--step", type=float, default=.2)
    parser.add_argument("--calibration", type=Path, default=Path("config/calibration.json"))
    parser.add_argument("--templates", type=Path, default=Path("config/character_templates"))
    parser.add_argument("--output", type=Path, default=Path("diagnostics/rehearsal.json"))
    mapping = parser.add_mutually_exclusive_group()
    mapping.add_argument("--p1-is-left", action="store_true")
    mapping.add_argument("--p1-is-right", action="store_true")
    args = parser.parse_args()
    p1_is_left = True if args.p1_is_left else False if args.p1_is_right else None
    report = asyncio.run(rehearse(args.video, args.window, step=args.step,
                                  calibration=args.calibration, templates=args.templates,
                                  p1_is_left=p1_is_left, output=args.output))
    print(json.dumps({"score": report["score"], "phase": report["phase"],
                      "events": report["events"]}, indent=2))


if __name__ == "__main__":
    main()
