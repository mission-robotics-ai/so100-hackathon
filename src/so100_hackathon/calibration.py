"""SO-100 motor calibration, ported from rerun-io/portugal ``src/robot.rs``.

Calibration JSONs live in ``calibrations/<usb_id>.json`` (lerobot-v0 style:
``homing_offset``/``start_pos``/``end_pos``/``calib_mode``/``motor_names``).
Arms without a calibration file fall back to raw-centered degrees so logging
still works out of the box.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

MOTOR_COUNT = 6
TICKS_PER_REV = 4096  # STS3215 position resolution; raw tick domain is 0..TICKS_PER_REV-1
HOMED_CENTER = TICKS_PER_REV // 2  # 2048; software homing lands the middle pose here (see software_homed)


def software_homed(raw: int, middle: int) -> int:
    """Raw servo tick -> host-homed tick, reading ``HOMED_CENTER`` at the calibration middle.

    Emulates in software what a servo-side Homing_Offset achieves — ``Present_Position =
    mechanical - offset (mod 4096)`` — for firmware that stores the offset register but never
    applies it. Placing the middle at ``HOMED_CENTER`` puts the 0/4095 tick wrap half a
    revolution away from the whole usable range, exactly like the servo-side half-turn homing.
    """
    return (raw - middle + HOMED_CENTER) % TICKS_PER_REV


def software_mechanical(homed: int, middle: int) -> int:
    """Inverse of ``software_homed``: host-homed tick -> raw servo tick for a Goal_Position write."""
    return (homed + middle - HOMED_CENTER) % TICKS_PER_REV


@dataclass(frozen=True)
class MechanicalRange:
    """A software-homed arm's swept range re-expressed in raw mechanical ticks, for the
    LeRobot dual-write. See ``mechanical_range_from_homed``."""

    range_min: list[int]
    range_max: list[int]
    full_turn_offsets: dict[int, int]  # motor index -> signed residual offset lerobot can't encode
    wrapped: list[int]  # motor indices whose bounded range crosses the mechanical 0/4095 seam (an error)


def mechanical_range_from_homed(range_min: list[int], range_max: list[int], middle: list[int]) -> MechanicalRange:
    """Map a homed ``[range_min, range_max]`` sweep back to raw mechanical ticks.

    A software-homed arm's servos report *mechanical* Present_Position (the offset register is
    ignored), but lerobot normalizes host-side against ``range_min``/``range_max`` alone. So the
    LeRobot file must carry the swept range in the mechanical frame with ``homing_offset = 0``;
    then lerobot's ``(present - range_min) / (range_max - range_min)`` equals what our export
    computed in the homed frame, because both ``present`` and ``range_*`` shift by the same
    per-motor constant. See ``save_lerobot_calibration``.

    Full-turn joints (homed range == the whole ``[0, TICKS_PER_REV-1]`` circle) are returned
    unshifted: a full circle is rotation-invariant, so its mechanical range is the same circle —
    but that also means the ``middle`` centering can't be baked into a min/max lerobot honors,
    leaving a residual constant offset at a lerobot deploy client (reported in
    ``full_turn_offsets`` for the caller to surface). A *bounded* joint whose shifted range
    crosses the 0/4095 seam genuinely can't be encoded and is reported in ``wrapped``.
    """
    mech_min: list[int] = []
    mech_max: list[int] = []
    full_turn_offsets: dict[int, int] = {}
    wrapped: list[int] = []
    for i, mid in enumerate(middle):
        if range_min[i] == 0 and range_max[i] == TICKS_PER_REV - 1:  # full-turn joint (e.g. wrist_roll)
            mech_min.append(range_min[i])
            mech_max.append(range_max[i])
            # Signed residual (-2048..2047): how far a lerobot deploy client's un-homed reading sits from ours.
            residual = (mid - HOMED_CENTER) % TICKS_PER_REV
            full_turn_offsets[i] = residual - TICKS_PER_REV if residual >= HOMED_CENTER else residual
            continue
        lo = software_mechanical(range_min[i], mid)
        hi = software_mechanical(range_max[i], mid)
        if lo > hi:
            wrapped.append(i)
        mech_min.append(lo)
        mech_max.append(hi)
    return MechanicalRange(range_min=mech_min, range_max=mech_max, full_turn_offsets=full_turn_offsets, wrapped=wrapped)


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
    homing_middle: list[int] | None = None,
) -> None:
    """Write portugal-format JSON (drive_mode derived from an inverted start/end range).

    ``kind`` ("leader"/"follower") and ``range_min``/``range_max`` (recorded
    range-of-motion sweep, raw ticks) are extra keys that portugal-format readers ignore.

    ``homing_middle`` (raw mechanical ticks at the middle pose) marks a *software-homed* arm:
    the servo-side Homing_Offset was NOT written, so every runtime path must apply
    ``software_homed`` per motor before ``calibrated_from_raw`` (and its inverse before a goal
    write). When present, ``homing`` is set to ``"software"`` and the recorded ``range_min``/
    ``range_max`` are in the HOMED frame, consistent with ``start_pos``/``end_pos``.
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
    if homing_middle is not None:
        payload["homing"] = "software"
        payload["homing_middle"] = homing_middle
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def load_homing(path: Path) -> list[int] | None:
    """Per-motor raw mechanical middle if this arm is software-homed, else ``None``.

    ``None`` covers both servo-homed arms (``homing`` absent) and missing files, so callers can
    unconditionally ``bus.enable_software_homing(...)`` only when a list comes back — servo-homed
    and uncalibrated arms keep reading raw ticks straight through, unchanged.
    """
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    if data.get("homing") != "software":
        return None
    middle = data.get("homing_middle")
    if not isinstance(middle, list) or len(middle) != MOTOR_COUNT:
        return None
    return [int(v) for v in middle]


