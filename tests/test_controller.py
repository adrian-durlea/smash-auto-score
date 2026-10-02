import pytest

from smash_auto_score.config import Settings
from smash_auto_score.controller import Controller
from smash_auto_score.domain import Observation, Side, Slot
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
