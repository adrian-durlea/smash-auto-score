import time

import pytest

from smash_auto_score.config import Settings
from smash_auto_score.controller import Controller
from smash_auto_score.domain import Mode, Observation, Side, Slot
from smash_auto_score.integrations import DemoTSHAdapter
from smash_auto_score.store import Store


@pytest.fixture
def controller(tmp_path):
    return Controller(DemoTSHAdapter(), Store(str(tmp_path / "test.db")), Settings())


@pytest.mark.asyncio
async def test_manual_score_undo_and_set_complete(controller):
    assert await controller.manual_score(Side.LEFT)
    assert (await controller.refresh()).left_score == 1
    assert await controller.undo()
    assert (await controller.refresh()).left_score == 0
    await controller.manual_score(Side.LEFT)
    await controller.manual_score(Side.LEFT)
    assert (await controller.refresh()).complete
    with pytest.raises(ValueError):
        await controller.manual_score(Side.RIGHT)


@pytest.mark.asyncio
async def test_duplicate_game_end_and_low_confidence(controller):
    controller.armed = True
    active = Observation(game_active=True, tags={Slot.P1: "DjDC"})
    for _ in range(3):
        await controller.ingest(active)
    end = Observation(game_set=True, result_screen=True, winner=Slot.P1,
                      winner_confidence=.99, tags={Slot.P1: "DjDC"})
    for _ in range(10):
        await controller.ingest(end)
    assert (await controller.refresh()).left_score == 1
    assert len([e for e in controller.store.recent() if e["kind"] == "score_applied"]) == 1


@pytest.mark.asyncio
async def test_unknown_winner_cannot_auto_score(controller):
    controller.armed = True
    for _ in range(3):
        await controller.ingest(Observation(game_active=True, tags={Slot.P1: "DjDC"}))
    for _ in range(3):
        await controller.ingest(Observation(game_set=True, result_screen=True, tags={Slot.P1: "DjDC"}))
    assert (await controller.refresh()).left_score == 0


@pytest.mark.asyncio
async def test_sparse_ocr_samples_confirm_end(controller):
    controller.armed = True
    for _ in range(3):
        await controller.ingest(Observation(game_active=True, tags={Slot.P1: "DjDC"}))
    sampled = Observation(game_set=True, result_screen=True, winner=Slot.P1,
                          winner_confidence=.99, tags={Slot.P1: "DjDC"})
    for _ in range(3):
        await controller.ingest(sampled)
        for _ in range(4):
            await controller.ingest(Observation(game_set=None, result_screen=None))
    assert (await controller.refresh()).left_score == 1


@pytest.mark.asyncio
async def test_replay_game_requires_new_active_generation(controller):
    controller.armed = True
    end = Observation(game_set=True, result_screen=True, winner=Slot.P1,
                      winner_confidence=.99, tags={Slot.P1: "DjDC"})
    for _ in range(3):
        await controller.ingest(Observation(game_active=True, tags={Slot.P1: "DjDC"}))
    for _ in range(3):
        await controller.ingest(end)
    for _ in range(3):
        await controller.ingest(Observation())
    for _ in range(3):
        await controller.ingest(Observation(game_active=True, tags={Slot.P1: "DjDC"}))
    for _ in range(3):
        await controller.ingest(end)
    assert (await controller.refresh()).left_score == 2


@pytest.mark.asyncio
async def test_swap_requires_independent_evidence(controller):
    controller.swap_mode = Mode.AUTO
    obs = Observation(tags={Slot.P1: "Snackz", Slot.P2: "DjDC"},
                      colors={Slot.P1: "#3075ef", Slot.P2: "#e33737"})
    await controller.ingest(obs)
    match = await controller.refresh()
    assert match.left.display_name == "Snackz"
    assert controller.auto_swapped_set_id == "demo-1"


@pytest.mark.asyncio
async def test_auto_next_set_waits_for_post_set_delay(controller):
    await controller.manual_score(Side.LEFT)
    await controller.manual_score(Side.LEFT)
    controller.set_mode = Mode.AUTO
    controller.observation = Observation(tags={Slot.P1: "MkLeo", Slot.P2: "Dabuz"},
                                         game_active=True, timestamp=time.time())
    await controller.suggest_next_set({"demo-2"})
    assert (await controller.refresh()).set_id == "demo-1"
    assert controller.set_complete_at is not None
    controller.set_complete_at -= 20
    controller.observation.timestamp = time.time()
    await controller.suggest_next_set({"demo-2"})
    assert (await controller.refresh()).set_id == "demo-2"
