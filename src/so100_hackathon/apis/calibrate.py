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
``log-so100`` loads — and DUAL-WRITES the same calibration in LeRobot's format
into the HF cache (``~/.cache/huggingface/lerobot/calibration/...``), so
LeRobot-ecosystem tools (e.g. the newt-starter-so101 deployment client) drive
the arm with exactly the calibration the datasets were recorded with, no second
``lerobot-calibrate`` sweep needed (``pixi run export-calibration`` re-emits it).

    pixi run calibrate-so100 leader --rr-config.connect
    pixi run calibrate-so100 follower --rr-config.connect

``--software-homing`` is the fallback for firmware that stores the Homing_Offset register
but never applies it to Present_Position (so the servo-side step above silently does nothing).
It skips every EEPROM write, captures the mechanical middle, and applies the half-turn homing
host-side on every read and goal instead; the calibration records ``homing="software"`` so
runtime paths do the same, and the LeRobot dual-write carries the centering as mechanical
ranges with ``homing_offset = 0`` — which lerobot honors host-side — so deploy clients stay
correct without depending on that register. Normal (servo-side) calibrations are unchanged.
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

from so100_hackathon.calibration import (
    DEFAULT_MOTOR_NAMES,
    TICKS_PER_REV,
    MechanicalRange,
    MotorCalibration,
    fallback_calibration,
    lerobot_calibration_path,
    mechanical_range_from_homed,
    save_calibration,
    save_lerobot_calibration,
)
from so100_hackathon.feetech import FeetechBus, detect_arm_ports, usb_id_from_port
from so100_hackathon.rerun_config import LiveViewerConfig
from so100_hackathon.setup_phases import announce_phase
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
    software_homing: bool = False
    """Home in software instead of writing the servos' Homing_Offset register — for firmware
    that stores the offset but never applies it to Present_Position (the middle pose would read
    un-centered and every downstream pose would be wrong). Captures the middle pose and applies
    the half-turn homing host-side on every read/goal; the calibration records ``homing=software``
    so runtime paths do the same, and the LeRobot dual-write expresses the centering as mechanical
    ranges (offset 0) so lerobot-driven deploy clients stay correct without touching that register.
    Arms calibrated the normal (servo-side) way are unaffected."""


class _LiveArmFeed:
    """Background thread: read the bus, track min/max, and (once a homing exists)
    animate a 'live' URDF ghost.

    The ghost is only attached after the middle pose is captured — before that
    there is no valid raw->angle mapping and a mismatched model just confuses.
    """

    def __init__(self, bus: FeetechBus, rec: rr.RecordingStream) -> None:
        self.bus = bus
        self.rec = rec
        self.urdf: UrdfArm | None = None
        self._display_calibration: list[MotorCalibration] | None = None
        self.latest_raw: list[int] | None = None
        self.show_ranges = False  # once true, the MIN/POS/MAX table is logged to /ranges (~5 Hz)
        self._tick = 0
        self.reset_ranges()
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread = threading.Thread(target=self._run, name="live-arm", daemon=True)
        self._thread.start()

    def _ranges_table(self, raw: list[int]) -> str:
        """The sweep table as monospace markdown, mirroring the terminal one."""
        lines = [f"{'NAME':<15} {'MIN':>6} {'POS':>6} {'MAX':>6}"]
        for i, name in enumerate(DEFAULT_MOTOR_NAMES):
            lo = str(self.range_min[i]) if self.range_min[i] < TICKS_PER_REV else "-"  # reset sentinels
            hi = str(self.range_max[i]) if self.range_max[i] > 0 else "-"
            lines.append(f"{name:<15} {lo:>6} {raw[i]:>6} {hi:>6}")
        return "```text\n" + "\n".join(lines) + "\n```"

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
                self.rec.set_time("time", timestamp=time.time())
                urdf.log_joints(self.rec, [calib.calibrated_from_raw(r) for calib, r in zip(display, raw, strict=True)])
            self._tick += 1
            if self.show_ranges and self._tick % 4 == 0:  # ~5 Hz is plenty for a table
                self.rec.log("/ranges", rr.TextDocument(self._ranges_table(raw), media_type="text/markdown"), static=True)
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
            raise RuntimeError(
                f"Homing check failed — the arm doesn't look like it's in the middle pose "
                f"(servos should read ~{half_turn} at center). Hold the arm still in the middle "
                f"pose and retry. Readings: {', '.join(drifted)}"
            )
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


