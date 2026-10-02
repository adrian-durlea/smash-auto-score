"""Temporal game lifecycle; end confirmation is separate from winner identity."""

import time
from dataclasses import dataclass, field
from enum import Enum

from .domain import GameEndDetection, Observation, Slot


class GamePhase(str, Enum):
    WAITING = "WAITING_FOR_GAME"
    STARTING = "GAME_STARTING"
    ACTIVE = "GAME_ACTIVE"
    END_CANDIDATE = "GAME_END_CANDIDATE"
    RESULT = "GAME_END_CONFIRMED"
    PENDING = "SCORE_UPDATE_PENDING"
    UPDATED = "SCORE_UPDATED"
    POST_GAME = "POST_GAME"
    SET_COMPLETE = "SET_COMPLETE"


@dataclass
class GameStateMachine:
    phase: GamePhase = GamePhase.WAITING
    generation: int = 0
    active_frames: int = 0
    end_frames: int = 0
    result_frames: int = 0
    clear_frames: int = 0
    winner_frames: int = 0
    winner_candidate: Slot | None = None
    winner: Slot | None = None
    winner_confidence: float = 0.0
    winner_evidence: list[str] = field(default_factory=list)
    end_detection: GameEndDetection = field(default_factory=GameEndDetection)
    ended_at: float = 0.0
    end_confirmation_frames: int = 2
    winner_confirmation_frames: int = 2
    new_game_confirmation_frames: int = 3
    post_game_lockout_seconds: float = 5.0

    def observe(self, obs: Observation) -> bool:
        """Return true only on the first confirmed end of this generation."""
        if self.phase == GamePhase.SET_COMPLETE:
            return False
        now = obs.timestamp or time.time()
        result = obs.result_screen is True and obs.result_confidence >= .97
        if obs.result_screen is True and obs.game_set is True:
            result = True  # Existing OCR/replay observations.
        active = (obs.game_active and obs.game_set is not True and not obs.result_screen
                  and (obs.hud_visible or obs.timestamp == 0))
        if self.phase in (GamePhase.WAITING, GamePhase.STARTING, GamePhase.POST_GAME,
                          GamePhase.UPDATED):
            lockout = bool(self.ended_at and obs.timestamp and
                           now - self.ended_at < self.post_game_lockout_seconds)
            if active and not lockout:
                self.active_frames += 1
                if self.phase == GamePhase.WAITING:
                    self.phase = GamePhase.STARTING
                needed = 3 if self.generation == 0 else self.new_game_confirmation_frames
                if self.active_frames >= needed:
                    self._new_game()
            elif obs.game_set is False:
                self.active_frames = 0
                if self.phase == GamePhase.UPDATED:
                    self.phase = GamePhase.POST_GAME
            return False
        if self.phase in (GamePhase.ACTIVE, GamePhase.END_CANDIDATE):
            if obs.game_set is True:
                self.end_frames += 1
            if result:
                self.result_frames += 1
            elif obs.result_screen is False and obs.game_set is False:
                self.result_frames = 0
                self.end_frames = 0
                self.winner_candidate = None
                self.winner_frames = 0
            if self.end_frames or self.result_frames:
                self.phase = GamePhase.END_CANDIDATE
            if result:
                self._winner(obs)
            if (self.result_frames >= self.end_confirmation_frames or
                    (self.end_frames >= self.end_confirmation_frames and result)):
                self.phase = GamePhase.RESULT
                self.ended_at = now
                evidence = list(obs.end_evidence)
                if self.end_frames:
                    evidence.append(f"GAME SET observations: {self.end_frames}")
                evidence.append(f"results observations: {self.result_frames}")
                self.end_detection = GameEndDetection(True, .98, evidence)
                return True
        elif self.phase == GamePhase.RESULT:
            self._winner(obs)
            if obs.result_screen is False and obs.game_set is False:
                self.clear_frames += 1
                if self.clear_frames >= 3:
                    self.phase = GamePhase.POST_GAME
            else:
                self.clear_frames = 0
        return False

    def _winner(self, obs: Observation) -> None:
        if obs.winner is None or obs.winner_confidence < .97 or obs.result_screen is False:
            self.winner_candidate = None
            self.winner_frames = 0
            return
        if obs.winner == self.winner_candidate:
            self.winner_frames += 1
        else:
            self.winner_candidate = obs.winner
            self.winner_frames = 1
        if self.winner_frames >= self.winner_confirmation_frames:
            self.winner = obs.winner
            self.winner_confidence = obs.winner_confidence
            self.winner_evidence = obs.winner_evidence.copy()

    def _new_game(self) -> None:
        self.generation += 1
        self.phase = GamePhase.ACTIVE
        self.active_frames = self.end_frames = self.result_frames = self.clear_frames = 0
        self.winner_frames = 0
        self.winner_candidate = self.winner = None
        self.winner_confidence = 0.0
        self.winner_evidence = []
        self.end_detection = GameEndDetection()

    def reset(self) -> None:
        self.phase = GamePhase.WAITING
        # Keep the sequence monotonic within a set so a manual reset cannot reuse
        # a persisted score event ID.
        self.active_frames = self.end_frames = self.result_frames = self.clear_frames = 0
        self.winner_frames = 0
        self.winner_candidate = self.winner = None
        self.winner_confidence = 0.0
        self.winner_evidence = []
        self.end_detection = GameEndDetection()
        self.ended_at = 0.0
