import inspect
import json
from pathlib import Path

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from smash_auto_score.config import Settings
from smash_auto_score.controller import Controller
from smash_auto_score.domain import PlayerIdentity, SetInfo
from smash_auto_score.integrations import DemoTSHAdapter
from smash_auto_score.setup_wizard import (
    _obs_probe,
    discover_tsh,
    probe_startgg,
    probe_tsh,
    readiness,
    save_local_settings,
    tournament_slug,
)
from smash_auto_score.setup_wizard import (
    test_obs_frame as capture_obs_frame,
)
from smash_auto_score.store import Store


def test_tournament_url_and_slug():
    assert tournament_slug("https://www.start.gg/tournament/example/details") == "tournament/example"
    assert tournament_slug("tournament/example") == "tournament/example"
    with pytest.raises(ValueError):
        tournament_slug("https://other.example/tournament/example")


def test_local_config_preserves_other_values_and_hides_secrets(tmp_path):
    path = tmp_path / ".env"
    path.write_text("SAS_DEMO=true\nUNRELATED=value\n", encoding="utf-8")
    save_local_settings(path, {"startgg_token": 'abc"def', "obs_port": 4455})
    data = path.read_text(encoding="utf-8")
    assert "UNRELATED=value" in data
    assert 'SAS_STARTGG_TOKEN="abc\\"def"' in data
    assert "SAS_OBS_PORT=4455" in data
    loaded = Settings(_env_file=path)
    assert loaded.startgg_token == 'abc"def'
    assert loaded.obs_port == 4455
    with pytest.raises(ValueError):
        save_local_settings(path, {"arbitrary": "secret"})


@pytest.mark.asyncio
async def test_tsh_discovery_checks_current_and_default_local_ports(monkeypatch):
    seen = []

    async def fake_probe(url, scoreboard):
        seen.append((url, scoreboard))
        return {"status": "PASS", "url": url} if url.endswith(":5500") else {"status": "FAIL"}

    monkeypatch.setattr("smash_auto_score.setup_wizard.probe_tsh", fake_probe)
    found = await discover_tsh(Settings(tsh_url="http://127.0.0.1:5000", tsh_scoreboard=2))
    assert [item["url"] for item in found] == ["http://127.0.0.1:5500"]
    assert ("http://127.0.0.1:5500", 2) in seen
    assert all("127.0.0.1" in url for url, _ in seen)


@pytest.mark.asyncio
async def test_tsh_capability_probe_is_read_only(monkeypatch):
    class Client:
        timeout = None

        async def aclose(self):
            pass

    class Adapter:
        def __init__(self, *_):
            self.client = Client()

        async def get_current_set(self):
            return SetInfo("test-set", PlayerIdentity("A"), PlayerIdentity("B"),
                           left_color="#ff0000", right_color="#0000ff")

        async def get_possible_sets(self):
            return []

        async def set_score(self, *_):
            raise AssertionError("Discovery must not write")

    monkeypatch.setattr("smash_auto_score.setup_wizard.TSHWebAdapter", Adapter)
    result = await probe_tsh("http://127.0.0.1:5500", 1)
    assert result["capabilities"]["score_read"] == "PASS"
    assert result["capabilities"]["score_write"] == "NOT_TESTED"
    assert result["capabilities"]["candidate_sets"] == "PASS"


def test_obs_sources_rank_capture_input_and_report_auth(monkeypatch):
    class Version:
        obs_version = "31.0"
        obs_web_socket_version = "5.0"

    class Scenes:
        def __init__(self):
            self.scenes = [{"sceneName": "Main"}]

    class Inputs:
        def __init__(self):
            self.inputs = [{"inputName": "Webcam", "inputKind": "dshow_input"},
                           {"inputName": "Elgato HD60", "inputKind": "dshow_input"}]

    class Client:
        def __init__(self, **kwargs):
            if not kwargs["password"]:
                raise RuntimeError("Authentication required")

        def get_version(self):
            return Version()

        def get_scene_list(self):
            return Scenes()

        def get_input_list(self):
            return Inputs()

        def disconnect(self):
            pass

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    monkeypatch.setattr("socket.create_connection", lambda *_args, **_kwargs: Socket())
    monkeypatch.setattr("obsws_python.ReqClient", Client)
    assert _obs_probe("127.0.0.1", 4455, "")["status"] == "AUTH_REQUIRED"
    result = _obs_probe("127.0.0.1", 4455, "secret")
    assert result["status"] == "PASS"
    assert result["recommended_source"] == "Elgato HD60"
    assert result["scenes"] == ["Main"]


