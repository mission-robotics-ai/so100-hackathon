"""Guided SO-100 calibration with a live Rerun viewer, following the standard
lerobot procedure (``lerobot-calibrate``):

1. Move the arm to the **middle of its range of motion** pose, press Enter.
   That pose defines 0 deg for every joint. Like lerobot's "half-turn homing",
   the offset is written to each servo's Homing_Offset EEPROM register so the
   middle reads ~2047 ticks — which pushes the 0/4095 tick wrap half a turn
   away from the whole usable range (software-only offsets can't prevent an
   unluckily-assembled joint from wrapping mid-sweep).
2. Move **every joint through its full range of motion** (including fully
   closing and opening the gripper/trigger); min/max are recorded live.
   Press Enter when done. The swept range is also written to the servos'
   Min/Max_Position_Limit registers (lerobot parity).

Joint directions are NOT calibrated per-arm: like lerobot, they follow the
standard assembly convention (raw ticks increasing == URDF-positive rotation).
If a joint mirrors on a non-standard build, flip its entry in ``DRIVE_SIGNS``.

The viewer shows two URDF arms: **target** (gray, the middle pose to match)
and **live** (follows the real arm). Torque is off; move the arm by hand.
Writes ``calibrations/<usb_id>.json`` in the portugal format that
``log-so100`` loads.

    pixi run calibrate-so100 leader --rr-config.connect
    pixi run calibrate-so100 follower --rr-config.connect
"""

from __future__ import annotations

import select
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import rerun as rr
import rerun.blueprint as rrb
import tyro

from so100_hackathon.calibration import DEFAULT_MOTOR_NAMES, TICKS_PER_REV, MotorCalibration, fallback_calibration, save_calibration
from so100_hackathon.feetech import FeetechBus, detect_arm_ports, usb_id_from_port
from so100_hackathon.rerun_config import LiveViewerConfig
from so100_hackathon.urdf_arm import FOLLOWER_URDF_PATH, LEADER_URDF_PATH, MATTE_BLACK, UrdfArm

GRIPPER_INDEX = 5
WRIST_ROLL_INDEX = 4  # full-turn joint: excluded from the sweep, range fixed to 0..4095 (as in lerobot)
DRIVE_SIGNS = (1, 1, 1, 1, 1, 1)  # standard assembly: raw+ == URDF-positive on every joint
MIN_SWEEP_TICKS = 300  # ~26 deg; a joint swept less than this probably wasn't moved
WIGGLE_TICKS = 100  # ~9 deg of joint motion identifies an arm during port selection


@dataclass
class CalibrateConfig:
    kind: tyro.conf.Positional[Literal["leader", "follower"]]
    """Which arm this is — required, so leader/follower is always explicit. The leader
    uses the handle + trigger model, and its gripper sweep is squeeze/release the trigger."""
    rr_config: LiveViewerConfig = field(default_factory=LiveViewerConfig)
    port: str | None = None
    """Serial port of the arm to calibrate. Default: the single plugged-in arm; with
    several plugged in, wiggle a joint on the one you want and it's picked automatically."""
    calibration_dir: Path = Path("calibrations")


class _LiveArmFeed:
    """Background thread: read the bus, track min/max, and (once a homing exists)
    animate a 'live' URDF ghost.

    The ghost is only attached after the middle pose is captured — before that
    there is no valid raw->angle mapping and a mismatched model just confuses.
    """

    def __init__(self, bus: FeetechBus) -> None:
        self.bus = bus
        self.urdf: UrdfArm | None = None
        self._display_calibration: list[MotorCalibration] | None = None
        self.latest_raw: list[int] | None = None
        self.reset_ranges()
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread = threading.Thread(target=self._run, name="live-arm", daemon=True)
        self._thread.start()

    def attach_urdf(self, urdf: UrdfArm, calibration: list[MotorCalibration]) -> None:
        self._display_calibration = calibration
        self.urdf = urdf

    def pause(self) -> None:
        """Stop polling before main-thread register writes; returns once no read is in flight.

        A read that slipped past the flag check still serializes against the writes via the
        bus lock, and the stale ranges it may record are cleared by reset_ranges() while paused.
        """
        self._paused.set()
        with self.bus.lock:  # wait out any in-flight transaction
            pass

    def resume(self) -> None:
        self._paused.clear()

    def _run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            if self._paused.is_set():
                time.sleep(0.05)
                continue
            try:
                raw = self.bus.read_positions()
            except RuntimeError as error:
                failures += 1
                if failures == 1 or failures % 50 == 0:  # ~every 5s; a hung table should be diagnosable
                    print(f"\nbus read failed ({failures}): {error}", flush=True)
                time.sleep(0.1)
                continue
            failures = 0
            self.latest_raw = raw
            if self._paused.is_set():  # a read that slipped past the flag check: skip the ranges
                continue
            self.range_min = [min(lo, r) for lo, r in zip(self.range_min, raw, strict=True)]
            self.range_max = [max(hi, r) for hi, r in zip(self.range_max, raw, strict=True)]
            urdf, display = self.urdf, self._display_calibration
            if urdf is not None and display is not None:
                rr.set_time("time", timestamp=time.time())
                urdf.log_joints([calib.calibrated_from_raw(r) for calib, r in zip(display, raw, strict=True)])
            time.sleep(1.0 / 20.0)

    def require_responding(self) -> None:
        if self.latest_raw is None:
            raise SystemExit("no positions read from the arm yet — is it powered?")

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


