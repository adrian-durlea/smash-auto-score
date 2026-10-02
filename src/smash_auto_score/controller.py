import asyncio
import logging
import time
from dataclasses import asdict

from .config import Settings
from .domain import MappingDecision, Mode, Observation, SetInfo, Side, Slot
from .identity import map_players, tag_score
from .integrations import TSHAdapter
from .player_data import LocalPlayerDataProvider
from .state import GamePhase, GameStateMachine
from .store import Store
from .supermajor import SupermajorPlayerDataProvider

log = logging.getLogger(__name__)


class Controller:
    def __init__(self, tsh: TSHAdapter, store: Store, settings: Settings):
        self.tsh, self.store, self.settings = tsh, store, settings
        self.machine = GameStateMachine()
        self.machine.end_confirmation_frames = settings.game_end_confirmation_frames
        self.machine.winner_confirmation_frames = settings.winner_confirmation_frames
        self.machine.new_game_confirmation_frames = settings.new_game_confirmation_frames
        self.machine.post_game_lockout_seconds = settings.post_game_lockout_seconds
        self.armed = False
        self.set_mode = Mode.SUGGEST
        self.swap_mode = Mode.SUGGEST
        self.previous_mapping: bool | None = None
        self.manual_mapping: bool | None = None
        self.match: SetInfo | None = None
        self.mapping = MappingDecision(None, 0, [])
        self.observation = Observation()
        self.next_candidate: dict | None = None
        self.error: str | None = None
        self.lock = asyncio.Lock()
        self.player_data = LocalPlayerDataProvider(store)
        self.supermajor = SupermajorPlayerDataProvider(
            store, enabled=settings.supermajor_enabled,
            cache_ttl_days=settings.supermajor_cache_ttl_days,
            timeout_seconds=settings.supermajor_timeout_seconds,
            min_games=settings.supermajor_min_games)
        self.set_complete_at: float | None = None
        self.next_dismissed = False
        self.auto_swapped_set_id: str | None = None
        self.handled_end_events: set[str] = set()
        self.last_tsh_ms = 0.0
        self.last_state_ms = 0.0

    async def refresh(self) -> SetInfo:
        started = time.perf_counter()
        match = await self.tsh.get_current_set()
        self.last_tsh_ms = (time.perf_counter() - started) * 1000
        await self.player_data.enrich(match.left)
        await self.player_data.enrich(match.right)
        await self.supermajor.enrich(match.left)
        await self.supermajor.enrich(match.right)
        await self.player_data.enrich(match.left)
        await self.player_data.enrich(match.right)
        if self.match and match.set_id != self.match.set_id:
            self.machine.reset()
            self.previous_mapping = self.manual_mapping = None
            self.next_candidate = None
            self.set_complete_at = None
            self.next_dismissed = False
            self.auto_swapped_set_id = None
            self.handled_end_events.clear()
            self.store.log("set_changed", {"set_id": match.set_id})
        if match.complete and self.set_complete_at is None:
            self.set_complete_at = time.time()
        elif not match.complete:
            self.set_complete_at = None
        self.match = match
        return match

    async def status(self) -> dict:
        try:
            await self.refresh()
            self.error = None
        except Exception as exc:  # noqa: BLE001 - status must survive adapter failures
            self.error = str(exc)
            self.armed = False
            log.warning("TSH read failed: %s", exc)
        ready = (self.armed and self.error is None and self.machine.phase == GamePhase.RESULT
                 and self.machine.end_detection.confidence >=
                 self.settings.game_end_confidence_threshold
                 and self.machine.winner is not None
                 and self.machine.winner_confidence >= self.settings.min_winner_confidence
                 and self.mapping.p1_is_left is not None
                 and self.mapping.confidence >= self.settings.min_mapping_confidence)
        mapped_competitor = None
        if self.match and self.machine.winner and self.mapping.p1_is_left is not None:
            left_won = (self.machine.winner == Slot.P1) == self.mapping.p1_is_left
            mapped_competitor = (self.match.left if left_won else self.match.right).display_name
        return {"connected": self.error is None, "error": self.error,
                "set": asdict(self.match) if self.match else None,
                "phase": self.machine.phase.value, "generation": self.machine.generation,
                "game_end": asdict(self.machine.end_detection),
                "winner": {"slot": self.machine.winner.value if self.machine.winner else None,
                           "confidence": self.machine.winner_confidence,
                           "evidence": self.machine.winner_evidence,
                           "mapped_competitor": mapped_competitor},
                "auto_score_ready": ready,
                "armed": self.armed, "set_mode": self.set_mode.value,
                "swap_mode": self.swap_mode.value,
                "swap_suggested": self.mapping.p1_is_left is False and
                 self.mapping.confidence >= self.settings.min_mapping_confidence and not self.mapping.conflict,
                "mapping": asdict(self.mapping), "observation": asdict(self.observation),
                "next_candidate": self.next_candidate, "events": self.store.recent(),
                "supermajor": {"left": self.supermajor.status(self.match.left),
                               "right": self.supermajor.status(self.match.right)} if self.match else {}}

    async def ingest(self, obs: Observation) -> None:
        async with self.lock:
            try:
                match = await self.refresh()
            except Exception:
                self.armed = False
                raise
            self.observation = obs
            self.mapping = map_players(match, obs, self.previous_mapping)
            if self.manual_mapping is not None:
                self.mapping = MappingDecision(self.manual_mapping, 1.0, self.mapping.evidence)
            independent = {e.source for e in self.mapping.evidence
                           if not e.p1_is_left and e.confidence >= .9 and e.source != "previous_mapping"}
            if (self.swap_mode == Mode.AUTO and self.mapping.p1_is_left is False
                    and self.mapping.confidence >= .99 and not self.mapping.conflict
                    and len(independent) >= 2 and self.manual_mapping is None
                    and match.left_score == match.right_score == 0
                    and self.auto_swapped_set_id != match.set_id):
                left_name, right_name = match.left.display_name, match.right.display_name
                await self.tsh.swap_players()
                check = await self.tsh.get_current_set()
                if (check.set_id != match.set_id or check.left.display_name != right_name
                        or check.right.display_name != left_name):
                    raise RuntimeError("TSH player swap could not be verified")
                self.auto_swapped_set_id = match.set_id
                self.previous_mapping = True
                self.mapping = MappingDecision(True, self.mapping.confidence, self.mapping.evidence)
                self.store.log("auto_players_swapped", {"set_id": match.set_id})
            state_started = time.perf_counter()
            confirmed_end = self.machine.observe(obs)
            self.last_state_ms = (time.perf_counter() - state_started) * 1000
            if self.mapping.p1_is_left is not None and self.mapping.confidence >= self.settings.min_mapping_confidence:
                self.previous_mapping = self.mapping.p1_is_left
            if confirmed_end:
                event_id = f"{match.set_id}:{self.machine.generation}"
                self.store.log("game_end", {"event_id": event_id,
                               "evidence": asdict(self.machine.end_detection)})
            if self.machine.end_detection.ended:
                event_id = f"{match.set_id}:{self.machine.generation}"
                if (event_id not in self.handled_end_events and self.armed
                        and self.machine.end_detection.confidence >=
                        self.settings.game_end_confidence_threshold
                        and self.machine.winner is not None
                        and self.machine.winner_confidence >= self.settings.min_winner_confidence
                        and self.mapping.p1_is_left is not None
                        and self.mapping.confidence >= self.settings.min_mapping_confidence
                        and self.machine.phase == GamePhase.RESULT):
                    side = Side.LEFT if ((self.machine.winner == Slot.P1) ==
                                         self.mapping.p1_is_left) else Side.RIGHT
                    self.handled_end_events.add(event_id)
                    await self._score(event_id, side, automatic=True)
                elif (event_id not in self.handled_end_events and
                      (not self.armed or self.machine.phase == GamePhase.POST_GAME)):
                    self.handled_end_events.add(event_id)
                    self.store.log("confirmation_required", {"event_id": event_id,
                                   "winner_confidence": self.machine.winner_confidence,
                                   "mapping_confidence": self.mapping.confidence})

    async def _score(self, event_id: str, side: Side, automatic: bool) -> bool:
        match = await self.refresh()
        if match.set_id in ("", "0", "None") or match.left.display_name == "?" or match.right.display_name == "?":
            raise ValueError("No valid two-player set loaded in TSH")
        if match.complete:
            raise ValueError("Set is complete")
        if not self.store.start_score(event_id, match.set_id, side.value,
                                      match.left_score, match.right_score):
            return False
        new_left = match.left_score + (side == Side.LEFT)
        new_right = match.right_score + (side == Side.RIGHT)
        try:
            await self.tsh.set_score(int(new_left), int(new_right))
            verified = await self.tsh.get_current_set()
            if (verified.set_id != match.set_id or verified.left_score != new_left
                    or verified.right_score != new_right):
                raise RuntimeError("TSH read back did not match expected score")
        except Exception as exc:
            self.store.finish_score(event_id, "uncertain")
            self.armed = False
            self.store.log("score_uncertain", {"event_id": event_id, "error": str(exc)})
            raise
        self.store.finish_score(event_id, "applied")
        self.match = verified
        self.machine.phase = GamePhase.SET_COMPLETE if verified.complete else GamePhase.UPDATED
        if verified.complete:
            self.set_complete_at = time.time()
        self.store.log("score_applied", {"event_id": event_id, "side": side.value,
                       "before": [match.left_score, match.right_score],
                       "after": [verified.left_score, verified.right_score],
                       "automatic": automatic, "mapping": asdict(self.mapping)})
        return True

    async def manual_score(self, side: Side) -> bool:
        async with self.lock:
            import uuid
            applied = await self._score(f"manual:{uuid.uuid4()}", side, automatic=False)
            if applied and self.match and self.machine.end_detection.ended:
                event_id = f"{self.match.set_id}:{self.machine.generation}"
                self.handled_end_events.add(event_id)
                if self.machine.winner and self.mapping.p1_is_left is not None:
                    predicted = Side.LEFT if ((self.machine.winner == Slot.P1) ==
                                              self.mapping.p1_is_left) else Side.RIGHT
                    if predicted != side:
                        self.store.log("winner_prediction_error", {
                            "event_id": event_id, "predicted_side": predicted.value,
                            "actual_side": side.value,
                            "winner_evidence": self.machine.winner_evidence})
            return applied

    async def undo(self) -> bool:
        async with self.lock:
            last = self.store.last_score()
            if not last:
                return False
            current = await self.refresh()
            expected = (last["before_left"] + (last["side"] == "left"),
                        last["before_right"] + (last["side"] == "right"))
            if current.set_id != last["set_id"] or (current.left_score, current.right_score) != expected:
                raise ValueError("Score changed since transaction; manual TSH reconciliation required")
            await self.tsh.set_score(last["before_left"], last["before_right"])
            check = await self.tsh.get_current_set()
            if (check.left_score, check.right_score) != (last["before_left"], last["before_right"]):
                raise RuntimeError("Undo read back failed")
            self.store.finish_score(last["event_id"], "undone")
            self.machine.phase = GamePhase.POST_GAME
            self.store.log("score_undone", {"event_id": last["event_id"]})
            return True

    def override_mapping(self, p1_is_left: bool) -> None:
        self.manual_mapping = self.previous_mapping = p1_is_left
        self.mapping = MappingDecision(p1_is_left, 1.0, [])
        self.store.log("mapping_override", {"p1_is_left": p1_is_left})

    async def suggest_next_set(self, assigned: set[str] | None = None) -> dict | None:
        candidates = await self.tsh.get_possible_sets()
        observation = self.observation
        if self.set_complete_at is not None and observation.timestamp <= self.set_complete_at:
            observation = Observation()
        ranked = []
        for match in candidates:
            await self.player_data.enrich(match.left)
            await self.player_data.enrich(match.right)
            await self.supermajor.enrich(match.left)
            await self.supermajor.enrich(match.right)
            await self.player_data.enrich(match.left)
            await self.player_data.enrich(match.right)
            scores: list[tuple[float, list[str]]] = []
            for p1, p2 in ((match.left, match.right), (match.right, match.left)):
                tag1 = tag_score(observation.tags.get(Slot.P1, ""), p1)
                tag2 = tag_score(observation.tags.get(Slot.P2, ""), p2)
                tag_fit = (tag1 + tag2) / 2 if min(tag1, tag2) >= .75 else max(tag1, tag2) * .8
                character_fits = []
                for slot, player in ((Slot.P1, p1), (Slot.P2, p2)):
                    character = observation.characters.get(slot, "").casefold()
                    if character and player.character_distribution:
                        character_fits.append(player.character_distribution.get(character, 0))
                char_fit = sum(character_fits) / len(character_fits) if len(character_fits) == 2 else 0
                score = max(tag_fit, char_fit * .9)
                reasons = [f"tag fit {tag_fit:.0%}"] if tag_fit else []
                if char_fit:
                    reasons.append(f"character fit {char_fit:.0%}")
                if min(tag1, tag2) >= .9 and char_fit >= .65:
                    score = max(score, .98)
                scores.append((score, reasons))
            score, reasons = max(scores, key=lambda item: item[0])
            assigned_here = (bool(assigned and match.set_id in assigned) or
                             bool(self.settings.startgg_stream_name and match.assigned_stream and
                                  self.settings.startgg_stream_name.casefold() in match.assigned_stream.casefold()))
            if assigned_here:
                score = max(score, .96)
                reasons.append("stream assignment")
                if score >= .75 and len(reasons) > 1:
                    score = .99
            ranked.append((score, match, reasons))
        ranked.sort(key=lambda x: x[0], reverse=True)
        if not ranked:
            self.next_candidate = None
            return None
        top, second = ranked[0], ranked[1][0] if len(ranked) > 1 else 0.0
        self.next_candidate = {"set_id": top[1].set_id, "players": [top[1].left.display_name,
                               top[1].right.display_name], "confidence": top[0],
                               "second_best": second, "reasons": top[2]}
        self.store.log("set_candidate", self.next_candidate)
        if (self.set_mode == Mode.AUTO and top[0] >= self.settings.min_set_confidence
                and top[0] - second >= self.settings.min_set_margin
                and (self.set_complete_at is None or
                     (time.time() - self.set_complete_at >= self.settings.post_set_delay_seconds
                      and observation.timestamp >= self.set_complete_at + self.settings.post_set_delay_seconds
                      and observation.game_active and observation.game_set is False
                      and observation.result_screen is False))):
            await self.load_next_set()
        return self.next_candidate

    async def load_next_set(self) -> None:
        if not self.next_candidate:
            raise ValueError("No next-set candidate")
        set_id = self.next_candidate["set_id"]
        await self.tsh.load_set(set_id)
        check = await self.tsh.get_current_set()
        if check.set_id != set_id:
            raise RuntimeError("TSH did not load expected set")
        self.store.log("set_loaded", {"set_id": set_id})
        self.match = check
        self.machine.reset()
        self.next_candidate = None
        self.set_complete_at = None
        self.next_dismissed = False
        self.previous_mapping = self.manual_mapping = None
