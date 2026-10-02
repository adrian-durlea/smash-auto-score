from dataclasses import dataclass
from enum import Enum

from .domain import Observation


class GamePhase(str, Enum):
    WAITING = "WAITING_FOR_GAME"
    STARTING = "GAME_STARTING"
    ACTIVE = "GAME_ACTIVE"
    END_CANDIDATE = "GAME_END_CANDIDATE"
    RESULT = "RESULT_CONFIRMATION"
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
    clear_frames: int = 0

    def observe(self, obs: Observation) -> bool:
        """Return true once for a confirmed end transition."""
        if self.phase == GamePhase.SET_COMPLETE:
            return False
        if obs.game_active and obs.game_set is False and obs.result_screen is False:
            self.active_frames += 1
            self.clear_frames = 0
            if self.phase in (GamePhase.WAITING, GamePhase.POST_GAME):
                if self.active_frames >= 3:
                    self.generation += 1
                    self.phase = GamePhase.ACTIVE
                    self.end_frames = 0
            elif self.phase == GamePhase.STARTING and self.active_frames >= 3:
                self.phase = GamePhase.ACTIVE
        elif obs.game_set is False:
            self.active_frames = 0
            if self.phase in (GamePhase.UPDATED, GamePhase.RESULT):
                self.clear_frames += 1
                if self.clear_frames >= 3:
                    self.phase = GamePhase.POST_GAME
        if self.phase in (GamePhase.ACTIVE, GamePhase.END_CANDIDATE):
            if obs.game_set:
                self.end_frames += 1
                self.phase = GamePhase.END_CANDIDATE
                if self.end_frames >= 3 and obs.result_screen:
                    self.phase = GamePhase.RESULT
                    return True
            elif obs.game_set is False:
                self.end_frames = 0
        return False

    def reset(self) -> None:
        self.phase = GamePhase.WAITING
        self.active_frames = self.end_frames = self.clear_frames = 0