def lerobot_calibration_path(kind: str, name: str) -> Path:
    """Where LeRobot-ecosystem tools (``lerobot-calibrate`` consumers, e.g. the
    newt-starter-so101 deployment client) look up this arm's calibration.

    Mirrors lerobot's constants: ``$HF_LEROBOT_CALIBRATION``, else
    ``$HF_LEROBOT_HOME/calibration``, else ``~/.cache/huggingface/lerobot/calibration``.
    Followers live under ``robots/so101_follower/<name>.json``, leaders under
    ``teleoperators/so101_leader/<name>.json``; ``<name>`` is the ``--robot.id`` those
    tools are launched with (we use the arm's USB id).
    """
    hf_home = Path(os.environ.get("HF_HOME", str(Path.home() / ".cache" / "huggingface")))
    lerobot_home = Path(os.environ.get("HF_LEROBOT_HOME", str(hf_home / "lerobot")))
    calibration_root = Path(os.environ.get("HF_LEROBOT_CALIBRATION", str(lerobot_home / "calibration")))
    group, device = ("robots", "so101_follower") if kind == "follower" else ("teleoperators", "so101_leader")
    return calibration_root / group / device / f"{name}.json"


def save_lerobot_calibration(
    path: Path,
    motor_names: tuple[str, ...],
    motor_ids: tuple[int, ...],
    homing_offsets: list[int],
    range_min: list[int],
    range_max: list[int],
) -> None:
    """The SAME calibration, re-expressed in LeRobot v3's on-disk schema (one object per
    motor: ``id``/``drive_mode``/``homing_offset``/``range_min``/``range_max``).

    Written so an arm calibrated by ``calibrate-so100`` needs NO second
    ``lerobot-calibrate`` sweep to be driven by LeRobot-ecosystem tools — train-time and
    inference-time joint angles then mean the same physical pose, because both sides use
    identical homing offsets and ranges.

    ``homing_offsets`` must be the values ACTUALLY in the servos' EEPROM (read back, not
    assumed): LeRobot's connect-time ``is_calibrated`` check compares this file against
    the servo registers, and on mismatch writes the file's values INTO the servos —
    a wrong file here would silently destroy the calibration.
    """
    payload = {
        name: {
            "id": motor_id,
            "drive_mode": 0,  # standard assembly convention, same as our DRIVE_SIGNS all-positive
            "homing_offset": offset,
            "range_min": lo,
            "range_max": hi,
        }
        for name, motor_id, offset, lo, hi in zip(motor_names, motor_ids, homing_offsets, range_min, range_max, strict=True)
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=4) + "\n")


def fallback_calibration() -> list[MotorCalibration]:
    """Uncalibrated arms: map raw ticks to degrees centered on 2048 ((raw - 2048) * 360 / 4096)."""
    return [
        MotorCalibration(motor_name=name, homing_offset=0, start_pos=TICKS_PER_REV // 2, end_pos=TICKS_PER_REV, calib_mode="DEGREE")
        for name in DEFAULT_MOTOR_NAMES
    ]
