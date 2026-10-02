import asyncio
import base64
import sys
from pathlib import Path
from types import SimpleNamespace

import cv2
import httpx
import numpy as np
import pytest

from smash_auto_score.config import Settings
from smash_auto_score.controller import Controller
from smash_auto_score.domain import Side
from smash_auto_score.integrations import DemoTSHAdapter, OBSVideoSource, TSHWebAdapter
from smash_auto_score.store import Store
from smash_auto_score.worker import VideoWorker


@pytest.mark.asyncio
async def test_tsh_routes_and_readback_with_mock_transport():
    score = [0, 0]
    paths = []

    def respond(request):
        path = request.url.path
        paths.append(path)
        if path.endswith("-get"):
            return httpx.Response(200, json={"team": [
                {"score": score[0], "player": [{"name": "A"}]},
                {"score": score[1], "player": [{"name": "B"}]}]})
        if path.endswith("-get-set"):
            return httpx.Response(200, text="42")
        if path.endswith("-team1-scoreup"):
            score[0] += 1
            return httpx.Response(200, text="OK")
        if path.endswith("-team1-scoredown"):
            score[0] -= 1
            return httpx.Response(200, text="OK")
        if path == "/get-sets":
            return httpx.Response(200, json=[{"id": 43, "players": ["A", "C"]}])
        return httpx.Response(200, text="OK")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond),
                                 base_url="http://test") as client:
        tsh = TSHWebAdapter("http://test")
        await tsh.client.aclose()
        tsh.client = client
        assert (await tsh.get_current_set()).set_id == "42"
        assert len(await tsh.get_possible_sets()) == 1
        await tsh.set_score(1, 0)
        assert await tsh.get_scores() == (1, 0)
        await tsh.set_score(0, 0)
        assert await tsh.get_scores() == (0, 0)
        await tsh.swap_players()
        await tsh.load_set("43")
    assert "/scoreboard1-team1-scoreup" in paths
    assert "/scoreboard1-team1-scoredown" in paths
    assert "/scoreboard1-swap-teams" in paths
    assert "/scoreboard1-load-set" in paths


@pytest.mark.asyncio
async def test_obs_screenshot_and_disconnect(monkeypatch):
    image = np.full((10, 10, 3), 120, np.uint8)
    _, encoded = cv2.imencode(".png", image)
    data = "data:image/png;base64," + base64.b64encode(encoded).decode()

    class Client:
        def __init__(self, **_kwargs):
            pass

        def get_source_screenshot(self, source, *_args):
            if source == "missing":
                raise OSError("OBS source missing")
            return SimpleNamespace(image_data=data)

        def disconnect(self):
            pass

    monkeypatch.setitem(sys.modules, "obsws_python", SimpleNamespace(ReqClient=Client))
    source = OBSVideoSource("localhost", 4455, "", "game")
    assert (await source.frame()).shape == (10, 10, 3)
    assert source.last_roundtrip_ms >= 0
    source.source = "missing"
    with pytest.raises(OSError):
        await source.frame()


@pytest.mark.asyncio
async def test_video_failure_disarms_and_is_cancelable(tmp_path):
    class Source:
        async def frame(self):
            raise OSError("OBS disconnected")

    ctl = Controller(DemoTSHAdapter(), Store(str(tmp_path / "events.db")), Settings())
    ctl.armed = True
    worker = VideoWorker(Source(), ctl, Path(tmp_path / "calibration.json"))
    task = asyncio.create_task(worker.run())
    await asyncio.sleep(.05)
    assert not worker.connected and not ctl.armed
    assert "OBS disconnected" in worker.error
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_tsh_recovery_stays_disarmed_and_uncertain_write_is_not_retried(tmp_path):
    class FlakyTSH(DemoTSHAdapter):
        offline = False
        ambiguous_write = False
        writes = 0

        async def get_current_set(self):
            if self.offline:
                raise httpx.ConnectError("offline")
            return await super().get_current_set()

        async def set_score(self, left, right):
            self.writes += 1
            await super().set_score(left, right)
            if self.ambiguous_write:
                raise httpx.ReadTimeout("response lost after write")

    tsh = FlakyTSH()
    ctl = Controller(tsh, Store(str(tmp_path / "events.db")), Settings())
    ctl.armed = True
    tsh.offline = True
    assert not (await ctl.status())["connected"]
    tsh.offline = False
    assert (await ctl.status())["connected"]
    assert not ctl.armed
    ctl.armed = True
    tsh.ambiguous_write = True
    with pytest.raises(httpx.ReadTimeout):
        await ctl.manual_score(Side.LEFT)
    assert not ctl.armed and tsh.writes == 1
    assert (await tsh.get_current_set()).left_score == 1
    assert ctl.store.last_score() is None  # Persisted as uncertain, not applied.
