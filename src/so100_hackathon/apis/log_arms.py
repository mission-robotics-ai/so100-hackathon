"""Realtime SO-100 telemetry + cameras + animated URDF -> Rerun.

Python port of rerun-io/portugal's log_v2 + robot.rs + camera.rs, plus the
dual-arm pattern from rerun's examples/python/animated_urdf.

Default: auto-detect every plugged-in arm and camera, spawn a viewer, and
stream live. Pass ``--rr-config.save out.rrd`` to also write the recording
(dual-sink). In a shell with no display, pass ``--rr-config.headless`` so the
viewer spawn doesn't wedge logging.

    pixi run log-so100 --fps 30 --cameras 1 2 --rr-config.save session.rrd

Lives in the package (not the ``tools/`` shim) so ``beartype_this_package()``
type-checks ``main`` and the config when running under the dev environment.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import rerun as rr

from so100_hackathon.blueprint import create_blueprint
from so100_hackathon.calibration import MotorCalibration, fallback_calibration, load_arm_kind, load_arm_ranges, load_calibration
from so100_hackathon.cameras import CameraStreamer, detect_camera_indices
from so100_hackathon.feetech import FeetechBus, MotorTelemetry, detect_arm_ports, usb_id_from_port
from so100_hackathon.rerun_config import LiveViewerConfig
from so100_hackathon.urdf_arm import FOLLOWER_URDF_PATH, LEADER_URDF_PATH, MATTE_BLACK, UrdfArm

# entity subpath -> attribute of MotorTelemetry (calibrated position is logged separately)
METRICS: dict[str, str] = {
    "position_raw": "position_raw",
    "speed": "speed_ticks_s",
    "load": "load_pct",
    "current": "current_ma",
    "voltage": "voltage_v",
    "temperature": "temperature_c",
}
METRIC_SUBPATHS = ("position", *METRICS)

TELEOP_RAMP_SECONDS = 1.5
"""On teleop start the follower glides to the leader's pose over this long, instead of jumping."""


@dataclass
class Arm:
    name: str
    bus: FeetechBus
    calibration: list[MotorCalibration]
    urdf: UrdfArm | None = None
    errors: int = 0
    """Consecutive bus-read failures (reset on the first successful read)."""
    is_leader: bool = False
    calibrated: bool = False
    """True when a real calibration file was loaded (teleop refuses to run on fallbacks)."""
    ranges: tuple[list[int], list[int]] | None = None
    """Raw-tick (min, max) per motor from the calibration sweep; teleop clamps goals to it."""
    last_calibrated: list[float] | None = None
    """Calibrated positions from the most recent successful read (this frame's, once read)."""


@dataclass
class _ResolvedArm:
    port: str
    name: str
    calibration: list[MotorCalibration]
    is_leader: bool | None
    calibrated: bool
    ranges: tuple[list[int], list[int]] | None


