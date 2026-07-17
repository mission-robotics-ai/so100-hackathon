"""One command from a recorded dataset to a fine-tune running on New Theory.

You named your dataset once, when you recorded it. That's the only name this needs::

    pixi run finetune -- --dataset roll_the_dice

It exports your episodes to the LeRobot format New Theory trains on, then launches the
fine-tune -- no repo id, namespace, or path to type. Under the hood it reuses the same
export the ``export-lerobot`` tool runs (curation, unit conversion, camera mapping), writes
to ``datasets/local/<dataset>/``, and hands that folder to ``newt finetune``.

Unlike ``export-lerobot`` (which defaults to only *Good episode*-tagged takes for the
Hugging Face path), this trains on *every* usable episode -- so it works the moment you
finish recording, before you've curated anything. Episodes missing a camera or motion
stream are skipped and reported; the rest are exported fresh on every run.

``--dry-run`` stops after the export and prints the ``newt finetune`` command it would run,
without uploading anything.
"""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
from pathlib import Path

import tyro

# Sibling scripts, on sys.path when run as tools/apps/finetune.py.
from export_lerobot import Config as ExportConfig  # pyrefly: ignore[missing-import]
from export_lerobot import export_dataset  # pyrefly: ignore[missing-import]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Constant local namespace for the export. Never asked -- the point of this tool is that
# the dataset name you chose at record time is the only name you ever type.
NAMESPACE = "local"


@dataclasses.dataclass
class Config:
    dataset: str
    """Recorded dataset to fine-tune on -- the name you chose at record time (as listed by
    ``pixi run query-dataset``)."""

    steps: int | None = None
    """[advanced] Total training steps. Forwarded to ``newt finetune --steps``."""

    name: str | None = None
    """[advanced] Name for the model this run produces. Forwarded to ``newt finetune --name``."""

    fresh: bool = False
    """[advanced] Ignore any existing checkpoint and retrain from scratch. Forwarded to
    ``newt finetune --fresh``."""

    calibration: Path | None = None
    """Follower calibration JSON for the degrees -> lerobot unit conversion. Default: the
    single follower calibration in ``--calibration-dir`` (only needed to disambiguate when
    several exist)."""

    dry_run: bool = False
    """Export, then print the ``newt finetune`` command instead of running it (nothing
    uploads)."""

    calibration_dir: Path = Path("calibrations")
    """Where follower calibrations live (mirrors ``export-lerobot``)."""

    catalog_port: int = 51234
    """so100-server catalog port (mirrors ``export-lerobot``)."""

    output_root: Path = REPO_ROOT / "datasets"
    """Root the export lands under (``<output_root>/local/<dataset>``). Overridable so a
    test run can write somewhere disposable instead of the repo's ``datasets/``."""


def main(config: Config) -> None:
    repo_id = f"{NAMESPACE}/{config.dataset}"
    output = config.output_root / repo_id

    # Re-export on every run. Episodes are small, and the catalog -- not a stale copy on
    # disk -- is the source of truth for what you'd want to train on. Only ever removes our
    # own ``local/<dataset>`` folder, never anything you named.
    if output.exists():
        shutil.rmtree(output)

    export_config = ExportConfig(
        dataset=config.dataset,
        repo_id=repo_id,
        tag="",  # every usable episode, not just Good-tagged -- works before curation
        root=config.output_root,
        calibration=config.calibration,
        calibration_dir=config.calibration_dir,
        catalog_port=config.catalog_port,
        push=False,
    )

    print(f"exporting '{config.dataset}' -> {output}")
    result = export_dataset(export_config)

    # Honesty: episodes the exporter dropped for a missing stream. Loud, before we launch,
    # and we continue anyway -- a partial recording still trains.
    if result.skipped:
        print(
            f"\n{result.staged} of {result.total} episodes usable -- the other {len(result.skipped)} "
            "are missing a stream and were skipped (see the hackathon troubleshooting page)."
        )

    command = ["newt", "finetune", "--dataset", f"./datasets/{repo_id}"]
    if config.steps is not None:
        command += ["--steps", str(config.steps)]
    if config.name is not None:
        command += ["--name", config.name]
    if config.fresh:
        command.append("--fresh")

    if config.dry_run:
        print("\ndry run -- would launch (nothing uploaded):")
        print(f"  {' '.join(command)}")
        return

    print("\nlaunching fine-tune on New Theory -- uploading the dataset + starting the run:")
    raise SystemExit(subprocess.run(command, cwd=REPO_ROOT).returncode)


if __name__ == "__main__":
    main(tyro.cli(Config))