def _capture_software_homing_middle(bus: FeetechBus) -> list[int]:
    """Software homing: capture the raw mechanical middle pose WITHOUT touching servo EEPROM.

    The servo-side ``_write_half_turn_homing`` can't be trusted on firmware that stores but never
    applies Homing_Offset, so here the half-turn homing is applied host-side (``software_homed``,
    wired into the bus by the caller). The only servo interaction is reading Present_Position, so
    this works regardless of the offset-register bug.

    Two reads confirm the arm is holding the middle pose (same >30-tick tolerance the servo path
    uses); this check is pure arithmetic — the firmware can't fool it.
    """
    middle = bus.read_positions(attempts=5)
    settle = bus.read_positions(attempts=5)
    drifted = [f"{name} moved {abs(b - a)} ticks" for name, a, b in zip(DEFAULT_MOTOR_NAMES, middle, settle, strict=True) if abs(b - a) > 30]
    if drifted:
        raise RuntimeError(
            f"Middle-pose capture failed — the arm moved while being read. Hold it still in the middle pose and retry. {', '.join(drifted)}"
        )
    return middle


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
    rec = config.rr_config.rec
    is_leader = config.kind == "leader"
    arm_label = "leader arm" if is_leader else "follower arm"
    rec.send_recording_name("Leader arm" if is_leader else "Follower arm")

    def instruct(text: str) -> None:
        """The current step's instructions, shown as a text panel INSIDE the viewer."""
        rec.log("/instructions", rr.TextDocument(text, media_type="text/markdown"), static=True)

    def send_split(text_view: rrb.TextDocumentView, other: rrb.View) -> None:
        """Instructions beside a view at 1:3, so the 3D content dominates (the
        follower's layout is mirrored left/right)."""
        views, shares = ((text_view, other), [1, 3]) if is_leader else ((other, text_view), [3, 1])
        rec.send_blueprint(rrb.Blueprint(rrb.Horizontal(*views, column_shares=shares), collapse_panels=True), make_active=True)

    def send_view(step: str, *arms: UrdfArm, table: bool = False) -> None:
        """Per-phase layout: instructions next to the 3D pose (step 1) or the live
        MIN/POS/MAX table (step 2)."""
        text_view = rrb.TextDocumentView(origin="/instructions", name=f"{step} Instructions for a {arm_label}")
        if table:
            other = rrb.TextDocumentView(origin="/ranges", name=f"Degrees of freedom of a {arm_label}")
            views = (text_view, other) if is_leader else (other, text_view)
            rec.send_blueprint(rrb.Blueprint(rrb.Horizontal(*views), collapse_panels=True), make_active=True)
            return
        send_split(
            text_view,
            rrb.Spatial3DView(
                name="Leader arm" if is_leader else "Follower arm",
                origin="/",
                contents=["$origin/**", "- /instructions/**", "- /ranges/**"],
                overrides={arm.collision_geometries_path: rrb.EntityBehavior(visible=False) for arm in arms},
            ),
        )

    ports = (config.port,) if config.port else detect_arm_ports()
    if not ports:
        raise SystemExit("no SO-100 arms found (no /dev/cu.usbmodem* ports); pass --port explicitly")
    urdf_path = LEADER_URDF_PATH if is_leader else FOLLOWER_URDF_PATH
    # The gray target doubles as the "which arm?" picture during port selection, so it is
    # created (and posed — unposed URDF meshes render as a disassembled pile) up front.
    # The model is several MB: parse and log it once.
    target = UrdfArm.create("target", fallback_calibration(), rec=rec, urdf_path=urdf_path, translation=(0.0, 0.0, 0.0), color=(0.5, 0.5, 0.5))
    rec.set_time("time", timestamp=time.time())
    target.log_pose(rec, list(target.center_angles_rad))
    if len(ports) == 1:
        port = ports[0]
    else:
        announce_phase("wiggle")
        look = "the handle and trigger" if is_leader else "the gripper jaws"
        instruct(
            f"# Wiggle the {arm_label.upper()}\n\n"
            f"Several arms are plugged in — the arm that moves is picked automatically.\n\n"
            f"The **{arm_label}** is the one with {look}: it looks like the 3D model shown next to this text. "
            f"**Wiggle any of its joints.**"
        )
        send_split(
            rrb.TextDocumentView(origin="/instructions", name="Instructions"),
            rrb.Spatial3DView(
                name=f"The {arm_label}",
                origin="/",
                contents=["+ /target/**"],
                overrides={target.collision_geometries_path: rrb.EntityBehavior(visible=False)},
            ),
        )
        port = _pick_arm_by_wiggle(ports)
    usb_id = usb_id_from_port(port)
    out_path = config.calibration_dir / f"{usb_id}.json"
    instruct(
        f"# Move the {arm_label.upper()}\n\n"
        f"## Step 1 of 2 — match the target pose\n\n"
        f"Move your **{arm_label}** by hand to match the **gray target**: every joint at the middle of its range of motion. "
        f"This pose becomes 0° for every joint.\n\n"
        f"When it matches, continue (or press Enter in the terminal)."
    )
    send_view("1/2", target)
    bus = FeetechBus(port)
    feed = _LiveArmFeed(bus, rec)
    half_rev = TICKS_PER_REV // 2  # ticks per 180 deg, so calibrated values come out in degrees

    print(f"\ncalibrating {config.kind} {usb_id} on {port} -> {out_path}")
    print("in the viewer: GRAY arm = the target pose to match (a live model appears after step 1)\n")
    homing_middle: list[int] | None = None  # set (raw mechanical middle) only in software-homing mode
    try:
        announce_phase("middle")
        input("1/2  move the arm to the MIDDLE of its range of motion (match the gray target), then press Enter...")
        feed.require_responding()  # make sure the arm is actually answering before touching EEPROM
        # Homing captures/writes at the middle pose, so KEEP THE ARM STILL here.
        feed.pause()
        bus.set_torque(False)  # torque off to move the arm by hand (also clears Lock for the servo-side EEPROM writes)
        if config.software_homing:
            # Firmware ignores Homing_Offset: home host-side instead. Capture the mechanical middle,
            # wire the transform into the bus, and from here read_positions returns HOMED ticks —
            # so the rest of the flow (display model, sweep, ranges) is identical to the servo path.
            homing_middle = _capture_software_homing_middle(bus)
            bus.enable_software_homing(homing_middle)
            raw_middle = bus.read_positions(attempts=5)
            print(f"     software homing: captured middle {homing_middle} — reads/goals now home host-side; middle reads {raw_middle}")
        else:
            raw_middle = _write_half_turn_homing(bus)
            print(f"     homing offsets written to the servos — middle pose now reads {raw_middle}")
        feed.reset_ranges()  # while still paused, so no stale pre-homing tick can leak into the sweep
        feed.resume()

        # From here the homing is known, so a live model is trustworthy: show it
        # mirroring the real arm (also instantly reveals any mirrored joint).
        display = [
            MotorCalibration(
                motor_name=name, homing_offset=0, start_pos=raw_middle[i], end_pos=raw_middle[i] + DRIVE_SIGNS[i] * half_rev, calib_mode="DEGREE"
            )
            for i, name in enumerate(DEFAULT_MOTOR_NAMES)
        ]
        live = UrdfArm.create("live", display, rec=rec, urdf_path=urdf_path, translation=(0.0, -0.4, 0.0), color=MATTE_BLACK)
        feed.attach_urdf(live, display)
        print("     middle pose captured — the black model now mirrors your arm live")

        grip = "squeeze/release the trigger fully" if is_leader else "fully close and open the gripper"
        instruct(
            f"# Sweep the {arm_label.upper()}\n\n"
            f"## Step 2 of 2 — every joint, full range\n\n"
            f"Move **every joint except wrist_roll** through its full range of motion ({grip} too). "
            f"Watch MIN and MAX fill in as you go — each joint needs a decent sweep to count.\n\n"
            f"When every joint is swept, continue (or press Enter in the terminal)."
        )
        send_view("2/2", target, live, table=True)
        feed.show_ranges = True
        print(f"2/2  move every joint EXCEPT wrist_roll through its full range of motion ({grip} too).")
        print("     recording positions — press Enter to stop...")
        announce_phase("sweep")
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
            homed_note = (
                "No calibration was written (nothing has been written to the servos)"
                if config.software_homing
                else "No limits or calibration were written (the homing offsets were)"
            )
            raise SystemExit(
                f"sweep incomplete for: {', '.join(unswept)} (each joint needs >= {MIN_SWEEP_TICKS} ticks of motion). {homed_note} — re-run and sweep every joint fully."
            )
        feed.pause()
        homing_offsets: list[int] | None = None  # servo-side EEPROM offsets, read back for the LeRobot copy (servo path only)
        mechanical: MechanicalRange | None = None  # swept range in raw mechanical ticks for the LeRobot copy (software path only)
        if config.software_homing:
            # No servo-side writes at all. Position_Limit is EEPROM too (same unreliable firmware),
            # and in software mode the servo works in the mechanical frame while our limits are homed —
            # a servo-frame limit would be both wrong and undependable. Motion limits are enforced
            # host-side (teleop/replay clamp goals to range_min/range_max); the LeRobot file carries the
            # mechanical range so a lerobot deploy client clamps and normalizes there too.
            print("software homing: motion limits enforced host-side only (servo Position_Limit registers left untouched)", flush=True)
            mechanical = mechanical_range_from_homed(range_min, range_max, homing_middle)  # type: ignore[arg-type]  # homing_middle is set in this branch
            if mechanical.wrapped:
                names = ", ".join(DEFAULT_MOTOR_NAMES[i] for i in mechanical.wrapped)
                raise SystemExit(
                    f"software homing can't build a LeRobot range for: {names} — the swept range crosses the servo's 0/4095 tick "
                    "seam in mechanical space (the joint's middle sits too close to the tick wrap). Re-seat the arm so the middle "
                    "pose is farther from the wrap, or calibrate this arm servo-side (without --software-homing)."
                )
        else:
            # Servo-side motion limits from the sweep (lerobot parity). Also overwrites stale limits a
            # previous lerobot calibration may have left, which no longer line up once the offsets changed.
            try:
                for i, motor_id in enumerate(bus.motor_ids):
                    bus.write_position_limits(motor_id, range_min[i], range_max[i])
            except RuntimeError as error:
                # The sweep data is good; don't throw away the whole session over a flaky write.
                print(
                    f"WARNING: writing servo position limits failed ({error}) — saving the calibration anyway; re-run if motion seems restricted",
                    flush=True,
                )
            # Read the servo-side homing offsets back for the LeRobot-format dual-write below.
            # Our own JSON stores homing_offset=0 (the real offsets live in EEPROM), but the
            # LeRobot file must mirror EEPROM exactly — see save_lerobot_calibration.
            try:
                homing_offsets = [bus.read_homing_offset(motor_id) for motor_id in bus.motor_ids]
            except RuntimeError as error:
                print(
                    f"WARNING: reading homing offsets back failed ({error}) — skipping the LeRobot-format copy; "
                    f"emit it later with: pixi run export-calibration -- {config.kind}",
                    flush=True,
                )
    finally:
        feed.stop()
        bus.close()

    calibration: list[MotorCalibration] = []
    for i, name in enumerate(DEFAULT_MOTOR_NAMES):
        span = range_max[i] - range_min[i]
        span_deg = span * 360.0 / TICKS_PER_REV
        if i == GRIPPER_INDEX:
            # Assembly convention: raw min = closed, raw max = open (0..100%).
            calibration.append(MotorCalibration(motor_name=name, homing_offset=0, start_pos=range_min[i], end_pos=range_max[i], calib_mode="LINEAR"))
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

    save_calibration(out_path, calibration, kind=config.kind, range_min=range_min, range_max=range_max, homing_middle=homing_middle)
    # Dual-write: the same calibration in LeRobot's format, at the path LeRobot-ecosystem
    # tools read from. An arm calibrated here can then be driven by the newt-starter /
    # newt SDK without a second calibration — so the joint angles a checkpoint was
    # trained on (our export) and the ones it commands at inference mean the same pose.
    lerobot_path = lerobot_calibration_path(config.kind, usb_id)
    if mechanical is not None:  # software homing: centering lives in the mechanical range, offset 0 (lerobot homes host-side)
        save_lerobot_calibration(
            lerobot_path, DEFAULT_MOTOR_NAMES, bus.motor_ids, [0] * len(bus.motor_ids), mechanical.range_min, mechanical.range_max
        )
        print(f"also wrote {lerobot_path} (LeRobot format — homing_offset 0, mechanical ranges; lerobot normalizes host-side against them)")
        for i, offset in mechanical.full_turn_offsets.items():
            if offset:  # a full-turn joint whose centering lerobot can't encode host-side
                print(
                    f"NOTE: {DEFAULT_MOTOR_NAMES[i]} is a full-turn joint — a lerobot deploy client reads it {offset:+d} ticks "
                    f"({offset * 360.0 / TICKS_PER_REV:+.0f} deg) off our zero (lerobot applies no host-side homing to a full "
                    "circle). This is the one joint software homing can't fully substitute for servo homing.",
                    flush=True,
                )
    elif homing_offsets is not None:  # servo homing: EEPROM offsets + homed ranges
        save_lerobot_calibration(lerobot_path, DEFAULT_MOTOR_NAMES, bus.motor_ids, homing_offsets, range_min, range_max)
        print(f"also wrote {lerobot_path} (LeRobot format — lerobot/newt tools find it with --robot.id={usb_id})")
    saved_where = f"Saved to `{out_path}`." if config.software_homing else f"Saved to `{out_path}` (and to the servos themselves)."
    instruct(f"# {arm_label.capitalize()} calibrated ✓\n\n{saved_where}")
    print(f"\nwrote {out_path} — verify with: pixi run log-so100")
