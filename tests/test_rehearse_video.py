import argparse

import pytest

from smash_auto_score.rehearse_video import _window


def test_window_accepts_recording_timestamps():
    assert _window("14:50-15:15") == (890, 915)
    assert _window("00:44:30-00:45:05") == (2670, 2705)


@pytest.mark.parametrize("value", ["15:15", "15:15-14:50", "-1-2", "abc-def"])
def test_window_rejects_invalid_ranges(value):
    with pytest.raises(argparse.ArgumentTypeError):
        _window(value)
