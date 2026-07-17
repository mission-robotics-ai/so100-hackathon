"""Software-homing wrap math — pure arithmetic, no hardware.

These verify the transform that replaces the servo's Homing_Offset for firmware that stores
but never applies it: the middle pose lands at HOMED_CENTER, reads round-trip losslessly, the
goal-write inverse undoes the read transform, and the LeRobot mechanical-range rebuild keeps a
bounded joint faithful while flagging the full-turn and seam-crossing cases.
"""

from __future__ import annotations

import pytest

from so100_hackathon.calibration import (
    HOMED_CENTER,
    TICKS_PER_REV,
    mechanical_range_from_homed,
    software_homed,
    software_mechanical,
)

# A spread of mechanical middles: dead-centre, off-centre both ways, and near each tick seam.
MIDDLES = [HOMED_CENTER, 0, TICKS_PER_REV - 1, 100, 4000, 2500, 1500]


@pytest.mark.parametrize("middle", MIDDLES)
def test_middle_reads_center(middle: int) -> None:
    """The captured middle pose homes to exactly HOMED_CENTER — the whole point of the offset."""
    assert software_homed(middle, middle) == HOMED_CENTER


@pytest.mark.parametrize("middle", MIDDLES)
@pytest.mark.parametrize("raw", [0, 1, 100, 2047, 2048, 2049, 4000, TICKS_PER_REV - 1])
def test_read_then_goal_roundtrips(raw: int, middle: int) -> None:
    """read (raw->homed) then goal-write (homed->raw) returns the original tick, every seam."""
    homed = software_homed(raw, middle)
    assert 0 <= homed < TICKS_PER_REV
    assert software_mechanical(homed, middle) == raw


@pytest.mark.parametrize("middle", MIDDLES)
@pytest.mark.parametrize("homed", [0, 1, 2048, 4000, TICKS_PER_REV - 1])
def test_goal_then_read_roundtrips(homed: int, middle: int) -> None:
    """The other direction: a homed goal maps to a raw tick that reads back as that homed goal."""
    raw = software_mechanical(homed, middle)
    assert 0 <= raw < TICKS_PER_REV
    assert software_homed(raw, middle) == homed


def test_homed_is_a_pure_shift_off_center() -> None:
    """Away from the wrap, homing is just (raw - middle) added to the center — no scaling."""
    middle = 2500
    for raw in range(2400, 2600):  # a window that doesn't cross the seam for this middle
        assert software_homed(raw, middle) == raw - middle + HOMED_CENTER


def test_bounded_joint_mechanical_range_is_faithful() -> None:
    """A bounded joint's homed range maps back to the raw ticks it was actually swept over,
    and lerobot's host-side normalization then matches our homed-frame export exactly."""
    middle = [2600, 1400, 2048, 2200, 2048, 3000]
    # Homed sweep: symmetric-ish window around HOMED_CENTER for the five bounded joints; the
    # gripper (index 5) a smaller homed window. wrist_roll (index 4) is full-turn, tested below.
    range_min = [1500, 1400, 1600, 1700, 0, 1800]
    range_max = [2600, 2700, 2500, 2400, TICKS_PER_REV - 1, 2300]
    result = mechanical_range_from_homed(range_min, range_max, middle)
    assert result.wrapped == []
    for i in (0, 1, 2, 3, 5):
        assert result.range_min[i] == software_mechanical(range_min[i], middle[i])
        assert result.range_max[i] == software_mechanical(range_max[i], middle[i])
        assert result.range_min[i] < result.range_max[i]
        # The invariant lerobot depends on: present - mech_min == homed_present - homed_min.
        homed_present = software_homed(software_mechanical(range_min[i], middle[i]) + 5, middle[i])
        mech_present = software_mechanical(range_min[i], middle[i]) + 5
        assert (mech_present - result.range_min[i]) == (homed_present - range_min[i])


def test_full_turn_joint_kept_whole_and_offset_reported() -> None:
    """A full-turn joint's range stays the whole circle (rotation-invariant), and the residual
    offset lerobot can't encode host-side is reported signed for the caller to surface."""
    middle = [2048, 2048, 2048, 2048, 2500, 2048]
    range_min = [1500] * 6
    range_max = [2600] * 6
    range_min[4], range_max[4] = 0, TICKS_PER_REV - 1  # wrist_roll: full turn
    result = mechanical_range_from_homed(range_min, range_max, middle)
    assert result.range_min[4] == 0
    assert result.range_max[4] == TICKS_PER_REV - 1
    assert result.full_turn_offsets[4] == 2500 - HOMED_CENTER  # +452, signed
    assert result.wrapped == []


def test_full_turn_offset_wraps_to_signed() -> None:
    """A middle below center gives a negative residual, not a ~4096 positive one."""
    middle = [1000, 1000, 1000, 1000, 1000, 1000]
    range_min = [0] * 6
    range_max = [TICKS_PER_REV - 1] * 6
    result = mechanical_range_from_homed(range_min, range_max, middle)
    assert result.full_turn_offsets[0] == 1000 - HOMED_CENTER  # -1048


def test_seam_crossing_bounded_joint_is_flagged() -> None:
    """A bounded joint whose middle sits so close to the wrap that its swept range straddles the
    mechanical 0/4095 seam can't be encoded as a min<max lerobot range — it must be flagged."""
    # middle near the top of the tick range: a homed window around center maps to raw ticks that
    # cross 0/4095, so mechanical lo ends up above hi.
    middle = [4090, 2048, 2048, 2048, 2048, 2048]
    range_min = [1500, 1500, 1500, 1500, 0, 1500]
    range_max = [2600, 2600, 2600, 2600, TICKS_PER_REV - 1, 2600]
    result = mechanical_range_from_homed(range_min, range_max, middle)
    assert 0 in result.wrapped
    assert result.range_min[0] > result.range_max[0]