@dataclass
class LogArmsConfig:
    rr_config: LiveViewerConfig = field(default_factory=LiveViewerConfig)
    ports: tuple[str, ...] = ()
    """Serial ports of the arms. Default: every /dev/cu.usbmodem* found."""
    names: tuple[str, ...] = ()
    """Entity names for the arms, same order as --ports. Default: the USB serial id."""
    calibration_dir: Path = Path("calibrations")
    """Directory of <usb_id>.json calibrations (portugal format). Missing files fall back to raw-centered degrees."""
    fps: float = 30.0
    """Target bus poll rate per arm."""
    seconds: float | None = None
    """Stop after N seconds. Default: run until Ctrl-C."""
    window_seconds: float = 10.0
    """Width of the sliding time window in the default blueprint views."""
    urdf: bool = True
    """Animate a URDF per arm from the live joint positions."""
    leader: str | None = None
    """USB id (or --names name) of the leader arm. Default: read from the calibration JSON's "kind" (written by calibrate-so100 --leader)."""
    arm_spacing: float = 0.4
    """Distance (m) between the URDF arms. Arms face +x; the leader sits at the origin
    and each next arm goes to its right (-y, i.e. left-to-right as seen from behind).
    Negative flips the side."""
    joint_offsets_deg: tuple[float, float, float, float, float, float] | None = None
    """URDF joint angles (deg) at calibrated zero. Default: midpoint of each joint's limits (the calibration reference pose)."""
    cameras: tuple[int, ...] | None = None
    """Camera indices to stream. Default: probe indices 0-4 and use whatever responds. Pass --cameras (empty) to disable."""
    jpeg_quality: int = 75
    """JPEG quality for camera frames."""
    teleop: bool = False
    """Drive the follower to mirror the leader (torque ON on the follower). Leader positions
    pass through both arms' calibrations, so both arms must be calibrated. Goals are clamped
    to the follower's swept range; Ctrl-C (or any exit) releases the follower's torque."""
    max_relative_target: float | None = None
    """Teleop safety clamp: max change of any follower goal per tick, in calibrated units
    (degrees; % for the gripper). Default: no clamp, like lerobot."""


def _open_arms(config: LogArmsConfig, rec: rr.RecordingStream) -> list[Arm]:
    ports = config.ports or detect_arm_ports()
    if not ports:
        raise SystemExit("no SO-100 arms found (no /dev/cu.usbmodem* ports); pass --ports explicitly")

    # Resolve each port's name, calibration, and leader/follower kind first, so the
    # leader can be placed on the left (first position) regardless of port order.
    resolved: list[_ResolvedArm] = []
    for i, port in enumerate(ports):
        usb_id = usb_id_from_port(port)
        name = config.names[i] if i < len(config.names) else usb_id
        calibration_path = config.calibration_dir / f"{usb_id}.json"
        try:
            calibration = load_calibration(calibration_path)
            calibrated = True
            print(f"{name}: {port} (calibration {calibration_path})", flush=True)
        except (FileNotFoundError, KeyError):  # missing, or a kind-only stub
            calibration = fallback_calibration()
            calibrated = False
            print(f"{name}: {port} (no calibration in {calibration_path}, using raw-centered degrees)", flush=True)
        if config.leader is not None:
            is_leader: bool | None = config.leader in (usb_id, name)
        else:
            kind = load_arm_kind(calibration_path)
            is_leader = None if kind is None else kind == "leader"
        resolved.append(
            _ResolvedArm(
                port=port,
                name=name,
                calibration=calibration,
                is_leader=is_leader,
                calibrated=calibrated,
                ranges=load_arm_ranges(calibration_path),
            )
        )

    # A two-arm rig is one leader + one follower: fill in whichever is unidentified.
    if len(resolved) == 2:
        unknown = [arm for arm in resolved if arm.is_leader is None]
        if len(unknown) == 1:
            known = next(arm for arm in resolved if arm.is_leader is not None)
            unknown[0].is_leader = not known.is_leader
        elif len(unknown) == 2:
            resolved[0].is_leader = True
            resolved[1].is_leader = False
            print(
                f"GUESSING {resolved[0].name} is the leader — pass --leader <usb_id> if wrong, or calibrate with --leader to make it permanent",
                flush=True,
            )
    resolved.sort(key=lambda arm: not arm.is_leader)  # leader first -> leftmost
    for arm in resolved:
        print(f"{arm.name}: {'LEADER' if arm.is_leader else 'follower'}", flush=True)

    arms: list[Arm] = []
    for i, res in enumerate(resolved):
        urdf = None
        if config.urdf:
            urdf = UrdfArm.create(
                res.name,
                res.calibration,
                rec=rec,
                urdf_path=LEADER_URDF_PATH if res.is_leader else FOLLOWER_URDF_PATH,
                translation=(0.0, -i * config.arm_spacing, 0.0),
                center_angles_deg=config.joint_offsets_deg,
                color=MATTE_BLACK,
            )
        arms.append(
            Arm(
                name=res.name,
                bus=FeetechBus(res.port),
                calibration=res.calibration,
                urdf=urdf,
                is_leader=bool(res.is_leader),
                calibrated=res.calibrated,
                ranges=res.ranges,
            )
        )
    return arms


