"""Two-hypothesis player identity inference."""
import math
import re
import unicodedata

from rapidfuzz import fuzz

from .domain import Evidence, MappingDecision, Observation, PlayerIdentity, SetInfo, Slot


def normalize_tag(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).casefold()
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = re.split(r"\s*[|\[\]]\s*", value)[-1]
    return re.sub(r"[^a-z0-9]", "", value).translate(str.maketrans("01l5", "oiis"))


def tag_score(tag: str, player: PlayerIdentity) -> float:
    needle = normalize_tag(tag)
    if len(needle) < 3:
        return 0.0
    scores = []
    for name in [player.display_name, *player.aliases]:
        name = normalize_tag(name)
        if not name:
            continue
        ratio = fuzz.ratio(needle, name) / 100
        partial = fuzz.partial_ratio(needle, name) / 100 if len(needle) >= 4 else 0
        subsequence = sum(a == b for a, b in zip(needle, name)) / max(len(needle), len(name))
        initials = "".join(x[0] for x in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", player.display_name) if x)
        abbreviation = fuzz.ratio(needle, normalize_tag(initials)) / 100 if len(initials) >= 3 else 0
        scores.append(max(ratio, partial * 0.91, subsequence, abbreviation * 0.9))
    return max(scores, default=0.0)


def _hue(hex_color: str) -> tuple[float, float] | None:
    import colorsys
    try:
        rgb = tuple(int(hex_color.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
        h, s, _ = colorsys.rgb_to_hsv(*rgb)
        return h, s
    except (ValueError, IndexError):
        return None


def color_similarity(a: str, b: str) -> float:
    x, y = _hue(a), _hue(b)
    if not x or not y or min(x[1], y[1]) < 0.2:
        return 0.0
    distance = min(abs(x[0] - y[0]), 1 - abs(x[0] - y[0]))
    return max(0.0, 1 - distance / 0.22)


def _character_probability(player: PlayerIdentity, character: str) -> float | None:
    if not player.character_distribution:
        return None
    return player.character_distribution.get(character.casefold(), 0.005)


def mapping_evidence(match: SetInfo, obs: Observation, previous: bool | None = None) -> list[Evidence]:
    evidence: list[Evidence] = []
    for slot, player, other, direct in ((Slot.P1, match.left, match.right, True),
                                         (Slot.P2, match.right, match.left, True)):
        tag = obs.tags.get(slot, "")
        own, alternative = tag_score(tag, player), tag_score(tag, other)
        if own >= 0.75 and own - alternative >= 0.2:
            evidence.append(Evidence("tag", direct, min(0.98, own),
                                     f"{slot.value} tag {tag!r} matches {player.display_name}"))
        elif alternative >= 0.75 and alternative - own >= 0.2:
            evidence.append(Evidence("tag", not direct, min(0.98, alternative),
                                     f"{slot.value} tag {tag!r} matches {other.display_name}"))
    if match.left_color and match.right_color:
        for slot, expected_color, other_color, direct in ((Slot.P1, match.left_color, match.right_color, True),
                                                          (Slot.P2, match.right_color, match.left_color, True)):
            color = obs.colors.get(slot)
            if color:
                color_fit, other_fit = color_similarity(color, expected_color), color_similarity(color, other_color)
                if max(color_fit, other_fit) >= 0.7 and abs(color_fit - other_fit) >= 0.35:
                    evidence.append(Evidence("color", direct if color_fit > other_fit else not direct,
                                             min(0.99, 0.7 + abs(color_fit - other_fit) * 0.29),
                                             f"{slot.value} HUD color {color} discriminates player colors", 1.3))
    if len(set(obs.characters.values())) == 1 and len(obs.characters) == 2:
        characters = {}
    else:
        characters = obs.characters
    for slot, char_player, char_other, direct in ((Slot.P1, match.left, match.right, True),
                                                  (Slot.P2, match.right, match.left, True)):
        character = characters.get(slot)
        if character:
            char_fit = _character_probability(char_player, character)
            other_char_fit = _character_probability(char_other, character)
            if char_fit is not None and other_char_fit is not None:
                ratio = math.log((char_fit + 0.01) / (other_char_fit + 0.01))
                if abs(ratio) >= 1:
                    evidence.append(Evidence("character_history", direct if ratio > 0 else not direct,
                                             min(0.92, 0.5 + abs(ratio) * 0.1),
                                             f"{character} usage: {char_player.display_name} {char_fit:.0%}, {char_other.display_name} {other_char_fit:.0%}", 0.8))
    if previous is not None:
        evidence.append(Evidence("previous_mapping", previous, 0.95, "Stable prior-game mapping", 1.3))
    return evidence


def fuse(evidence: list[Evidence]) -> MappingDecision:
    if not evidence:
        return MappingDecision(None, 0.0, [])
    log_odds = 0.0
    opposing = set()
    for item in evidence:
        c = min(0.99, max(0.51, item.confidence))
        log_odds += (1 if item.p1_is_left else -1) * item.weight * math.log(c / (1 - c))
        if item.confidence >= 0.8:
            opposing.add(item.p1_is_left)
    conflict = len(opposing) > 1
    confidence = 1 / (1 + math.exp(-abs(log_odds)))
    if conflict:
        confidence = min(confidence, 0.85)
    if all(item.source == "character_history" for item in evidence):
        confidence = min(confidence, 0.90)
    return MappingDecision(log_odds > 0, confidence, evidence, conflict)


def map_players(match: SetInfo, obs: Observation, previous: bool | None = None) -> MappingDecision:
    return fuse(mapping_evidence(match, obs, previous))
