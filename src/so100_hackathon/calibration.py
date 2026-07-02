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
    payload = {
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
        MotorCalibration(motor_name=name, homing_offset=0, start_pos=2048, end_pos=4096, calib_mode="DEGREE")
        for name in DEFAULT_MOTOR_NAMES
    ]
