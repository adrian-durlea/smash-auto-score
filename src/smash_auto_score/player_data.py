"""On-demand identity enrichment. No unverified external scraping."""
from typing import Protocol

from .domain import PlayerIdentity
from .store import Store


class PlayerDataProvider(Protocol):
    async def enrich(self, player: PlayerIdentity) -> PlayerIdentity: ...


class LocalPlayerDataProvider:
    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def key(player: PlayerIdentity) -> str:
        return f"startgg:{player.startgg_id}" if player.startgg_id else f"name:{player.display_name.casefold()}"

    async def enrich(self, player: PlayerIdentity) -> PlayerIdentity:
        raw = self.store.cache_get(self.key(player))
        if raw:
            player.aliases = sorted(set(player.aliases) | set(raw.get("aliases", [])))
            player.character_distribution.update(raw.get("character_distribution", {}))
            player.supermajor_id = raw.get("supermajor_id") or player.supermajor_id
        return player

    def save(self, player: PlayerIdentity) -> None:
        from dataclasses import asdict
        self.store.cache_put(self.key(player), asdict(player), ttl=365 * 86400)
        self.store.log("player_override", {"player": player.display_name,
                       "startgg_id": player.startgg_id, "aliases": player.aliases})
