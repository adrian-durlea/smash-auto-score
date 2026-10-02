import json

import pytest

from smash_auto_score.config import Settings
from smash_auto_score.controller import Controller
from smash_auto_score.domain import Observation, Side, Slot
from smash_auto_score.integrations import DemoTSHAdapter
from smash_auto_score.state import GamePhase, GameStateMachine
from smash_auto_score.store import Store


def test_game_starts_without_character_templates():
    machine = GameStateMachine()
    for second in (10.0, 10.2, 10.4):
        machine.observe(Observation(game_active=True, hud_visible=False, timestamp=second))
    assert machine.phase == GamePhase.ACTIVE


@pytest.mark.asyncio
async def test_game_event_ids_survive_controller_restart(tmp_path):
    path = str(tmp_path / "scores.db")
    tsh = DemoTSHAdapter()
    first = Controller(tsh, Store(path), Settings())
    first.armed = True
    active = Observation(game_active=True, tags={Slot.P1: "DjDC"})
    result = Observation(game_set=True, result_screen=True, winner=Slot.P1,
                         winner_confidence=.99, tags={Slot.P1: "DjDC"})
    for _ in range(3):
        await first.ingest(active)
    for _ in range(3):
        await first.ingest(result)
    assert (await first.refresh()).left_score == 1

    second = Controller(tsh, Store(path), Settings())
    second.armed = True
    for _ in range(3):
        await second.ingest(active)
    for _ in range(3):
        await second.ingest(result)
    assert (await second.refresh()).left_score == 2
    assert second.store.event("demo-1:1") is not None
    assert second.store.event("demo-1:2") is not None


def test_generation_migrates_existing_score_events(tmp_path):
    store = Store(str(tmp_path / "legacy.db"))
    assert store.start_score("demo-1:7", "demo-1", "left", 0, 0)
    assert store.next_game_generation("demo-1") == 8


@pytest.mark.asyncio
async def test_shadow_mode_logs_would_score_and_blocks_writes(tmp_path):
    ctl = Controller(DemoTSHAdapter(), Store(str(tmp_path / "events.db")), Settings())
    ctl.override_mapping(True)
    ctl.set_shadow_mode(True)
    for _ in range(3):
        await ctl.ingest(Observation(game_active=True))
    result = Observation(game_set=True, result_screen=True, winner=Slot.P2,
                         winner_confidence=.99)
    for _ in range(3):
        await ctl.ingest(result)
    assert (await ctl.refresh()).right_score == 0
    events = [json.loads(item["detail"]) for item in ctl.store.recent()
              if item["kind"] == "would_score"]
    assert len(events) == 1 and events[0]["side"] == "right"
    with pytest.raises(ValueError, match="Shadow mode"):
        await ctl.manual_score(Side.LEFT)


@pytest.mark.asyncio
async def test_external_score_change_requires_review(tmp_path):
    tsh = DemoTSHAdapter()
    ctl = Controller(tsh, Store(str(tmp_path / "events.db")), Settings())
    await ctl.refresh()
    ctl.armed = True
    await tsh.set_score(1, 0)
    assert (await ctl.refresh()).left_score == 1
    assert ctl.external_score_change is not None and not ctl.armed
    with pytest.raises(ValueError, match="external"):
        await ctl.manual_score(Side.LEFT)
    ctl.accept_external_score()
    assert ctl.external_score_change is None


@pytest.mark.asyncio
@pytest.mark.parametrize("applies", [True, False])
async def test_uncertain_write_readback_reconciliation(tmp_path, applies):
    class Flaky(DemoTSHAdapter):
        async def set_score(self, left, right):
            if applies:
                await super().set_score(left, right)
            raise TimeoutError("Ambiguous TSH response")

    ctl = Controller(Flaky(), Store(str(tmp_path / "events.db")), Settings())
    with pytest.raises(TimeoutError):
        await ctl.manual_score(Side.LEFT)
    result = await ctl.reconcile_uncertain_score()
    assert result["outcome"] == ("APPLIED" if applies else "NOT_APPLIED")
    assert not ctl.armed
