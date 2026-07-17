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
    MotorCalibration,
    fallback_calibration,
    lerobot_calibration_path,
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

    Both the mechanical-center capture and the post-write verify are stability-gated reads.
    This firmware applies a written Homing_Offset to Present_Position hundreds of ms LATE, so a
    single read taken right after a write can catch a transient — a straggler mid-apply, or a
    PRIOR run's restored offset still draining out of the readout. That silently corrupts the
    capture, and every offset derived from it. Each read therefore settles, then polls until two
    consecutive reads agree on every motor. If the verify is stable but still off center, a brief
    torque-enable kick is tried (some Feetech firmware only latches a stored offset on a torque
    edge) before raising, with a diagnostic carrying the stored offsets and snapshots so the cause
    (firmware-not-applying vs. arm-moved) is readable off the error.

    On any failure the previous offsets are restored (best-effort), so a transient bus
    flake doesn't leave the servos half-homed with the on-disk calibration silently stale.
    """
    half_turn = TICKS_PER_REV // 2 - 1  # 2047
    settle_s = 0.5  # let a just-written offset START applying before we judge; this firmware applies LATE
    poll_s = 0.15
    stable_timeout_s = 2.0
    stable_tol = 3  # ticks; two consecutive reads this close on every motor == settled (sensor noise is ~1-2)

    def off_center(positions: list[int]) -> list[str]:
        """Motors whose homed Present_Position isn't ~half_turn (>30 ticks off center)."""
        return [f"{name} reads {p}" for name, p in zip(DEFAULT_MOTOR_NAMES, positions, strict=True) if abs(p - half_turn) > 30]

    def read_stable(what: str) -> list[int]:
        """Read positions until two consecutive reads agree within stable_tol on every motor.

        This firmware applies a written Homing_Offset to Present_Position LATE (2026-07-16, real
        follower: offsets read back correct, but Present_Position caught up hundreds of ms later —
        wrist_roll 1766 -> 622 between an immediate and a 300ms read). A single read right after a
        write can therefore capture a transient: a straggler mid-apply, or a PRIOR run's restored
        offset still draining out of the readout (the gripper drifted a stale-offset-constant -235
        ticks in BOTH failing runs — not hand motion). Both the zero-offset capture and the verify
        are corrupted by this. Settle first so the apply has begun, then poll until it stops moving;
        a real hand motion also never settles. Raise loud with the last two reads if it never does.
        """
        time.sleep(settle_s)
        prev = bus.read_positions(attempts=5)
        deadline = time.monotonic() + stable_timeout_s
        cur = prev
        while time.monotonic() < deadline:
            time.sleep(poll_s)
            cur = bus.read_positions(attempts=5)
            if all(abs(c - p) <= stable_tol for c, p in zip(cur, prev, strict=True)):
                return cur
            prev = cur
        raise RuntimeError(
            f"{what}: positions never stabilized within {stable_timeout_s:.0f}s (offset still applying, or the "
            f"arm is moving) — last two reads {prev} then {cur}, differing by more than {stable_tol} ticks. "
            "Hold the arm still in the middle pose and retry."
        )

    previous = [bus.read_homing_offset(motor_id) for motor_id in bus.motor_ids]
    try:
        for motor_id in bus.motor_ids:
            bus.write_homing_offset(motor_id, 0)
        # Capture the TRUE mechanical center: wait out the zero-offset apply (and any stale offset
        # from a prior run still draining from Present_Position) before reading — a polluted capture
        # writes a wrong offset on every motor, which is the bug this fix closes.
        mechanical = read_stable("capturing the mechanical center")
        for motor_id, mech in zip(bus.motor_ids, mechanical, strict=True):
            bus.write_homing_offset(motor_id, mech - half_turn)
        homed = read_stable("verifying the homed center")
        if not off_center(homed):
            return homed

        # Stable but not centered: the offset is stored (write-verify passed) and has finished
        # applying (read_stable waited it out), yet Present_Position still isn't ~half_turn.
        # Torque kick: some Feetech firmware only latches a stored Homing_Offset into Present_Position
        # on a torque-enable edge. Toggle on->off back-to-back (set_torque writes TORQUE_ENABLE then
        # LOCK per motor) — kept brief so the servo can't fight the hand holding the middle pose.
        bus.set_torque(True)
        bus.set_torque(False)
        post_kick = read_stable("verifying after the torque kick")
        if not off_center(post_kick):
            return post_kick

        # Still off center after the kick. Fail loud with the full diagnostic (it caught the
        # capture-pollution and the slow-apply on real hardware): stored offsets at their intended
        # values + snapshots that BARELY MOVE => firmware isn't applying the stored offset (needs a
        # different trigger); snapshots that swing => the arm moved.
        stored = [bus.read_homing_offset(motor_id) for motor_id in bus.motor_ids]

        def row(label: str, values: list[int]) -> str:
            return f"  {label:<10} " + "  ".join(f"{name}={v}" for name, v in zip(DEFAULT_MOTOR_NAMES, values, strict=True))

        raise RuntimeError(
            "Homing check failed — after writing Homing_Offset the servos should read "
            f"~{half_turn} at center, but Present_Position did not re-zero. Off center: {', '.join(off_center(post_kick))}.\n"
            "If the stored offsets below are their intended values and the snapshots barely change, this "
            "firmware batch is storing Homing_Offset without applying it to Present_Position (settle + torque "
            "kick didn't trigger it) — re-run, and if it persists this arm needs a different homing-apply "
            "trigger. If instead the snapshots swing, the arm moved between reads — hold it still and retry.\n"
            + row("stored", stored)
            + "\n"
            + row("stable", homed)
            + "\n"
            + row("post-kick", post_kick)
        )
    except RuntimeError:
        try:
            for motor_id, offset in zip(bus.motor_ids, previous, strict=True):
                bus.write_homing_offset(motor_id, offset)
            time.sleep(settle_s)  # this firmware applies offsets late; don't exit mid-apply and pollute the next run's capture read
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
        f"Start with the arm folded at rest, then move your **{arm_label}** by hand to match the **gray target**: every joint at the middle of its range of motion. "
        f"This pose becomes 0° for every joint.\n\n"
        f"When it matches, continue (or press Enter in the terminal)."
    )
    send_view("1/2", target)
    bus = FeetechBus(port)
    feed = _LiveArmFeed(bus, rec)
    half_rev = TICKS_PER_REV // 2  # ticks per 180 deg, so calibrated values come out in degrees

    print(f"\ncalibrating {config.kind} {usb_id} on {port} -> {out_path}")
    print("in the viewer: GRAY arm = the target pose to match (a live model appears after step 1)\n")
    try:
        announce_phase("middle")
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
            homing_offsets = None
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

    save_calibration(out_path, calibration, kind=config.kind, range_min=range_min, range_max=range_max)
    # Dual-write: the same numbers in LeRobot's format, at the path LeRobot-ecosystem
    # tools read from. An arm calibrated here can then be driven by the newt-starter /
    # newt SDK without a second calibration — so the joint angles a checkpoint was
    # trained on (our export) and the ones it commands at inference mean the same pose.
    if homing_offsets is not None:
        lerobot_path = lerobot_calibration_path(config.kind, usb_id)
        save_lerobot_calibration(lerobot_path, DEFAULT_MOTOR_NAMES, bus.motor_ids, homing_offsets, range_min, range_max)
        print(f"also wrote {lerobot_path} (LeRobot format — lerobot/newt tools find it with --robot.id={usb_id})")
    instruct(f"# {arm_label.capitalize()} calibrated ✓\n\nSaved to `{out_path}` (and to the servos themselves).")
    print(f"\nwrote {out_path} — verify with: pixi run log-so100")