def _log_arm(arm: Arm, rec: rr.RecordingStream) -> None:
    telemetry: list[MotorTelemetry] = arm.bus.read_telemetry()
    calibrated = [calib.calibrated_from_raw(t.position_raw) for calib, t in zip(arm.calibration, telemetry, strict=True)]
    arm.last_calibrated = calibrated
    rec.log(f"{arm.name}/position", rr.Scalars(calibrated))
    for subpath, attr in METRICS.items():
        rec.log(f"{arm.name}/{subpath}", rr.Scalars([float(getattr(t, attr)) for t in telemetry]))
    if arm.urdf is not None:
        arm.urdf.log_joints(rec, calibrated)


def _drive_follower(leader: Arm, follower: Arm, *, rec: rr.RecordingStream, blend: float, max_step: float | None) -> None:
    """Mirror the leader: leader calibrated values -> follower raw ticks -> Goal_Position.

    ``blend`` < 1 eases the follower from its own pose toward the leader's (startup ramp);
    ``max_step`` caps the per-tick goal change (lerobot's max_relative_target). Both work in
    calibrated space, where the two arms agree by construction.
    """
    assert leader.last_calibrated is not None and follower.last_calibrated is not None and follower.ranges is not None
    range_min, range_max = follower.ranges
    goals: list[int] = []
    goals_calibrated: list[float] = []
    for i, calib in enumerate(follower.calibration):
        present = follower.last_calibrated[i]
        target = present + blend * (leader.last_calibrated[i] - present)
        if max_step is not None:
            target = min(max(target, present - max_step), present + max_step)
        raw = min(max(calib.raw_from_calibrated(target), range_min[i]), range_max[i])
        goals.append(raw)
        goals_calibrated.append(calib.calibrated_from_raw(raw))
    follower.bus.sync_write_goal(goals)
    rec.log(f"{follower.name}/goal", rr.Scalars(goals_calibrated))


