"""SO-100 motor calibration, ported from rerun-io/portugal ``src/robot.rs``.

Calibration JSONs live in ``calibrations/<usb_id>.json`` (lerobot-v0 style:
``homing_offset``/``start_pos``/``end_pos``/``calib_mode``/``motor_names``).
Arms without a calibration file fall back to raw-centered degrees so logging
still works out of the box.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

MOTOR_COUNT = 6
TICKS_PER_REV = 4096  # STS3215 position resolution; raw tick domain is 0..TICKS_PER_REV-1

DEFAULT_MOTOR_NAMES: tuple[str, ...] = (
    "shoulder_pan",
    "shoulder_lift",
    "elbow_flex",
    "wrist_flex",
    "wrist_roll",
    "gripper",
)

CalibMode = Literal["DEGREE", "LINEAR"]


@dataclass(frozen=True)
class MotorCalibration:
    motor_name: str
    homing_offset: int
    start_pos: int
    end_pos: int
    calib_mode: CalibMode

    def calibrated_from_raw(self, raw: int) -> float:
        """Raw servo ticks -> degrees (DEGREE) or percent (LINEAR, e.g. gripper)."""
        raw_homed = float(raw) - float(self.homing_offset)
        pos = (raw_homed - float(self.start_pos)) / float(self.end_pos - self.start_pos)
        return pos * 180.0 if self.calib_mode == "DEGREE" else pos * 100.0

    def raw_from_calibrated(self, calibrated: float) -> int:
        """Inverse of ``calibrated_from_raw``, clamped to the servo's 0..4095 tick range.

        Teleop passes leader values through here with the FOLLOWER's calibration, which is
        how per-arm homing offsets and range differences cancel out (as in portugal/lerobot).
        """
        pos = calibrated / (180.0 if self.calib_mode == "DEGREE" else 100.0)
        raw = pos * float(self.end_pos - self.start_pos) + float(self.start_pos) + float(self.homing_offset)
        return min(max(round(raw), 0), TICKS_PER_REV - 1)


def load_calibration(path: Path) -> list[MotorCalibration]:
    raw = json.loads(path.read_text())
    return [
        MotorCalibration(
            motor_name=raw["motor_names"][i],
            homing_offset=raw["homing_offset"][i],
            start_pos=raw["start_pos"][i],
            end_pos=raw["end_pos"][i],
            calib_mode=raw["calib_mode"][i],
        )
        for i in range(MOTOR_COUNT)
    ]


def load_arm_kind(path: Path) -> str | None:
    """Read the extra "kind" key ("leader"/"follower") from a calibration JSON, if present."""
    if not path.exists():
        return None
    kind = json.loads(path.read_text()).get("kind")
    return kind if isinstance(kind, str) else None


def load_arm_ranges(path: Path) -> tuple[list[int], list[int]] | None:
    """Read the recorded range-of-motion sweep (raw ticks) from a calibration JSON, if present.

    Written by ``calibrate-so100``; teleop clamps follower goals to this range so the
    follower is never commanded past the physical limits found during calibration.
    """
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    range_min, range_max = data.get("range_min"), data.get("range_max")
    if not isinstance(range_min, list) or not isinstance(range_max, list) or len(range_min) != MOTOR_COUNT or len(range_max) != MOTOR_COUNT:
        return None
    return [int(v) for v in range_min], [int(v) for v in range_max]


def save_calibration(
    path: Path,
    calibration: list[MotorCalibration],
    *,
    kind: str | None = None,
    range_min: list[int] | None = None,
    range_max: list[int] | None = None,
) -> None:
    """Write portugal-format JSON (drive_mode derived from an inverted start/end range).

    ``kind`` ("leader"/"follower") and ``range_min``/``range_max`` (recorded
    range-of-motion sweep, raw ticks) are extra keys that portugal-format readers ignore.
    """
    payload: dict[str, object] = {
        "homing_offset": [c.homing_offset for c in calibration],
        "drive_mode": [1 if c.end_pos < c.start_pos else 0 for c in calibration],
        "start_pos": [c.start_pos for c in calibration],
        "end_pos": [c.end_pos for c in calibration],
        "calib_mode": [c.calib_mode for c in calibration],
        "motor_names": [c.motor_name for c in calibration],
    }
    if kind is not None:
        payload["kind"] = kind
    if range_min is not None:
        payload["range_min"] = range_min
    if range_max is not None:
        payload["range_max"] = range_max
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def fallback_calibration() -> list[MotorCalibration]:
    """Uncalibrated arms: map raw ticks to degrees centered on 2048 ((raw - 2048) * 360 / 4096)."""
    return [
        MotorCalibration(motor_name=name, homing_offset=0, start_pos=TICKS_PER_REV // 2, end_pos=TICKS_PER_REV, calib_mode="DEGREE")
        for name in DEFAULT_MOTOR_NAMES
    ]
