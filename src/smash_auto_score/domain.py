from dataclasses import dataclass, field
from enum import Enum


class Side(str, Enum):
    LEFT = "left"
    RIGHT = "right"


class Slot(str, Enum):
    P1 = "P1"
    P2 = "P2"


class Mode(str, Enum):
    OFF = "OFF"
    SUGGEST = "SUGGEST"
    AUTO = "AUTO"


@dataclass
class PlayerIdentity:
    display_name: str
    aliases: list[str] = field(default_factory=list)
    sponsor: str | None = None
    startgg_id: str | None = None
    supermajor_id: str | None = None
    character_distribution: dict[str, float] = field(default_factory=dict)


@dataclass
class SetInfo:
    set_id: str
    left: PlayerIdentity
    right: PlayerIdentity
    left_score: int = 0
    right_score: int = 0
    best_of: int = 3
    round: str = ""
    event: str = ""
    left_color: str | None = None
    right_color: str | None = None
    assigned_stream: str | None = None

    @property
    def complete(self) -> bool:
        target = self.best_of // 2 + 1
        return self.left_score >= target or self.right_score >= target


@dataclass
class Evidence:
    source: str
    p1_is_left: bool
    confidence: float
    description: str
    weight: float = 1.0


@dataclass
class MappingDecision:
    p1_is_left: bool | None
    confidence: float
    evidence: list[Evidence]
    conflict: bool = False


@dataclass
class Observation:
    tags: dict[Slot, str] = field(default_factory=dict)
    colors: dict[Slot, str] = field(default_factory=dict)
    characters: dict[Slot, str] = field(default_factory=dict)
    game_active: bool = False
    game_set: bool | None = False
    result_screen: bool | None = False
    winner: Slot | None = None
    winner_confidence: float = 0.0
    timestamp: float = 0.0
