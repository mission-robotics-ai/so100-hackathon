"""Guided SO-100 calibration with a live Rerun viewer, following the standard
lerobot procedure (``lerobot-calibrate``):

1. Move the arm to the **middle of its range of motion** pose, press Enter.
   That pose defines 0 deg for every joint (lerobot's "half-turn homing").
2. Move **every joint through its full range of motion** (including fully
   closing and opening the gripper/trigger); min/max are recorded live.
   Press Enter when done.

Joint directions are NOT calibrated per-arm: like lerobot, they follow the
standard assembly convention (raw ticks increasing == URDF-positive rotation).
If a joint mirrors on a non-standard build, flip its entry in ``DRIVE_SIGNS``.

The viewer shows two URDF arms: **target** (gray, the middle pose to match)
and **live** (follows the real arm). Torque is off; move the arm by hand.
Writes ``calibrations/<usb_id>.json`` in the portugal format that
``log-so100`` loads.

    pixi run calibrate-so100 --rr-config.connect [--leader]
"""

from __future__ import annotations

import select
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import rerun as rr
import rerun.blueprint as rrb

from so100_hackathon.calibration import DEFAULT_MOTOR_NAMES, MotorCalibration, fallback_calibration, save_calibration
from so100_hackathon.feetech import TICKS_PER_REV, FeetechBus, detect_arm_ports, usb_id_from_port
from so100_hackathon.rerun_config import LiveViewerConfig
from so100_hackathon.urdf_arm import FOLLOWER_URDF_PATH, LEADER_URDF_PATH, MATTE_BLACK, UrdfArm

GRIPPER_INDEX = 5
WRIST_ROLL_INDEX = 4  # full-turn joint: excluded from the sweep, range fixed to 0..4095 (as in lerobot)
DRIVE_SIGNS = (1, 1, 1, 1, 1, 1)  # standard assembly: raw+ == URDF-positive on every joint
MIN_SWEEP_TICKS = 300  # ~26 deg; a joint swept less than this probably wasn't moved


@dataclass
class CalibrateConfig:
    rr_config: LiveViewerConfig = field(default_factory=LiveViewerConfig)
    port: str | None = None
    """Serial port of the arm to calibrate. Default: the single /dev/cu.usbmodem*, or an error listing the options."""
    leader: bool = False
    """Calibrate a leader arm (handle + trigger model; the gripper sweep is squeeze/release the trigger)."""
    calibration_dir: Path = Path("calibrations")


class _LiveArmFeed:
    """Background thread: read the bus, animate the 'live' URDF ghost, track min/max."""

    def __init__(self, bus: FeetechBus, urdf: UrdfArm) -> None:
        self.bus = bus
        self.urdf = urdf
        self.latest_raw: list[int] | None = None
        self.reset_ranges()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="live-arm", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        fallback = fallback_calibration()
        failures = 0
        while not self._stop.is_set():
            try:
                telemetry = self.bus.read_telemetry()
            except RuntimeError as error:
                failures += 1
                if failures == 1 or failures % 50 == 0:  # ~every 5s; a hung table should be diagnosable
                    print(f"\nbus read failed ({failures}): {error}", flush=True)
                time.sleep(0.1)
                continue
            failures = 0
            raw = [t.position_raw for t in telemetry]
            self.latest_raw = raw
            self.range_min = [min(lo, r) for lo, r in zip(self.range_min, raw, strict=True)]
            self.range_max = [max(hi, r) for hi, r in zip(self.range_max, raw, strict=True)]
            rr.set_time("time", timestamp=time.time())
            self.urdf.log_joints([calib.calibrated_from_raw(r) for calib, r in zip(fallback, raw, strict=True)])
            time.sleep(1.0 / 20.0)

    def capture(self) -> list[int]:
        raw = self.latest_raw
        if raw is None:
            raise SystemExit("no positions read from the arm yet — is it powered?")
        return raw

    def reset_ranges(self) -> None:
        self.range_min = [TICKS_PER_REV] * len(DEFAULT_MOTOR_NAMES)
        self.range_max = [0] * len(DEFAULT_MOTOR_NAMES)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)