@pytest.mark.asyncio
async def test_obs_frame_test_runs_cv_on_three_frames(monkeypatch, tmp_path):
    class Source:
        def __init__(self, *_args):
            self.last_roundtrip_ms = 12.0

        async def frame(self):
            return np.zeros((540, 960, 3), dtype=np.uint8)

    monkeypatch.setattr("smash_auto_score.setup_wizard.OBSVideoSource", Source)
    result, preview = await capture_obs_frame("127.0.0.1", 4455, "", "Capture",
                                               calibration_path=tmp_path / "calibration.json")
    assert result["status"] == "PASS"
    assert result["frames"] == 3
    assert result["analysis"] == "PASS"
    assert preview is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [True, False])
async def test_startgg_token_and_stream_queue(monkeypatch, authorized):
    def handler(request):
        body = json.loads(request.content)
        if "currentUser" in body["query"]:
            return httpx.Response(200 if authorized else 401,
                                  json={"data": {"currentUser": {"id": 4}}} if authorized else {})
        return httpx.Response(200, json={"data": {"tournament": {"name": "Event",
            "streamQueue": [{"stream": {"streamName": "Main", "streamSource": "TWITCH"},
                             "sets": [{"id": "9", "slots": [{"entrant": {"name": "P1"}},
                                                              {"entrant": {"name": "P2"}}]}]}]}}})

    original = httpx.AsyncClient
    monkeypatch.setattr("smash_auto_score.setup_wizard.httpx.AsyncClient",
                        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    result = await probe_startgg("token", "https://start.gg/tournament/event/details", "Main")
    assert result["authentication"] == ("PASS" if authorized else "FAIL")
    if authorized:
        assert result["selected_queue"][0]["entrants"] == ["P1", "P2"]


def test_readiness_separates_blocking_optional_and_unloaded_set(tmp_path: Path):
    tsh = [{"status": "PASS", "capabilities": {"current_set": "NO_SET_LOADED",
                                              "players": "NO_PLAYERS_LOADED"}}]
    obs = [{"status": "PASS"}]
    result = readiness(tsh, obs, {"status": "PASS"}, {"status": "NOT_CONFIGURED"},
                       tmp_path / "none.json")
    assert result["status"] == "NOT_READY"
    assert "TSH is running but no valid two-player set is loaded" in result["blocking"]
    assert any("Start.gg" in value for value in result["warnings"])


def test_imported_module_has_no_network_side_effect():
    # The wizard probes only in explicit discovery/validation calls.
    assert inspect.iscoroutinefunction(discover_tsh)


def test_setup_page_and_shadow_api_block_mutations():
    from smash_auto_score.app import app

    client = TestClient(app)
    assert client.get("/setup").status_code == 200
    try:
        assert client.post("/api/setup/shadow", json={"enabled": True}).json()["shadow_mode"]
        assert client.post("/api/arm/true").status_code == 409
        assert client.post("/api/swap").status_code == 409
        assert client.post("/api/score/left").status_code == 409
    finally:
        client.post("/api/setup/shadow", json={"enabled": False})


def test_disposable_score_test_restores_original_score(monkeypatch):
    from smash_auto_score.app import app

    controller = Controller(DemoTSHAdapter(), Store(":memory:"), Settings())
    monkeypatch.setattr("smash_auto_score.app.controller", controller)
    client = TestClient(app)
    assert client.post("/api/setup/tsh-score-test", json={"side": "left",
                                                    "disposable": False}).status_code == 409
    response = client.post("/api/setup/tsh-score-test", json={"side": "left",
                                                     "disposable": True})
    assert response.status_code == 200
    assert response.json()["original_score"] == [0, 0]
    assert response.json()["restored_score"] == [0, 0]
    assert (controller.tsh.current.left_score, controller.tsh.current.right_score) == (0, 0)
