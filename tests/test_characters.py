import cv2
import numpy as np

from smash_auto_score.characters import TemplateCharacterDetector
from smash_auto_score.domain import Slot


def face(style):
    image = np.zeros((120, 120, 3), np.uint8)
    cv2.circle(image, (60, 60), 40, (50, 150, 220), -1)
    if style == "eyes":
        cv2.circle(image, (45, 50), 8, (10, 10, 10), -1)
        cv2.circle(image, (75, 50), 8, (10, 10, 10), -1)
    else:
        cv2.rectangle(image, (35, 45), (85, 65), (10, 10, 10), -1)
    return image


def test_temporal_votes_switch_and_reset(tmp_path):
    detector = TemplateCharacterDetector(tmp_path, votes=3)
    detector.save(face("eyes"), Slot.P1, "Fox")
    detector.save(face("mask"), Slot.P1, "Joker")
    assert detector.detect(face("eyes"), Slot.P1)[0] is None
    detector.detect(face("eyes"), Slot.P1)
    assert detector.detect(face("eyes"), Slot.P1)[0] == "fox"
    assert detector.detect(face("mask"), Slot.P1)[0] is None
    detector.detect(face("mask"), Slot.P1)
    assert detector.detect(face("mask"), Slot.P1)[0] == "joker"
    detector.reset()
    assert detector.detect(face("mask"), Slot.P1)[0] is None


def test_unknown_without_templates(tmp_path):
    detector = TemplateCharacterDetector(tmp_path)
    assert detector.detect(face("eyes"), Slot.P2) == (None, 0.0)
