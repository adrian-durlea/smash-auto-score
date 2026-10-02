import json

import httpx
import numpy as np
import pytest

from smash_auto_score.config import Settings
from smash_auto_score.controller import Controller
from smash_auto_score.diagnostics import WinnerDiagnostics
from smash_auto_score.domain import Observation, Side, Slot
from smash_auto_score.integrations import DemoTSHAdapter
from smash_auto_score.state import GamePhase, GameStateMachine
from smash_auto_score.store import Store
from smash_auto_score.telemetry import ProcessSampler
from smash_auto_score.vision import Calibration, WinnerDetector


class NullOCR:
    def read(self, _crop):
        return "", 0.0


def synthetic_result(slot: Slot, *, placement: bool = True, opposite: bool = True):
    frame = np.full((540, 960, 3), 80, np.uint8)
    rois = WinnerDetector.DEFAULT_ROIS
    def fill(name, color):
        roi = rois[name]
        crop = roi.crop(frame)
        crop[:] = color
    if placement:
        area = rois["placement"].crop(frame)
        area[:, :area.shape[1] // 4] = (0, 210, 255)
    fill("winner_badge", (0, 0, 225) if slot == Slot.P1 else (225, 70, 0))
    if opposite:
        fill("loser_badge", (225, 70, 0) if slot == Slot.P1 else (0, 0, 225))
    return frame


@pytest.mark.parametrize("slot", [Slot.P1, Slot.P2])
def test_result_layout_recognizes_both_slots(slot):
    found, winner, confidence, evidence = WinnerDetector(NullOCR()).detect_visual(
        synthetic_result(slot), Calibration())
    assert found and winner == slot and confidence >= .97
    assert len(evidence) == 3


def test_gold_stage_or_same_color_badges_do_not_confirm_result():
    detector = WinnerDetector(NullOCR())
    gold_only = np.full((540, 960, 3), 80, np.uint8)
    WinnerDetector.DEFAULT_ROIS["placement"].crop(gold_only)[:] = (0, 210, 255)
    assert detector.detect_visual(gold_only, Calibration())[0] is False
    found, winner, confidence, _ = detector.detect_visual(
        synthetic_result(Slot.P1, opposite=False), Calibration())
    assert found and winner is None and confidence == 0


def test_temporal_end_winner_unknown_and_new_game_lockout():
    machine = GameStateMachine()
    machine.new_game_confirmation_frames = 3
    machine.post_game_lockout_seconds = 5
    for t in (10.0, 10.2, 10.4):
        machine.observe(Observation(game_active=True, hud_visible=True, timestamp=t))
    assert machine.phase == GamePhase.ACTIVE and machine.generation == 1
    result = Observation(result_screen=True, result_confidence=.98, winner=Slot.P2,
                         winner_confidence=.98, timestamp=20)
    assert machine.observe(result) is False
    assert machine.winner is None
    assert machine.observe(Observation(timestamp=20.1)) is False
    assert machine.phase == GamePhase.END_CANDIDATE
    assert machine.observe(result) is False
    assert machine.observe(result) is True
    assert machine.winner == Slot.P2
    for _ in range(3):
        assert machine.observe(Observation(timestamp=20.5)) is False
    assert machine.phase == GamePhase.POST_GAME
    for t in (21.0, 21.2, 21.4):
        machine.observe(Observation(game_active=True, hud_visible=True, timestamp=t))
    assert machine.generation == 1
    for t in (26.0, 26.2, 26.4):
        machine.observe(Observation(game_active=True, hud_visible=True, timestamp=t))
    assert machine.generation == 2 and machine.winner is None
    machine.reset()
    for t in (30.0, 30.2, 30.4):
        machine.observe(Observation(game_active=True, hud_visible=True, timestamp=t))
    assert machine.generation == 3


@pytest.mark.asyncio
async def test_persistent_results_score_once_and_manual_correction(tmp_path):
    controller = Controller(DemoTSHAdapter(), Store(str(tmp_path / "events.db")), Settings())
    controller.armed = True
    for _ in range(3):
        await controller.ingest(Observation(game_active=True, tags={Slot.P1: "DjDC"}))
    result = Observation(result_screen=True, result_confidence=.98, winner=Slot.P1,
                         winner_confidence=.98, tags={Slot.P1: "DjDC"})
    for _ in range(20):
        await controller.ingest(result)
    assert (await controller.refresh()).left_score == 1
    applied = [e for e in controller.store.recent() if e["kind"] == "score_applied"]
    assert len(applied) == 1
    audit = json.loads(applied[0]["detail"])
    assert audit["before"] == [0, 0]
    assert audit["after"] == [1, 0]
    await controller.manual_score(Side.RIGHT)
    assert len([e for e in controller.store.recent() if e["kind"] == "winner_prediction_error"]) == 1


@pytest.mark.asyncio
async def test_tsh_disconnect_disarms(tmp_path):
    class FailingTSH(DemoTSHAdapter):
        async def get_current_set(self):
            raise httpx.ConnectError("TSH closed")

    controller = Controller(FailingTSH(), Store(str(tmp_path / "events.db")), Settings())
    controller.armed = True
    status = await controller.status()
    assert not status["connected"] and not status["armed"]
    controller.armed = True
    with pytest.raises(httpx.ConnectError):
        await controller.ingest(Observation())
    assert not controller.armed


def test_local_winner_diagnostics_and_process_metrics(tmp_path):
    controller = Controller(DemoTSHAdapter(), Store(str(tmp_path / "events.db")), Settings())
    recorder = WinnerDiagnostics(tmp_path / "winner", controller)
    saved = recorder.record("manual-p2", synthetic_result(Slot.P2), actual=Slot.P2,
                            actual_side=Side.RIGHT)
    assert saved is not None and saved.exists()
    assert saved.with_suffix(".json").exists()
    assert (saved.parent / f"{saved.stem}-winner_badge.jpg").exists()
    metadata = json.loads(saved.with_suffix(".json").read_text(encoding="utf-8"))
    assert metadata["actual_winner"] == "P2"
    assert ProcessSampler().sample()["ram_mb"] is not None
