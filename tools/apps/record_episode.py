"""Record one episode from the command line -- no server, no browser, no buttons.

Opens the SO-100 arms itself (teleop on), tees everything to
``recordings/<dataset>/<episode>.rrd`` with the episode metadata stamped on as recording
properties, and stops on Enter (or after ``--seconds``)::

    pixi run record-episode -- --dataset my_arm --episode episode_21 \\
        --task "Pick up a ball" --tag "Good episode"

If the local data server (``pixi run so100-server``) is running, the take also streams
to its gRPC proxy (live view in any connected viewer) and is registered to its catalog
on stop. If it is not running, the file is still written -- the next server start
re-registers everything under ``recordings/`` from disk.

NOTE: this tool needs the serial ports, so the server must not be holding the arms --
disconnect them there first (``curl -X POST localhost:8000/arms/disconnect``) or record
exclusively from this CLI.
"""

from __future__ import annotations

import dataclasses
import os
import socket
import time
from pathlib import Path

import tyro

from so100_hackathon.apis.log_arms import ArmSession, LogArmsConfig
from so100_hackathon.cameras import CameraSource
from so100_hackathon.console import enable_pretty_tracebacks, info, success, warn
from so100_hackathon.rerun_config import LiveViewerConfig
from so100_hackathon.takes import (
    SEGMENT_TAGS,
    begin_take,
    episode_path,
    finish_take,
    next_episode,
    optimize_rrd,
    register_blueprint,
    register_rrd,
    sanitize_name,
    save_dataset_blueprint,
)

os.environ.setdefault("RERUN_INSECURE_SKIP_HOST_CHECK", "1")
os.environ.setdefault("RERUN_FLUSH_TICK_SECS", "0.008")

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclasses.dataclass
class _NoViewerConfig(LiveViewerConfig):
    """A ``LiveViewerConfig`` whose ``__post_init__`` does nothing (this tool owns all sinks)."""

    def __post_init__(self) -> None:
        pass


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("localhost", port)) == 0


@dataclasses.dataclass
class Config:
    dataset: str = "my_dataset"
    """Dataset the episode belongs to (folder under ``recordings/`` + catalog dataset name)."""

    episode: str | None = None
    """Episode name (recording name + file stem). Default: ``episode_<N>``, N auto-incremented."""

    task: str = ""
    """Natural-language task description, e.g. "Pick up a ball" (becomes the LeRobot task)."""

    tag: str = SEGMENT_TAGS[0]
    """Curation tag stamped on stop (suggested: 'Good episode', 'Bad episode', 'Needs review')."""

    seconds: float | None = None
    """Stop after N seconds. Default: record until you press Enter."""

    fps: float = 30.0
    """Target logging rate (arm poll rate)."""

    teleop: bool = True
    """Drive the follower to mirror the leader while recording (needs two calibrated arms)."""

    fake: bool = False
    """Record only the connected camera(s), no arms (for testing without hardware)."""

    recordings_dir: Path = REPO_ROOT / "recordings"
    """Folder the episode is written to, as ``<dataset>/<episode>.rrd``."""

    grpc_port: int = 9876
    """so100-server proxy port; if something is listening, the take also streams there live."""

    catalog_port: int = 51234
    """so100-server catalog port; if reachable, the episode is registered on stop."""


def main(config: Config) -> None:
    episode = config.episode or next_episode(config.recordings_dir, config.dataset)
    path = episode_path(config.recordings_dir, config.dataset, episode)

    proxy_uri = f"rerun+http://localhost:{config.grpc_port}/proxy" if _port_open(config.grpc_port) else None
    if proxy_uri is not None:
        info(f"live view:  streaming to {proxy_uri} (so100-server proxy)")

    rec = begin_take(path, episode=episode, dataset=sanitize_name(config.dataset), task=config.task, proxy_uri=proxy_uri)
    source: ArmSession | CameraSource = (
        CameraSource(rec) if config.fake else ArmSession(LogArmsConfig(fps=config.fps, teleop=config.teleop, rr_config=_NoViewerConfig()), rec)
    )
    source.start()
    source.begin(rec)

    info(f"recording:  {path}")
    try:
        if config.seconds is not None:
            time.sleep(config.seconds)
        else:
            input("recording... press Enter to stop\n")
    except KeyboardInterrupt:
        info("\nstopping")
    finally:
        finish_take(rec, dataset=sanitize_name(config.dataset), task=config.task, tag=config.tag, proxy_uri=proxy_uri)
        source.close()

    optimize_rrd(path)
    # Save the dataset's default blueprint next to the episodes: registered below if the
    # catalog is up, re-registered by every so100-server start either way -- without it,
    # episodes opened from the catalog get the viewer's heuristic layout.
    blueprint_file = save_dataset_blueprint(config.recordings_dir, config.dataset, source.blueprint()) if isinstance(source, ArmSession) else None
    if _port_open(config.catalog_port):
        catalog_uri = f"rerun+http://localhost:{config.catalog_port}"
        registration = register_rrd(catalog_uri, sanitize_name(config.dataset), path)
        success(f"registered: dataset '{registration['dataset']}', segments {registration['segment_ids']}")
        if blueprint_file is not None and register_blueprint(catalog_uri, sanitize_name(config.dataset), blueprint_file):
            success("blueprint:  registered as the dataset's default")
    else:
        warn(f"saved:      {path} (no catalog server on port {config.catalog_port}; the next `pixi run so100-server` start registers it)")


if __name__ == "__main__":
    enable_pretty_tracebacks()
    main(tyro.cli(Config))