def _write_half_turn_homing(bus: FeetechBus) -> list[int]:
    """lerobot's set_half_turn_homings: write servo-side Homing_Offset so the CURRENT pose
    (the middle of the range of motion) reads ~2047 on every motor.

    This puts the 0/4095 tick wrap half a revolution away from the middle, so no joint can
    cross it during the range sweep — software-only offsets can't guarantee that, and an
    arm whose middle happens to sit near the wrap point gets +-360 deg jumps (seen on real
    hardware). Returns the re-read (homed) middle positions.

    On any failure the previous offsets are restored (best-effort), so a transient bus
    flake doesn't leave the servos half-homed with the on-disk calibration silently stale.
    """
    half_turn = TICKS_PER_REV // 2 - 1  # 2047
    previous = [bus.read_homing_offset(motor_id) for motor_id in bus.motor_ids]
    try:
        for motor_id in bus.motor_ids:
            bus.write_homing_offset(motor_id, 0)
        mechanical = bus.read_positions(attempts=5)
        for motor_id, mech in zip(bus.motor_ids, mechanical, strict=True):
            bus.write_homing_offset(motor_id, mech - half_turn)
        homed = bus.read_positions(attempts=5)
        drifted = [f"{name} reads {p}" for name, p in zip(DEFAULT_MOTOR_NAMES, homed, strict=True) if abs(p - half_turn) > 30]
        if drifted:  # the arm moved between the two reads, or a write was lost
            raise RuntimeError(f"homing verification failed (expected ~{half_turn}): {', '.join(drifted)} — hold the arm still and retry")
        return homed
    except RuntimeError:
        try:
            for motor_id, offset in zip(bus.motor_ids, previous, strict=True):
                bus.write_homing_offset(motor_id, offset)
            print("homing failed — previous servo offsets restored, just re-run calibration", flush=True)
        except RuntimeError:
            print(
                "homing failed AND restoring the previous offsets failed — this arm's servo homing is now "
                "inconsistent and any existing calibration for it is stale; re-run calibration before using it",
                flush=True,
            )
        raise


def _pick_arm_by_wiggle(ports: tuple[str, ...]) -> str:
    """Several arms are plugged in: identify one physically instead of by port name."""
    buses = {port: FeetechBus(port) for port in ports}
    try:
        baselines: dict[str, list[int]] = {}
        print(f"{len(ports)} arms found — WIGGLE any joint on the arm you want to calibrate...", flush=True)
        while True:
            for port, bus in buses.items():
                try:
                    positions = bus.read_positions()
                except RuntimeError:
                    continue
                if port not in baselines:
                    baselines[port] = positions
                elif any(abs(now - then) > WIGGLE_TICKS for now, then in zip(positions, baselines[port], strict=True)):
                    print(f"detected movement on {port}", flush=True)
                    return port
            time.sleep(0.05)
    finally:
        for bus in buses.values():
            bus.close()


