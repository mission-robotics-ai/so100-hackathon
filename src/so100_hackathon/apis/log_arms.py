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

import time
from dataclasses import dataclass, field
from pathlib import Path

import rerun as rr

from so100_hackathon.blueprint import create_blueprint
from so100_hackathon.calibration import MotorCalibration, fallback_calibration, load_arm_kind, load_calibration
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


@dataclass
class Arm:
    name: str
    bus: FeetechBus
    calibration: list[MotorCalibration]
    urdf: UrdfArm | None = None
    errors: int = 0
    """Consecutive bus-read failures (reset on the first successful read)."""


@dataclass
class _ResolvedArm:
    port: str
    name: str
    calibration: list[MotorCalibration]
    is_leader: bool | None


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


def _open_arms(config: LogArmsConfig) -> list[Arm]:
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
            print(f"{name}: {port} (calibration {calibration_path})", flush=True)
        except (FileNotFoundError, KeyError):  # missing, or a kind-only stub
            calibration = fallback_calibration()
            print(f"{name}: {port} (no calibration in {calibration_path}, using raw-centered degrees)", flush=True)
        if config.leader is not None:
            is_leader: bool | None = config.leader in (usb_id, name)
        else:
            kind = load_arm_kind(calibration_path)
            is_leader = None if kind is None else kind == "leader"
        resolved.append(_ResolvedArm(port=port, name=name, calibration=calibration, is_leader=is_leader))

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
                f"GUESSING {resolved[0].name} is the leader — pass --leader <usb_id> if wrong, "
                "or calibrate with --leader to make it permanent",
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
                urdf_path=LEADER_URDF_PATH if res.is_leader else FOLLOWER_URDF_PATH,
                translation=(0.0, -i * config.arm_spacing, 0.0),
                center_angles_deg=config.joint_offsets_deg,
                color=MATTE_BLACK,
            )
        arms.append(Arm(name=res.name, bus=FeetechBus(res.port), calibration=res.calibration, urdf=urdf))
    return arms


def _log_arm(arm: Arm) -> None:
    telemetry: list[MotorTelemetry] = arm.bus.read_telemetry()
    calibrated = [calib.calibrated_from_raw(t.position_raw) for calib, t in zip(arm.calibration, telemetry, strict=True)]
    rr.log(f"{arm.name}/position", rr.Scalars(calibrated))
    for subpath, attr in METRICS.items():
        rr.log(f"{arm.name}/{subpath}", rr.Scalars([float(getattr(t, attr)) for t in telemetry]))
    if arm.urdf is not None:
        arm.urdf.log_joints(calibrated)


def main(config: LogArmsConfig) -> None:
    arms = _open_arms(config)

    # Name the per-motor series once, statically, so plot legends show joint names.
    for arm in arms:
        motor_names = [calib.motor_name for calib in arm.calibration]
        for subpath in METRIC_SUBPATHS:
            rr.log(f"{arm.name}/{subpath}", rr.SeriesLines(names=motor_names), static=True)

    camera_indices = detect_camera_indices() if config.cameras is None else config.cameras
    streamers = [CameraStreamer(index, jpeg_quality=config.jpeg_quality) for index in camera_indices]
    for streamer in streamers:
        streamer.start()

    rr.send_blueprint(
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
    print(f"streaming {len(arms)} arm(s) + {len(streamers)} camera(s) at target {config.fps:.0f} Hz (Ctrl-C to stop)...", flush=True)
    try:
        while True:
            loop_start = time.monotonic()
            rr.set_time("time", timestamp=time.time())
            for arm in arms:
                # Tolerate transient bus glitches (bad packets, USB drops) per arm, so one
                # unplugged arm never stalls the others; frame pacing spaces the retries.
                try:
                    _log_arm(arm)
                    arm.errors = 0
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
        for arm in arms:
            arm.bus.close()