def _sweep_until_enter(feed: _LiveArmFeed) -> None:
    """Live min/pos/max table (like lerobot's record_ranges_of_motion) until Enter."""
    n_lines = len(DEFAULT_MOTOR_NAMES) + 1
    while True:
        raw = feed.latest_raw or [0] * len(DEFAULT_MOTOR_NAMES)
        print(f"{'NAME':<15} | {'MIN':>6} | {'POS':>6} | {'MAX':>6}")
        for i, name in enumerate(DEFAULT_MOTOR_NAMES):
            print(f"{name:<15} | {feed.range_min[i]:>6} | {raw[i]:>6} | {feed.range_max[i]:>6}")
        if select.select([sys.stdin], [], [], 0.25)[0]:
            sys.stdin.readline()
            return
        print(f"\x1b[{n_lines}A", end="")  # move cursor up to overwrite the table


def main(config: CalibrateConfig) -> None:
    ports = (config.port,) if config.port else detect_arm_ports()
    if not ports:
        raise SystemExit("no SO-100 arms found (no /dev/cu.usbmodem* ports); pass --port explicitly")
    if len(ports) > 1:
        listing = "\n  ".join(ports)
        raise SystemExit(f"multiple arms plugged in, pick one with --port:\n  {listing}")
    port = ports[0]
    usb_id = usb_id_from_port(port)
    out_path = config.calibration_dir / f"{usb_id}.json"

    placeholder = fallback_calibration()
    urdf_path = LEADER_URDF_PATH if config.leader else FOLLOWER_URDF_PATH
    target = UrdfArm.create("target", placeholder, urdf_path=urdf_path, translation=(0.0, 0.0, 0.0), color=(0.5, 0.5, 0.5))
    live = UrdfArm.create("live", placeholder, urdf_path=urdf_path, translation=(0.4, 0.0, 0.0), color=MATTE_BLACK)
    rr.send_blueprint(
        rrb.Blueprint(
            rrb.Spatial3DView(
                name="calibration",
                origin="/",
                overrides={arm.collision_geometries_path: rrb.EntityBehavior(visible=False) for arm in (target, live)},
            ),
            collapse_panels=True,
        ),
        make_active=True,
    )

    bus = FeetechBus(port)
    feed = _LiveArmFeed(bus, live)

    kind = "leader" if config.leader else "follower"
    print(f"\ncalibrating {kind} {usb_id} on {port} -> {out_path}")
    print("in the viewer: GRAY arm = target pose, the other arm = live view of your arm\n")
    try:
        rr.set_time("time", timestamp=time.time())
        target.log_pose(list(target.center_angles_rad))
        input("1/2  move the arm to the MIDDLE of its range of motion (match the gray target), then press Enter...")
        raw_middle = feed.capture()
        feed.reset_ranges()

        grip = "squeeze/release the trigger fully" if config.leader else "fully close and open the gripper"
        print(f"2/2  move every joint EXCEPT wrist_roll through its full range of motion ({grip} too).")
        print("     recording positions — press Enter to stop...")
        _sweep_until_enter(feed)
        range_min, range_max = list(feed.range_min), list(feed.range_max)
        range_min[WRIST_ROLL_INDEX], range_max[WRIST_ROLL_INDEX] = 0, TICKS_PER_REV - 1
    finally:
        feed.stop()
        bus.close()

    calibration: list[MotorCalibration] = []
    half_rev = TICKS_PER_REV // 2  # ticks per 180 deg, so calibrated values come out in degrees
    for i, name in enumerate(DEFAULT_MOTOR_NAMES):
        span = range_max[i] - range_min[i]
        span_deg = span * 360.0 / TICKS_PER_REV
        if span < MIN_SWEEP_TICKS and i != WRIST_ROLL_INDEX:
            print(f"WARNING: {name} only swept {span_deg:.0f} deg — did you move it through its full range?")
        if i == GRIPPER_INDEX:
            # Assembly convention: raw min = closed, raw max = open (0..100%).
            calibration.append(
                MotorCalibration(motor_name=name, homing_offset=0, start_pos=range_min[i], end_pos=range_max[i], calib_mode="LINEAR")
            )
            print(f"{name}: closed={range_min[i]} open={range_max[i]} (span {span_deg:.0f} deg)")
            continue
        calibration.append(
            MotorCalibration(
                motor_name=name,
                homing_offset=0,
                start_pos=raw_middle[i],
                end_pos=raw_middle[i] + DRIVE_SIGNS[i] * half_rev,
                calib_mode="DEGREE",
            )
        )
        print(f"{name}: middle={raw_middle[i]} range=[{range_min[i]}, {range_max[i]}] (span {span_deg:.0f} deg)")

    save_calibration(out_path, calibration, kind=kind, range_min=range_min, range_max=range_max)
    print(f"\nwrote {out_path} — verify with: pixi run log-so100")