def main(config: CalibrateConfig) -> None:
    ports = (config.port,) if config.port else detect_arm_ports()
    if not ports:
        raise SystemExit("no SO-100 arms found (no /dev/cu.usbmodem* ports); pass --port explicitly")
    port = ports[0] if len(ports) == 1 else _pick_arm_by_wiggle(ports)
    usb_id = usb_id_from_port(port)
    out_path = config.calibration_dir / f"{usb_id}.json"

    is_leader = config.kind == "leader"
    urdf_path = LEADER_URDF_PATH if is_leader else FOLLOWER_URDF_PATH
    target = UrdfArm.create("target", fallback_calibration(), urdf_path=urdf_path, translation=(0.0, 0.0, 0.0), color=(0.5, 0.5, 0.5))

    def send_view(*arms: UrdfArm) -> None:
        rr.send_blueprint(
            rrb.Blueprint(
                rrb.Spatial3DView(
                    name="calibration",
                    origin="/",
                    overrides={arm.collision_geometries_path: rrb.EntityBehavior(visible=False) for arm in arms},
                ),
                collapse_panels=True,
            ),
            make_active=True,
        )

    send_view(target)
    bus = FeetechBus(port)
    feed = _LiveArmFeed(bus)
    half_rev = TICKS_PER_REV // 2  # ticks per 180 deg, so calibrated values come out in degrees

    print(f"\ncalibrating {config.kind} {usb_id} on {port} -> {out_path}")
    print("in the viewer: GRAY arm = the target pose to match (a live model appears after step 1)\n")
    try:
        rr.set_time("time", timestamp=time.time())
        target.log_pose(list(target.center_angles_rad))
        input("1/2  move the arm to the MIDDLE of its range of motion (match the gray target), then press Enter...")
        feed.require_responding()  # make sure the arm is actually answering before touching EEPROM
        # Half-turn homing (lerobot): written to the servos, so KEEP THE ARM STILL here.
        feed.pause()
        bus.set_torque(False)  # clears Lock so the EEPROM writes below land (torque is already off)
        raw_middle = _write_half_turn_homing(bus)
        feed.reset_ranges()  # while still paused, so no stale pre-homing tick can leak into the sweep
        feed.resume()
        print(f"     homing offsets written to the servos — middle pose now reads {raw_middle}")

        # From here the homing is known, so a live model is trustworthy: show it
        # mirroring the real arm (also instantly reveals any mirrored joint).
        display = [
            MotorCalibration(motor_name=name, homing_offset=0, start_pos=raw_middle[i], end_pos=raw_middle[i] + DRIVE_SIGNS[i] * half_rev, calib_mode="DEGREE")
            for i, name in enumerate(DEFAULT_MOTOR_NAMES)
        ]
        live = UrdfArm.create("live", display, urdf_path=urdf_path, translation=(0.0, -0.4, 0.0), color=MATTE_BLACK)
        feed.attach_urdf(live, display)
        send_view(target, live)
        print("     middle pose captured — the black model now mirrors your arm live")

        grip = "squeeze/release the trigger fully" if is_leader else "fully close and open the gripper"
        print(f"2/2  move every joint EXCEPT wrist_roll through its full range of motion ({grip} too).")
        print("     recording positions — press Enter to stop...")
        _sweep_until_enter(feed)
        range_min, range_max = list(feed.range_min), list(feed.range_max)
        range_min[WRIST_ROLL_INDEX], range_max[WRIST_ROLL_INDEX] = 0, TICKS_PER_REV - 1
        # Validate BEFORE anything is persisted: an early Enter or unmoved joint would
        # otherwise burn a garbage range (even the 4096/0 reset sentinels) into the servos.
        unswept = [
            name
            for i, name in enumerate(DEFAULT_MOTOR_NAMES)
            if i != WRIST_ROLL_INDEX and not (0 <= range_min[i] <= range_max[i] < TICKS_PER_REV and range_max[i] - range_min[i] >= MIN_SWEEP_TICKS)
        ]
        if unswept:
            raise SystemExit(
                f"sweep incomplete for: {', '.join(unswept)} (each joint needs >= {MIN_SWEEP_TICKS} ticks of motion). "
                "No limits or calibration were written (the homing offsets were) — re-run and sweep every joint fully."
            )
        # Servo-side motion limits from the sweep (lerobot parity). Also overwrites stale
        # limits a previous lerobot calibration may have left, which no longer line up
        # once the homing offsets above changed.
        feed.pause()
        try:
            for i, motor_id in enumerate(bus.motor_ids):
                bus.write_position_limits(motor_id, range_min[i], range_max[i])
        except RuntimeError as error:
            # The sweep data is good; don't throw away the whole session over a flaky write.
            print(f"WARNING: writing servo position limits failed ({error}) — saving the calibration anyway; re-run if motion seems restricted", flush=True)
    finally:
        feed.stop()
        bus.close()

    calibration: list[MotorCalibration] = []
    for i, name in enumerate(DEFAULT_MOTOR_NAMES):
        span = range_max[i] - range_min[i]
        span_deg = span * 360.0 / TICKS_PER_REV
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

    save_calibration(out_path, calibration, kind=config.kind, range_min=range_min, range_max=range_max)
    print(f"\nwrote {out_path} — verify with: pixi run log-so100")
