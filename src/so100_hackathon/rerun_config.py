"""Tyro-facing Rerun viewer/save/connect config, vendored from simplecv.rerun_log_utils.

Add it as a nested dataclass field (``rr_config: RerunTyroConfig``); its
``__post_init__`` wires up spawn/connect/save/serve/headless behavior so the
tool can use plain global ``rr.*`` logging calls afterwards.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import rerun as rr


def get_safe_application_id() -> str:
    """Get application ID safely, with fallback if __main__.__file__ doesn't exist"""
    try:
        main = sys.modules.get("__main__")
        if main:
            file_attr = getattr(main, "__file__", None)
            if isinstance(file_attr, str):
                return Path(file_attr).stem
    except Exception:
        pass
    return "rerun-application"  # Default fallback


@dataclass
class RerunTyroConfig:
    application_id: str = field(default_factory=get_safe_application_id)
    """Name of the application"""
    recording_id: str | UUID | None = None
    """Recording ID"""
    connect: bool = False
    """Whether to connect to an existing rerun instance or not"""
    connect_url: str | None = None
    """Optional gRPC url for ``connect`` (default: the local viewer at 9876)."""
    save: Path | None = None
    """Path to save the rerun data, this will make it so no data is visualized but saved"""
    serve: bool = False
    """Serve the rerun data"""
    headless: bool = False
    """Run rerun in headless mode"""
    live: bool = False
    """When combined with ``save``, stream to a spawned viewer AND write the .rrd
    file simultaneously (via ``set_sinks``), instead of ``save`` being file-only.
    Ignored when ``headless`` (no viewer), or when ``serve``/``connect`` is set."""
    port: int = 9876
    """Port for the spawned viewer's gRPC proxy (used by ``spawn`` and ``live`` + ``save``)."""
    executable_name: str = "rerun"
    """Executable name passed to ``rerun.spawn`` when launching the viewer."""
    executable_path: str | None = None
    """Optional absolute or relative path to the Rerun executable."""

    def __post_init__(self):
        rr.init(
            application_id=self.application_id,
            recording_id=self.recording_id,
            default_enabled=True,
            strict=True,
        )
        self.rec_stream: rr.RecordingStream = rr.get_global_data_recording()  # type: ignore[assignment]

        if self.serve:
            rr.serve_grpc()
            rr.serve_web_viewer(open_browser=not self.headless)
        elif self.connect:
            rr.connect_grpc(url=self.connect_url)
        elif self.save is not None and self.live and not self.headless:
            # Stream to a spawned viewer AND save to a .rrd at the same time by
            # fanning out through explicit sinks. ``spawn``/``save`` each install a
            # single sink that would replace the other, so we spawn the viewer
            # process without auto-connecting and wire both sinks ourselves.
            rr.spawn(
                port=self.port,
                connect=False,
                executable_name=self.executable_name,
                executable_path=self.executable_path,
            )
            rr.set_sinks(
                rr.GrpcSink(f"rerun+http://127.0.0.1:{self.port}/proxy"),
                rr.FileSink(str(self.save)),
            )
        elif self.save is not None:
            rr.save(self.save)
        elif not self.headless:
            rr.spawn(
                port=self.port,
                executable_name=self.executable_name,
                executable_path=self.executable_path,
            )


@dataclass
class LiveViewerConfig(RerunTyroConfig):
    # Realtime tools default to a live viewer. Combined with --rr-config.save this
    # fans out to viewer + .rrd simultaneously (see RerunTyroConfig.live).
    live: bool = True