class ArmSession:
    """Always-on arm + camera logging (optionally with teleop), for the dataset collector.

    Unlike ``main`` (which owns the viewer and its own loop), this opens the hardware once
    and, on :meth:`start`, runs its own background loop that reads every arm, optionally
    drives the follower to mirror the leader (teleop), and logs a frame -- continuously, into
    :attr:`rec`. :meth:`begin` swaps :attr:`rec` (and the sinks are swapped alongside), which
    is how the collector starts/stops *takes* without interrupting teleop or the live view.

    * :meth:`start` — arm the follower (if teleop) and start the camera + logging threads.
      Call once a log sink is attached.
    * :meth:`begin` — redirect the session (and its cameras) to ``rec``, then (re-)log the
      static per-motor series, URDF meshes, and blueprint into it; called for each new take.
    * :meth:`close` — stop the threads, release the follower's torque, close the buses.
    """

    def __init__(self, config: LogArmsConfig, rec: rr.RecordingStream) -> None:
        self.config = config
        self.rec = rec
        self.arms = _open_arms(config, rec)
        camera_indices = detect_camera_indices() if config.cameras is None else config.cameras
        self.streamers = [CameraStreamer(index, rec=rec, jpeg_quality=config.jpeg_quality) for index in camera_indices]
        self._reconnect_every = max(1, int(config.fps))  # attempt a reconnect roughly once a second
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._teleop_t0: float | None = None  # ramp anchor; None whenever driving is paused
        self._write_errors = 0

        # Teleop: resolve the leader/follower pair, degrading gracefully if the rig can't do it.
        self._leader: Arm | None = None
        self._follower: Arm | None = None
        if config.teleop:
            leader = next((arm for arm in self.arms if arm.is_leader), None)
            follower = next((arm for arm in self.arms if not arm.is_leader), None)
            if (
                len(self.arms) == 2
                and leader is not None
                and follower is not None
                and leader.calibrated
                and follower.calibrated
                and follower.ranges is not None
            ):
                self._leader, self._follower = leader, follower
            else:
                print("teleop requested but needs two calibrated arms (with a follower range) — running WITHOUT teleop", flush=True)

        teleop_note = ""
        if self._leader is not None and self._follower is not None:
            teleop_note = f", teleop {self._follower.name} <- {self._leader.name}"
        print(f"arm session: {len(self.arms)} arm(s) + {len(self.streamers)} camera(s){teleop_note}", flush=True)

    @property
    def fps(self) -> float:
        return self.config.fps

    @property
    def leader(self) -> Arm | None:
        """The teleop leader arm, or ``None`` if teleop isn't running."""
        return self._leader

    @property
    def follower(self) -> Arm | None:
        """The teleop follower arm, or ``None`` if teleop isn't running."""
        return self._follower

    def start(self) -> None:
        """Arm the follower (if teleop) and start the camera + logging threads."""
        for streamer in self.streamers:
            streamer.start()
        if self._follower is not None and self._leader is not None:
            self._follower.bus.set_torque(False)
            self._follower.bus.configure_follower_control()
            self._follower.bus.set_torque(True)
            print(f"teleop: {self._follower.name} torque ON, mirroring {self._leader.name}", flush=True)
        self._thread = threading.Thread(target=self._run, name="arm-session", daemon=True)
        self._thread.start()

    def begin(self, rec: rr.RecordingStream) -> None:
        # Redirect this session's loop and its camera threads into rec (they snapshot it per frame).
        self.rec = rec
        for streamer in self.streamers:
            streamer.rec = rec
        for arm in self.arms:
            motor_names = [calib.motor_name for calib in arm.calibration]
            for subpath in METRIC_SUBPATHS:
                rec.log(f"{arm.name}/{subpath}", rr.SeriesLines(names=motor_names), static=True)
            if arm.urdf is not None:
                arm.urdf.log_static(rec)  # re-log the meshes into this take's recording
        if self._follower is not None:
            goal_names = [f"{calib.motor_name} goal" for calib in self._follower.calibration]
            rec.log(f"{self._follower.name}/goal", rr.SeriesLines(names=goal_names), static=True)
        rec.send_blueprint(
            create_blueprint(
                [arm.name for arm in self.arms],
                camera_paths=[streamer.entity_path for streamer in self.streamers],
                collision_paths=[arm.urdf.collision_geometries_path for arm in self.arms if arm.urdf is not None],
                show_urdf=self.config.urdf,
                window_seconds=self.config.window_seconds,
            ),
            make_active=True,
        )

    def _run(self) -> None:
        frame_time = 1.0 / self.config.fps
        while not self._stop.is_set():
            loop_start = time.monotonic()
            rec = self.rec  # snapshot: the collector may swap it between frames
            rec.set_time("time", timestamp=time.time())
            for arm in self.arms:
                # Tolerate transient bus glitches per arm, so one unplugged arm never stalls the others.
                try:
                    recovering = arm.errors > 0
                    _log_arm(arm, rec)
                    arm.errors = 0
                    if recovering and arm is self._follower:
                        # A servo power blip resets Torque_Enable, and goal writes are
                        # fire-and-forget — re-arm, or teleop resumes silently limp.
                        arm.bus.set_torque(True)
                        print(f"{arm.name}: recovered — torque re-armed", flush=True)
                except RuntimeError as error:
                    arm.errors += 1
                    if arm.errors == 1 or arm.errors % self._reconnect_every == 0:
                        print(f"{arm.name}: bus read failed ({arm.errors}): {error}", flush=True)
                    if arm.errors % self._reconnect_every == 0:
                        try:
                            arm.bus.reconnect()
                            print(f"{arm.name}: reconnected", flush=True)
                        except (RuntimeError, OSError):
                            pass  # device still gone; keep retrying

            if self._leader is not None and self._follower is not None:
                if self._leader.errors == 0 and self._follower.errors == 0:
                    if self._teleop_t0 is None:  # first drive, or resuming after a dropout
                        self._teleop_t0 = loop_start
                    blend = min(1.0, (loop_start - self._teleop_t0) / TELEOP_RAMP_SECONDS)
                    try:
                        _drive_follower(self._leader, self._follower, rec=rec, blend=blend, max_step=self.config.max_relative_target)
                        self._write_errors = 0
                    except RuntimeError as error:
                        self._write_errors += 1
                        if self._write_errors == 1 or self._write_errors % self._reconnect_every == 0:
                            print(f"{self._follower.name}: goal write failed ({self._write_errors}): {error}", flush=True)
                else:
                    self._teleop_t0 = None  # a bus dropped: restart the ramp on resume

            sleep_s = frame_time - (time.monotonic() - loop_start)
            if sleep_s > 0.0:
                time.sleep(sleep_s)

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        for streamer in self.streamers:
            streamer.stop()
        if self._follower is not None:
            try:
                self._follower.bus.set_torque(False)
                print(f"{self._follower.name}: torque released", flush=True)
            except (RuntimeError, OSError) as error:
                print(f"{self._follower.name}: FAILED to release torque ({error}) — power-cycle the arm to free it", flush=True)
        for arm in self.arms:
            arm.bus.close()


