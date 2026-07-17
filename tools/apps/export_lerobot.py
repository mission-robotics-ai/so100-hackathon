"""Export a recorded catalog dataset to LeRobot v3 format (the Train step).

Runs in the *default* environment (rerun-sdk 0.34.1) and stages episodes to a temp
directory, then spawns ``tools/apps/_export_lerobot_writer.py`` inside the isolated
``export`` environment (which has ``lerobot`` -- its rerun-sdk pin conflicts with ours,
so the two never share an interpreter)::

    pixi run export-lerobot -- --dataset my_task --repo-id <hf-user>/my_task
    pixi run export-lerobot -- --dataset my_task --repo-id <hf-user>/my_task --push

Only episodes tagged "Good episode" are exported by default (``--tag ""`` for all).
Output lands in ``datasets/<repo-id>/``; ``--push`` uploads to the Hugging Face Hub
(login first with ``pixi run -e export hf auth login``).

Units note (important for training): recordings store joints in OUR calibrated degrees
(middle pose = 0 deg), but the SO-100/101 training ecosystem — the pooled community data,
the ``allenai/MolmoAct2-SO100_101`` base checkpoint, and deployment clients built on
lerobot's ``SO101Follower`` with its default ``use_degrees=False`` (e.g. the
newt-starter-so101) — exchanges joints in lerobot's NORMALIZED units: each arm joint's
calibrated range mapped to [-100, 100], gripper [0, 100]. A model fine-tuned on degrees
would command values a lerobot-driven arm misreads as +-100 units -> wrong poses. So by
default the export converts degrees -> +-100 using the follower's calibration
(``calibrations/<usb_id>.json``, the same ranges dual-written for those clients);
``--units degrees`` keeps raw degrees for stacks that expect them.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Literal

import numpy as np
import tyro

os.environ.setdefault("RERUN_INSECURE_SKIP_HOST_CHECK", "1")

import rerun as rr  # noqa: E402 - the env var above must be set before use

from so100_hackathon.calibration import DEFAULT_MOTOR_NAMES, load_arm_kind, load_arm_ranges, load_calibration  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _flatten(value: Any) -> Any:
    if isinstance(value, str) or value is None:
        return value
    try:
        return value[0] if len(value) else None
    except TypeError:
        return value


@dataclasses.dataclass
class Episode:
    segment_id: str
    name: str
    task: str


def select_episodes(dataset: rr.catalog.DatasetEntry, tag: str) -> list[Episode]:
    """Episodes to export, with their task as the LeRobot task string."""
    table = dataset.segment_table().to_pandas()
    episodes = []
    for _, row in table.iterrows():
        row_tag = _flatten(row.get("property:episode:tag"))
        if tag and row_tag != tag:
            continue
        name = _flatten(row.get("property:RecordingInfo:name")) or row["rerun_segment_id"]
        task = _flatten(row.get("property:episode:task")) or ""
        episodes.append(Episode(segment_id=row["rerun_segment_id"], name=str(name), task=str(task)))
    return sorted(episodes, key=lambda episode: episode.name)


def discover_entities(dataset: rr.catalog.DatasetEntry) -> tuple[str, str, list[str]]:
    """Find (action, state, cameras) entity paths in the dataset schema."""
    paths = [str(path) for path in dataset.schema().entity_paths()]
    goals = [path for path in paths if path.endswith("/goal")]
    if not goals:
        raise SystemExit(f"no '<arm>/goal' entity found -- was this recorded with teleop? entities: {', '.join(paths)}")
    action = goals[0]
    state = action.removesuffix("/goal") + "/position"
    if state not in paths:
        raise SystemExit(f"found action entity '{action}' but no matching '{state}'")
    cameras = sorted(path for path in paths if re.fullmatch(r".*/cam\d+", path))
    return action, state, cameras


class LerobotNormalizer:
    """Convert our calibrated units (degrees, gripper %) to lerobot's normalized wire units.

    Per DEGREE joint: degrees -> raw ticks (inverse of our calibration mapping) -> the
    joint's swept [range_min, range_max] mapped linearly to [-100, 100]. That is exactly
    what lerobot's ``MotorNormMode.RANGE_M100_100`` does at read time, so a model trained
    on these values speaks the same language as an arm driven through ``SO101Follower``.
    LINEAR joints (the gripper) pass through: our 0-100% over the swept range IS
    lerobot's ``RANGE_0_100``.
    """

    def __init__(self, calibration_path: Path) -> None:
        self.path = calibration_path
        self.calibration = load_calibration(calibration_path)
        ranges = load_arm_ranges(calibration_path)
        if ranges is None:
            raise SystemExit(f"{calibration_path} has no range-of-motion sweep (too old?) -- re-run `pixi run calibrate-so100 follower`")
        self.range_min, self.range_max = ranges

    def __call__(self, values: np.ndarray) -> np.ndarray:
        if values.shape[1] != len(self.calibration):
            raise SystemExit(f"cannot normalize: {values.shape[1]} joints in the recording vs {len(self.calibration)} in {self.path}")
        out = np.empty_like(values)
        for i, calib in enumerate(self.calibration):
            if calib.calib_mode == "LINEAR":
                out[:, i] = values[:, i]
                continue
            raw = values[:, i] / 180.0 * float(calib.end_pos - calib.start_pos) + float(calib.start_pos)
            out[:, i] = (raw - float(self.range_min[i])) / float(self.range_max[i] - self.range_min[i]) * 200.0 - 100.0
        return out


def find_follower_calibration(calibration_dir: Path) -> Path:
    followers = sorted(path for path in calibration_dir.glob("*.json") if load_arm_kind(path) == "follower")
    if not followers:
        raise SystemExit(f"no follower calibration in {calibration_dir}/ -- run `pixi run calibrate-so100 follower`, or export with --units degrees")
    if len(followers) > 1:
        raise SystemExit(
            f"several follower calibrations in {calibration_dir}/ ({', '.join(p.name for p in followers)}) -- pick one with --calibration"
        )
    return followers[0]


def _column(df, entity: str, suffix: str) -> str:
    matches = [name for name in df.columns if name.endswith(suffix) and entity in name]
    if not matches:
        raise SystemExit(f"column '{entity}{suffix}' missing from the query result (got: {list(df.columns)})")
    return matches[0]


def stage_episode(
    dataset: rr.catalog.DatasetEntry,
    episode: Episode,
    action: str,
    state: str,
    cameras: list[str],
    camera_keys: dict[str, str],
    normalize: LerobotNormalizer | None,
    out_dir: Path,
) -> int:
    """Query one episode and write action/state arrays + camera JPEGs to out_dir."""
    contents = [action, state, *cameras]
    df = dataset.filter_segments(episode.segment_id).filter_contents(contents).reader(index="time", fill_latest_at=True).to_pandas()

    action_col = _column(df, action, ":Scalars:scalars")
    state_col = _column(df, state, ":Scalars:scalars")
    camera_cols = {camera: _column(df, camera, ":EncodedImage:blob") for camera in cameras}

    # One output frame per action row; fill_latest_at already carried the most recent
    # state/camera values forward. Drop leading rows where anything is still missing.
    keep = df[action_col].notna()
    for column in (state_col, *camera_cols.values()):
        keep &= df[column].notna()
    df = df[keep]
    if df.empty:
        return 0

    out_dir.mkdir(parents=True)
    action_values = np.stack(list(df[action_col])).astype(np.float32)
    state_values = np.stack(list(df[state_col])).astype(np.float32)
    if normalize is not None:  # degrees -> lerobot +-100 wire units (see module docstring)
        action_values, state_values = normalize(action_values), normalize(state_values)
    np.save(out_dir / "action.npy", action_values)
    np.save(out_dir / "state.npy", state_values)
    for camera, column in camera_cols.items():
        cam_dir = out_dir / camera_keys[camera]
        cam_dir.mkdir()
        for index, blob in enumerate(df[column].to_numpy()):
            # The blob column is list-typed: one uint8 array per row, nested one level.
            data = blob[0] if blob.dtype == object else blob
            (cam_dir / f"{index:06d}.jpg").write_bytes(bytes(data))
    return len(df)


@dataclasses.dataclass
class Config:
    dataset: str
    """Catalog dataset to export (as listed by `pixi run query-dataset`)."""

    repo_id: str
    """Hugging Face repo id, e.g. ``your-hf-user/my_task``."""

    tag: str = "Good episode"
    """Only export episodes with this curation tag (pass ``--tag ""`` to export all)."""

    fps: int = 30
    """Frame rate stamped on the LeRobot dataset (recordings default to 30)."""

    root: Path = REPO_ROOT / "datasets"
    """Local output directory; the dataset lands in ``<root>/<repo-id>/``."""

    push: bool = False
    """Upload to the Hugging Face Hub after exporting (private repo)."""

    camera_names: tuple[str, ...] = ("top", "side")
    """Semantic names for the exported camera streams, in cam-index order (cam0 gets the
    first name, cam1 the second, ...). MolmoAct2's SO-100/101 checkpoints expect ``top``
    and ``side`` third-person views; extra cameras keep their camNN names."""

    max_height: int = 720
    """Cap the exported video frame height in pixels; taller camera streams are downscaled
    (aspect preserved) before writing. The policy's vision processor resizes frames far
    below this at train and deploy time anyway, and smaller frames export much faster --
    the full-resolution recordings stay untouched. 0 keeps native resolution."""

    units: Literal["lerobot", "degrees"] = "lerobot"
    """Joint units written to the dataset. ``lerobot`` (default) converts our calibrated
    degrees to lerobot's normalized wire units (arm joints [-100, 100] over the calibrated
    range, gripper [0, 100]) — the convention of the pooled SO-100/101 community data, the
    MolmoAct2-SO100_101 base checkpoint, and lerobot-driven deploy clients. ``degrees``
    exports raw calibrated degrees unchanged."""

    calibration: Path | None = None
    """Calibration JSON of the follower arm the episodes were recorded with (needed for
    the degrees -> +-100 conversion). Default: the single follower calibration found in
    ``--calibration-dir``."""

    calibration_dir: Path = Path("calibrations")

    catalog_port: int = 51234
    """so100-server catalog port."""


@dataclasses.dataclass
class ExportResult:
    """Outcome of a finished export, for callers that drive the export in-process
    (e.g. the one-command ``finetune`` wrapper) and want the honest episode tally."""

    output: Path
    """Directory the LeRobot dataset was written to (``<root>/<repo-id>``)."""

    total: int
    """Episodes selected for export (after the tag filter)."""

    staged: int
    """Episodes actually written (had at least one complete frame)."""

    skipped: list[str]
    """Names of episodes dropped for missing a stream (no complete frames)."""


def export_dataset(config: Config) -> ExportResult:
    """Run the export and return its tally. Prints the same progress the CLI shows;
    the CLI's closing ``done``/``next`` lines are added by ``main`` so this function
    stays reusable by other tools. Raises ``SystemExit`` on the same conditions the
    CLI does (already-exported without ``--push``, nothing to export, ...)."""
    writer = REPO_ROOT / "tools" / "apps" / "_export_lerobot_writer.py"
    output = config.root / config.repo_id

    if output.exists():
        if not config.push:
            raise SystemExit(f"{output} already exists -- remove it first to re-export")
        # Already exported: just push the existing local dataset.
        print(f"{output} already exists -- pushing it to the Hub")
        command = ["pixi", "run", "--environment", "export", "python", str(writer), "--root", str(config.root), "--repo-id", config.repo_id, "--push"]
        raise SystemExit(subprocess.run(command, cwd=REPO_ROOT).returncode)

    client = rr.catalog.CatalogClient(f"rerun+http://localhost:{config.catalog_port}")
    dataset = client.get_dataset(name=config.dataset)
    episodes = select_episodes(dataset, config.tag)
    if not episodes:
        raise SystemExit(f"no episodes{f' tagged {config.tag!r}' if config.tag else ''} in dataset '{config.dataset}'")
    action, state, cameras = discover_entities(dataset)
    camera_keys = {camera: (config.camera_names[i] if i < len(config.camera_names) else Path(camera).name) for i, camera in enumerate(cameras)}
    camera_summary = ", ".join(f"{Path(camera).name}->{key}" for camera, key in camera_keys.items()) or "none"
    normalize = None
    if config.units == "lerobot":
        normalize = LerobotNormalizer(config.calibration or find_follower_calibration(config.calibration_dir))
        print(f"units: degrees -> lerobot +-100 (calibration: {normalize.path})")
    else:
        print("units: calibrated degrees (unconverted — NOT the lerobot/MolmoAct2-SO100_101 wire convention)")
    print(f"exporting {len(episodes)} episode(s): action={action} state={state} cameras={camera_summary}")

    with tempfile.TemporaryDirectory(prefix="lerobot-stage-") as stage:
        stage_dir = Path(stage)
        staged = []
        skipped: list[str] = []
        for episode in episodes:
            frames = stage_episode(dataset, episode, action, state, cameras, camera_keys, normalize, stage_dir / episode.segment_id)
            if frames == 0:
                print(f"  {episode.name}: no complete frames, skipped")
                skipped.append(episode.name)
                continue
            print(f"  {episode.name}: {frames} frames")
            staged.append({"dir": episode.segment_id, "name": episode.name, "task": episode.task})
        if not staged:
            raise SystemExit("nothing to export")
        size_note = f"frames capped at {config.max_height}px tall" if config.max_height else "native resolution"
        print(f"writing {len(staged)} episode(s) + encoding videos ({size_note}; local, nothing uploads without --push)...", flush=True)
        print("note: the export is all-or-nothing -- if it fails partway, delete the output and re-run (episodes are not resumable).", flush=True)

        motor_names = list(DEFAULT_MOTOR_NAMES)
        dim = int(np.load(stage_dir / staged[0]["dir"] / "action.npy").shape[1])
        (stage_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "repo_id": config.repo_id,
                    "fps": config.fps,
                    "max_height": config.max_height,
                    "motor_names": motor_names if dim == len(motor_names) else [f"motor_{i}" for i in range(dim)],
                    "cameras": [camera_keys[camera] for camera in cameras],
                    "episodes": staged,
                }
            )
        )

        # Hand off to the isolated lerobot environment (first run solves + installs it).
        print("starting the LeRobot writer in the isolated export env (first run installs it — can take a while)...", flush=True)
        command = ["pixi", "run", "--environment", "export", "python", str(writer), "--root", str(config.root), "--stage", str(stage_dir)]
        if config.push:
            command.append("--push")
        result = subprocess.run(command, cwd=REPO_ROOT)
        if result.returncode != 0:
            raise SystemExit(result.returncode)

    return ExportResult(output=output, total=len(episodes), staged=len(staged), skipped=skipped)


def main(config: Config) -> None:
    result = export_dataset(config)

    print(f"\ndone: {result.output}")
    rel_output = f"./datasets/{config.repo_id}"
    print(f"next — fine-tune on New Theory: pixi run newt finetune --dataset {rel_output}")
    if not config.push:
        print(f"(prefer Hugging Face? push with: pixi run export-lerobot -- --dataset {config.dataset} --repo-id {config.repo_id} --push)")


if __name__ == "__main__":
    try:
        main(tyro.cli(Config))
    except ConnectionError as error:
        raise SystemExit(f"cannot reach the catalog -- is `pixi run so100-server` running? ({error})") from None
