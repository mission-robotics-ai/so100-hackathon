"""Replay a recorded episode on the follower arm (the Deploy step).

Queries the episode's action trajectory (the ``<follower>/goal`` series) from the
catalog served by ``pixi run so100-server``, ramps the follower gently to the starting
pose, then plays the trajectory back at recorded speed::

    pixi run replay-episode -- --dataset my_task --episode episode_01
    pixi run replay-episode -- --dataset my_task --episode episode_01 --speed 0.5

The follower must be plugged in and calibrated; the leader is not needed. If the
long-lived server currently holds the arms (Collect page), disconnect them there first --
the serial port is exclusive. Replayed joints stream to the server's live proxy, so the
embedded viewer (or any viewer watching it) shows the replay. Torque is released on exit.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import time
from pathlib import Path

import numpy as np
import tyro

os.environ.setdefault("RERUN_INSECURE_SKIP_HOST_CHECK", "1")

import rerun as rr  # noqa: E402 - the env var above must be set before use

from so100_hackathon.calibration import MotorCalibration, load_arm_kind, load_arm_ranges, load_calibration  # noqa: E402
from so100_hackathon.feetech import FeetechBus, detect_arm_ports, usb_id_from_port  # noqa: E402
from so100_hackathon.takes import APP_ID  # noqa: E402


def load_trajectory(dataset: rr.catalog.DatasetEntry, episode: str) -> tuple[np.ndarray, np.ndarray, str]:
    """The episode's action series: (times_s, goals[N, 6], goal_entity_path)."""
    goal_paths = [str(path) for path in dataset.schema().entity_paths() if str(path).endswith("/goal")]
    if not goal_paths:
        raise SystemExit("no '<arm>/goal' entity in this dataset -- was it recorded with teleop?")
    goal_path = goal_paths[0]

    table = dataset.segment_table().to_pandas()
    names = table["property:RecordingInfo:name"].map(lambda v: v[0] if len(v) else "")
    segments = table.loc[names == episode, "rerun_segment_id"].tolist()
    if not segments:
        raise SystemExit(f"no episode named '{episode}' in dataset '{dataset.name}' (see `pixi run query-dataset -- --dataset {dataset.name}`)")

    df = dataset.filter_segments(segments).filter_contents([goal_path]).reader(index="time").to_pandas()
    column = next((name for name in df.columns if name.endswith(":Scalars:scalars")), None)
    if column is None:
        raise SystemExit(f"'{goal_path}' has no scalar data in episode '{episode}'")
    df = df[df[column].notna()]
    times = df["time"].astype("int64").to_numpy() / 1e9
    goals = np.stack(list(df[column])).astype(np.float64)
    return times, goals, goal_path


@dataclasses.dataclass
class Follower:
    name: str
    bus: FeetechBus
    calibration: list[MotorCalibration]
    range_min: list[int]
    range_max: list[int]


def open_follower(calibration_dir: Path, port: str | None) -> Follower:
    """Open the (calibrated) follower arm, identified by the "kind" in its calibration JSON."""
    ports = (port,) if port else detect_arm_ports()
    if not ports:
        raise SystemExit("no SO-100 arms found (no /dev/cu.usbmodem* ports); pass --port explicitly")
    candidates = []
    for candidate in ports:
        usb_id = usb_id_from_port(candidate)
        calibration_path = calibration_dir / f"{usb_id}.json"
        kind = load_arm_kind(calibration_path)
        if kind == "leader" and port is None:
            continue
        try:
            calibration = load_calibration(calibration_path)
        except (FileNotFoundError, KeyError):
            continue
        ranges = load_arm_ranges(calibration_path)
        if ranges is None:
            raise SystemExit(f"{usb_id}: calibration has no range_min/range_max -- re-run: pixi run calibrate-so100 follower")
        candidates.append((candidate, usb_id, calibration, ranges))
    if not candidates:
        raise SystemExit("no calibrated follower arm found (pixi run calibrate-so100 follower)")
    if len(candidates) > 1:
        raise SystemExit(f"multiple follower candidates ({', '.join(c[1] for c in candidates)}); pass --port to pick one")
    chosen_port, usb_id, calibration, (range_min, range_max) = candidates[0]
    print(f"follower: {usb_id} on {chosen_port}")
    return Follower(name=usb_id, bus=FeetechBus(chosen_port), calibration=calibration, range_min=range_min, range_max=range_max)