def main(config: LogArmsConfig) -> None:
    rec = config.rr_config.rec
    arms = _open_arms(config, rec)

    # Name the per-motor series once, statically, so plot legends show joint names.
    for arm in arms:
        motor_names = [calib.motor_name for calib in arm.calibration]
        for subpath in METRIC_SUBPATHS:
            rec.log(f"{arm.name}/{subpath}", rr.SeriesLines(names=motor_names), static=True)

    leader_arm: Arm | None = None
    follower_arm: Arm | None = None
    if config.teleop:
        leader_arm = next((arm for arm in arms if arm.is_leader), None)
        follower_arm = next((arm for arm in arms if not arm.is_leader), None)
        if len(arms) != 2 or leader_arm is None or follower_arm is None:
            raise SystemExit("teleop needs exactly one leader and one follower arm plugged in")
        if not (leader_arm.calibrated and follower_arm.calibrated):
            raise SystemExit("teleop needs both arms calibrated (pixi run calibrate-so100 leader / follower)")
        if follower_arm.ranges is None:
            raise SystemExit(
                f"teleop clamps goals to the follower's swept range, but {follower_arm.name}'s calibration "
                "has no range_min/range_max — re-run: pixi run calibrate-so100 follower"
            )
        goal_names = [f"{calib.motor_name} goal" for calib in follower_arm.calibration]
        rec.log(f"{follower_arm.name}/goal", rr.SeriesLines(names=goal_names), static=True)

    camera_indices = detect_camera_indices() if config.cameras is None else config.cameras
    streamers = [CameraStreamer(index, rec=rec, jpeg_quality=config.jpeg_quality) for index in camera_indices]
    for streamer in streamers:
        streamer.start()

    rec.send_blueprint(
        create_blueprint(
            [arm.name for arm in arms],
            camera_paths=[streamer.entity_path for streamer in streamers],
            collision_paths=[arm.urdf.collision_geometries_path for arm in arms if arm.urdf is not None],
            show_urdf=config.urdf,
            window_seconds=config.window_seconds,
        ),
        make_active=True,
    )

    frame_time = 1.0 / config.fps
    deadline: float | None = None if config.seconds is None else time.monotonic() + config.seconds
    frames = 0
    rate_t0 = time.monotonic()
    max_errors = int(30.0 * config.fps)  # give up after ~30s of continuous failure
    reconnect_every = max(1, int(config.fps))  # attempt a reconnect roughly once a second
    write_errors = 0
    teleop_t0: float | None = None  # ramp anchor; None whenever driving is paused, so resuming glides again
    print(f"streaming {len(arms)} arm(s) + {len(streamers)} camera(s) at target {config.fps:.0f} Hz (Ctrl-C to stop)...", flush=True)
    try:
        if leader_arm is not None and follower_arm is not None:
            # Arm the follower inside try/finally (and after the slow camera probing above),
            # so any exit from here on — including Ctrl-C — releases its torque.
            follower_arm.bus.set_torque(False)
            follower_arm.bus.configure_follower_control()
            follower_arm.bus.set_torque(True)
            print(f"teleop: {follower_arm.name} torque ON, mirroring {leader_arm.name} — Ctrl-C releases it", flush=True)
        while True:
            loop_start = time.monotonic()
            rec.set_time("time", timestamp=time.time())
            for arm in arms:
                # Tolerate transient bus glitches (bad packets, USB drops) per arm, so one
                # unplugged arm never stalls the others; frame pacing spaces the retries.
                try:
                    recovering = arm.errors > 0
                    _log_arm(arm, rec)
                    arm.errors = 0
                    if recovering and arm is follower_arm:
                        # A servo power blip resets Torque_Enable, and goal writes are
                        # fire-and-forget — re-arm, or teleop resumes silently limp.
                        arm.bus.set_torque(True)
                        print(f"{arm.name}: recovered — torque re-armed", flush=True)
                except RuntimeError as error:
                    arm.errors += 1
                    if arm.errors == 1 or arm.errors % reconnect_every == 0:
                        print(f"{arm.name}: bus read failed ({arm.errors}): {error}", flush=True)
                    if arm.errors >= max_errors:
                        raise
                    if arm.errors % reconnect_every == 0:
                        try:
                            arm.bus.reconnect()
                            print(f"{arm.name}: reconnected", flush=True)
                        except (RuntimeError, OSError):
                            pass  # device still gone; keep retrying

            # Both reads fresh this frame -> send the follower after the leader. A failed
            # read skips the write, so the follower just holds its last goal (torque stays on).
            if leader_arm is not None and follower_arm is not None:
                if leader_arm.errors == 0 and follower_arm.errors == 0:
                    if teleop_t0 is None:  # first drive, or resuming after a dropout
                        teleop_t0 = loop_start
                    blend = min(1.0, (loop_start - teleop_t0) / TELEOP_RAMP_SECONDS)
                    try:
                        _drive_follower(leader_arm, follower_arm, rec=rec, blend=blend, max_step=config.max_relative_target)
                        write_errors = 0
                    except RuntimeError as error:
                        write_errors += 1
                        if write_errors == 1 or write_errors % reconnect_every == 0:
                            print(f"{follower_arm.name}: goal write failed ({write_errors}): {error}", flush=True)
                else:
                    # Driving paused (a bus dropped): restart the ramp on resume, so the
                    # follower glides to wherever the leader moved meanwhile, not snaps.
                    teleop_t0 = None

            frames += 1
            if loop_start - rate_t0 >= 5.0:
                print(f"logging at {frames / (loop_start - rate_t0):.1f} Hz", flush=True)
                frames = 0
                rate_t0 = loop_start
            if deadline is not None and loop_start >= deadline:
                break
            sleep_s = frame_time - (time.monotonic() - loop_start)
            if sleep_s > 0.0:
                time.sleep(sleep_s)
    except KeyboardInterrupt:
        print("\nstopped.", flush=True)
    finally:
        for streamer in streamers:
            streamer.stop()
        if follower_arm is not None:
            try:
                follower_arm.bus.set_torque(False)
                print(f"{follower_arm.name}: torque released", flush=True)
            except (RuntimeError, OSError) as error:
                print(f"{follower_arm.name}: FAILED to release torque ({error}) — power-cycle the arm to free it", flush=True)
        for arm in arms:
            arm.bus.close()
