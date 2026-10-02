from smash_auto_score.domain import Observation, PlayerIdentity, SetInfo, Slot
from smash_auto_score.identity import color_similarity, map_players, tag_score


def match():
    return SetInfo("1", PlayerIdentity("DjDickCheese", ["DjDC"],
                    character_distribution={"terry": .8}),
                   PlayerIdentity("Snackz", character_distribution={"fox": .8}),
                   left_color="#ef3636", right_color="#307af0")


def test_tag_examples():
    assert tag_score("DjDC", match().left) > .95
    assert tag_score("snakz", match().right) >= .75
    assert tag_score("LEO", PlayerIdentity("MkLeo")) >= .75
    assert tag_score("dab", PlayerIdentity("Liquid | Dabuz")) >= .75
    assert tag_score("goob", match().left) < .75


def test_one_tag_and_random_tag():
    decision = map_players(match(), Observation(tags={Slot.P1: "DjDC", Slot.P2: "balls"}))
    assert decision.p1_is_left and decision.confidence >= .95


def test_random_tags_color_mapping():
    decision = map_players(match(), Observation(tags={Slot.P1: "goob", Slot.P2: "fortnite"},
                                                colors={Slot.P1: "#e33840", Slot.P2: "#3476e9"}))
    assert decision.p1_is_left and decision.confidence >= .95


def test_ambiguous_and_character_discrimination():
    assert map_players(match(), Observation(tags={Slot.P1: "goob"})).p1_is_left is None
    strong = map_players(match(), Observation(characters={Slot.P1: "terry", Slot.P2: "fox"}))
    assert strong.p1_is_left and strong.confidence > .8
    shared = SetInfo("2", PlayerIdentity("A", character_distribution={"steve": .6}),
                     PlayerIdentity("B", character_distribution={"steve": .55}))
    assert map_players(shared, Observation(characters={Slot.P1: "steve"})).confidence == 0


def test_conflict_lowers_confidence():
    decision = map_players(match(), Observation(tags={Slot.P1: "DjDC"}, colors={Slot.P1: "#3375eb"}), True)
    assert decision.conflict
    assert decision.confidence <= .85


def test_color_tolerance():
    assert color_similarity("#e33840", "#ef3636") > .8
    assert color_similarity("#e33840", "#307af0") < .2
