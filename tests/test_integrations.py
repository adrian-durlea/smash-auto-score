import numpy as np
import pytest
from fastapi.testclient import TestClient

from smash_auto_score.app import app
from smash_auto_score.config import Settings
from smash_auto_score.controller import Controller
from smash_auto_score.domain import Mode, Observation, PlayerIdentity, SetInfo, Slot
from smash_auto_score.integrations import DemoTSHAdapter, parse_tsh_scoreboard
from smash_auto_score.store import Store
from smash_auto_score.vision import ROI, Calibration, WinnerDetector
from smash_auto_score.worker import (
    activate_profile,
    calibration_profiles,
    load_calibration,
    save_calibration,
)


def test_tsh_5_scoreboard_shape():
    raw = {"best_of": 5, "team": {"1": {"score": 2, "color": "#ee3333",
           "player": {"1": {"name": "Snackz", "id": 123}}},
           "2": {"score": 1, "color": "#3366ee", "player": {"1": {"name": "Leo"}}}}}
    match = parse_tsh_scoreboard(raw, "456")
    assert (match.left.display_name, match.right.display_name) == ("Snackz", "Leo")
    assert (match.left_score, match.right_score) == (2, 1)
    assert match.best_of == 5


@pytest.mark.asyncio
async def test_next_set_ambiguity_and_assignment(tmp_path):
    tsh = DemoTSHAdapter()
    tsh.candidates = [SetInfo("2", PlayerIdentity("A"), PlayerIdentity("B")),
                      SetInfo("3", PlayerIdentity("C"), PlayerIdentity("D"))]
    ctl = Controller(tsh, Store(str(tmp_path / "sets.db")), Settings())
    ctl.set_mode = Mode.AUTO
    ctl.observation = Observation(tags={Slot.P1: "A", Slot.P2: "B"})
    candidate = await ctl.suggest_next_set()
    assert candidate is not None and candidate["set_id"] in {"2", "3"}
    assert tsh.current.set_id == "demo-1"
    candidate = await ctl.suggest_next_set({"3"})
    assert candidate is not None and candidate["set_id"] == "3"
    assert tsh.current.set_id == "demo-1"  # assignment alone remains below auto threshold


def test_dashboard_launches_and_manual_score():
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        status = client.get("/api/status").json()
        assert "set" in status and "video" in status
        assert client.post("/api/score/left").status_code == 200
        assert client.post("/api/undo").status_code == 200


def test_winner_detector_requires_explicit_slot_and_confidence():
    class FakeOCR:
        def __init__(self, text, confidence):
            self.text, self.confidence = text, confidence
        def read(self, crop):
            return self.text, self.confidence
    crop = np.zeros((20, 80, 3), np.uint8)
    assert WinnerDetector(FakeOCR("P2 WINS", .99)).detect(crop) == (Slot.P2, .99)
    assert WinnerDetector(FakeOCR("P1 WINS P2 WINS", .99)).detect(crop)[0] is None
    assert WinnerDetector(FakeOCR("P1 WINS", .8)).detect(crop)[0] is None


@pytest.mark.asyncio
async def test_tsh_disconnect_preserves_dashboard(tmp_path):
    class FailingTSH(DemoTSHAdapter):
        async def get_current_set(self):
            raise OSError("offline")
    ctl = Controller(FailingTSH(), Store(str(tmp_path / "offline.db")), Settings())
    status = await ctl.status()
    assert not status["connected"]
    assert status["error"] == "offline"


def test_named_calibration_profiles(tmp_path):
    path = tmp_path / "calibration.json"
    save_calibration(path, Calibration({"p1_tag": ROI(.1, .2, .3, .1)}))
    activate_profile(path, "Weekly-720p")
    assert load_calibration(path).rois == {}
    save_calibration(path, Calibration({"p2_hud": ROI(.5, .8, .2, .1)}))
    assert calibration_profiles(path) == ("Weekly-720p", ["Default", "Weekly-720p"])
    activate_profile(path, "Default")
    assert "p1_tag" in load_calibration(path).rois