def read_calibrated(follower: Follower) -> list[float]:
    telemetry = follower.bus.read_telemetry()
    return [calib.calibrated_from_raw(t.position_raw) for calib, t in zip(follower.calibration, telemetry, strict=True)]


def drive_to(follower: Follower, target: np.ndarray, rec: rr.RecordingStream) -> None:
    """Write one clamped goal frame and log commanded + measured joints."""
    goals_raw: list[int] = []
    goals_calibrated: list[float] = []
    for i, calib in enumerate(follower.calibration):
        raw = min(max(calib.raw_from_calibrated(float(target[i])), follower.range_min[i]), follower.range_max[i])
        goals_raw.append(raw)
        goals_calibrated.append(calib.calibrated_from_raw(raw))
    follower.bus.sync_write_goal(goals_raw)
    rec.set_time("time", timestamp=time.time())
    rec.log(f"{follower.name}/goal", rr.Scalars(goals_calibrated))
    with contextlib.suppress(RuntimeError):  # a dropped read shouldn't interrupt playback
        rec.log(f"{follower.name}/position", rr.Scalars(read_calibrated(follower)))


@dataclasses.dataclass
class Config:
    dataset: str
    """Catalog dataset holding the episode."""

    episode: str
    """Episode name to replay (as shown by `pixi run query-dataset -- --dataset <name>`)."""

    speed: float = 1.0
    """Playback speed multiplier (0.5 = half speed). Keep it <= 1 on the first run."""

    ramp_seconds: float = 2.0
    """How long the follower takes to glide to the trajectory's starting pose."""

    port: str | None = None
    """Serial port of the follower. Default: the plugged-in arm whose calibration says "follower"."""

    calibration_dir: Path = Path("calibrations")
    """Directory of <usb_id>.json calibrations."""

    catalog_port: int = 51234
    """so100-server catalog port."""

    proxy_port: int = 9876
    """so100-server live proxy port (the replay streams there for the viewer)."""


def main(config: Config) -> None:
    if not 0.0 < config.speed <= 2.0:
        raise SystemExit("--speed must be in (0, 2]")

    client = rr.catalog.CatalogClient(f"rerun+http://localhost:{config.catalog_port}")
    dataset = client.get_dataset(name=config.dataset)
    times, goals, goal_path = load_trajectory(dataset, config.episode)
    duration = (times[-1] - times[0]) / config.speed
    print(f"replaying '{config.episode}': {len(goals)} steps over {duration:.1f}s (from {goal_path})")

    follower = open_follower(config.calibration_dir, config.port)

    rec = rr.RecordingStream(APP_ID, recording_id=f"replay-{config.dataset}-{config.episode}")
    rec.connect_grpc(url=f"rerun+http://localhost:{config.proxy_port}/proxy")
    motor_names = [calib.motor_name for calib in follower.calibration]
    rec.log(f"{follower.name}/goal", rr.SeriesLines(names=[f"{name} goal" for name in motor_names]), static=True)
    rec.log(f"{follower.name}/position", rr.SeriesLines(names=motor_names), static=True)

    try:
        follower.bus.set_torque(False)
        follower.bus.configure_follower_control()
        follower.bus.set_torque(True)
        print("torque ON -- keep a hand near the arm; Ctrl-C stops and releases it")

        # Glide from wherever the arm is to the trajectory's first pose.
        start_pose = np.asarray(read_calibrated(follower))
        ramp_steps = max(2, int(config.ramp_seconds * 30))
        for step in range(ramp_steps):
            blend = (step + 1) / ramp_steps
            drive_to(follower, start_pose + blend * (goals[0] - start_pose), rec)
            time.sleep(config.ramp_seconds / ramp_steps)

        # Play the trajectory on its own recorded clock, scaled by --speed.
        wall_start = time.monotonic()
        for i in range(len(goals)):
            target_wall = wall_start + (times[i] - times[0]) / config.speed
            sleep_s = target_wall - time.monotonic()
            if sleep_s > 0:
                time.sleep(sleep_s)
            drive_to(follower, goals[i], rec)
        print("replay finished")
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        try:
            follower.bus.set_torque(False)
            print("torque released")
        except (RuntimeError, OSError) as error:
            print(f"FAILED to release torque ({error}) -- power-cycle the arm to free it")
        follower.bus.close()


if __name__ == "__main__":
    try:
        main(tyro.cli(Config))
    except ConnectionError as error:
        raise SystemExit(f"cannot reach the catalog -- is `pixi run so100-server` running? ({error})") from None
