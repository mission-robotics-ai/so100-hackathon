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
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import tyro
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

os.environ.setdefault("RERUN_INSECURE_SKIP_HOST_CHECK", "1")

import rerun as rr  # noqa: E402 - the env var above must be set before use

from so100_hackathon.calibration import DEFAULT_MOTOR_NAMES  # noqa: E402
from so100_hackathon.console import console, enable_pretty_tracebacks, info, note, success, warn  # noqa: E402

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


def _column(df, entity: str, suffix: str) -> str:
    matches = [name for name in df.columns if name.endswith(suffix) and entity in name]
    if not matches:
        raise SystemExit(f"column '{entity}{suffix}' missing from the query result (got: {list(df.columns)})")
    return matches[0]


def stage_episode(dataset: rr.catalog.DatasetEntry, episode: Episode, action: str, state: str, cameras: list[str], out_dir: Path) -> int:
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
    np.save(out_dir / "action.npy", np.stack(list(df[action_col])).astype(np.float32))
    np.save(out_dir / "state.npy", np.stack(list(df[state_col])).astype(np.float32))
    for camera, column in camera_cols.items():
        cam_dir = out_dir / Path(camera).name
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

    catalog_port: int = 51234
    """so100-server catalog port."""


def main(config: Config) -> None:
    writer = REPO_ROOT / "tools" / "apps" / "_export_lerobot_writer.py"
    output = config.root / config.repo_id

    if output.exists():
        if not config.push:
            raise SystemExit(f"{output} already exists -- remove it first to re-export")
        # Already exported: just push the existing local dataset.
        info(f"{output} already exists -- pushing it to the Hub")
        command = ["pixi", "run", "--environment", "export", "python", str(writer), "--root", str(config.root), "--repo-id", config.repo_id, "--push"]
        raise SystemExit(subprocess.run(command, cwd=REPO_ROOT).returncode)

    client = rr.catalog.CatalogClient(f"rerun+http://localhost:{config.catalog_port}")
    dataset = client.get_dataset(name=config.dataset)
    episodes = select_episodes(dataset, config.tag)
    if not episodes:
        raise SystemExit(f"no episodes{f' tagged {config.tag!r}' if config.tag else ''} in dataset '{config.dataset}'")
    action, state, cameras = discover_entities(dataset)
    info(f"exporting {len(episodes)} episode(s): action={action} state={state} cameras={cameras or 'none'}")

    with tempfile.TemporaryDirectory(prefix="lerobot-stage-") as stage:
        stage_dir = Path(stage)
        staged = []
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("staging episodes", total=len(episodes))
            for episode in episodes:
                progress.update(task, description=f"staging {episode.name}")
                frames = stage_episode(dataset, episode, action, state, cameras, stage_dir / episode.segment_id)
                if frames == 0:
                    warn(f"  {episode.name}: no complete frames, skipped")
                else:
                    staged.append({"dir": episode.segment_id, "name": episode.name, "task": episode.task})
                progress.advance(task)
        if not staged:
            raise SystemExit("nothing to export")

        motor_names = list(DEFAULT_MOTOR_NAMES)
        dim = int(np.load(stage_dir / staged[0]["dir"] / "action.npy").shape[1])
        (stage_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "repo_id": config.repo_id,
                    "fps": config.fps,
                    "motor_names": motor_names if dim == len(motor_names) else [f"motor_{i}" for i in range(dim)],
                    "cameras": [Path(camera).name for camera in cameras],
                    "episodes": staged,
                }
            )
        )

        # Hand off to the isolated lerobot environment (first run solves + installs it).
        command = ["pixi", "run", "--environment", "export", "python", str(writer), "--root", str(config.root), "--stage", str(stage_dir)]
        if config.push:
            command.append("--push")
        result = subprocess.run(command, cwd=REPO_ROOT)
        if result.returncode != 0:
            raise SystemExit(result.returncode)

    success(f"\ndone: {output}")
    if not config.push:
        note(f"push it later with: pixi run export-lerobot -- --dataset {config.dataset} --repo-id {config.repo_id} --push")


if __name__ == "__main__":
    enable_pretty_tracebacks()
    try:
        main(tyro.cli(Config))
    except ConnectionError as error:
        raise SystemExit(f"cannot reach the catalog -- is `pixi run so100-server` running? ({error})") from None
