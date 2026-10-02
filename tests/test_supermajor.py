import json

import httpx
import pytest

from smash_auto_score.domain import PlayerIdentity
from smash_auto_score.store import Store
from smash_auto_score.supermajor import SupermajorPlayerDataProvider, parse_supermajor_page


def page(player_id="S42", name="Test Player"):
    initial = {"characters": {"contexts": {
        "All Time": {"characters": {"1": {"num_games": 80}, "2": {"num_games": 20}}},
        "Last 6 Mo": {"characters": {"1": {"num_games": 3}, "2": {"num_games": 1}}}}}}
    lookup = {"1": {"sport": "ultimate", "name": "Fox"},
              "2": {"sport": "ultimate", "name": "Wolf"}}
    payload = (f'"playerId":"{player_id}","initialData":{json.dumps(initial)},'
               f'"character_info_lookup":{json.dumps(lookup)}')
    return (f"<title>Supermajor - {name}</title><script>"
            f"self.__next_f.push({json.dumps([1, payload])})</script>")


def test_parser_uses_reported_games_and_rejects_wrong_id():
    result = parse_supermajor_page(page(), "S42")
    assert result["distribution_context"] == "All Time"
    assert result["character_distribution"] == {"fox": .8, "wolf": .2}
    with pytest.raises(ValueError):
        parse_supermajor_page(page(), "S43")


@pytest.mark.asyncio
async def test_lookup_cache_ambiguity_and_manual_override(tmp_path):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, text=page(name="Different Player"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = SupermajorPlayerDataProvider(Store(str(tmp_path / "test.db")),
                                               enabled=True, client=client)
        player = PlayerIdentity("Test Player", supermajor_id="S42",
                                character_distribution={"fox": .9})
        assert (await provider.lookup(player))["status"] == "MATCHED"
        assert player.character_distribution == {"fox": .9, "wolf": .2}
        assert (await provider.lookup(player))["cached"]
        assert len(requests) == 1
        assert requests[0].url.params["id"] == "S42"

        unconfirmed = PlayerIdentity("Unrelated", startgg_id="42")
        assert (await provider.lookup(unconfirmed))["status"] == "NOT_LOOKED_UP"
        assert len(requests) == 1


@pytest.mark.asyncio
async def test_lookup_http_failure_is_fail_closed(tmp_path):
    async def failure(_request):
        raise httpx.ConnectError("offline")

    async with httpx.AsyncClient(transport=httpx.MockTransport(failure)) as client:
        provider = SupermajorPlayerDataProvider(Store(str(tmp_path / "test.db")),
                                               enabled=True, client=client)
        player = PlayerIdentity("Test Player", supermajor_id="S42")
        assert (await provider.lookup(player))["status"] == "ERROR"
        assert player.character_distribution == {}
