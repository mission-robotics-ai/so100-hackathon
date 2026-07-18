"""SO-100 local data server: one long-lived process for the whole data-collection loop.

Start it once and leave it running -- record episodes, take a break, close the browser
page, come back, query, record more, or create new datasets, all without restarting::

    pixi run so100-server

Three things run inside:

* ``--grpc-port``    (default 9876):  a Rerun gRPC *proxy* server (in its own process)
  -- the live stream CLI tools tee into (``record-episode``, ``replay-episode``) and the
  Deploy page's embedded viewer watches. The Set up and Collect pages do NOT use it:
  every setup run and every arms session gets its own throwaway proxy on a fresh port
  (exposed as ``proxy_port`` in ``/status``), so their viewers start from a clean state
  and all buffered data is freed when the run/session ends. The proxy's memory limit
  (1 GiB by default) drops the oldest data, so the arms session's ONE continuous live
  stream flushes itself instead of growing forever.
* ``--catalog-port`` (default 51234): an in-process Rerun catalog (``rr.server.Server``).
  On startup every ``recordings/<dataset>/*.rrd`` on disk is registered into a catalog
  dataset named after its folder -- so shutting the server down loses nothing.
* ``--control-port`` (default 8000):  a small JSON API (CORS-enabled, so the course site
  can call it from any origin):

  - ``GET  /status``            -- arms + recording + setup-tool state
  - ``GET  /datasets``          -- catalog dataset names (for the form dropdown)
  - ``GET  /episodes?dataset=X``-- the dataset's registered episodes (id, task, tag,
    viewer deep link) plus the id the next recording will get (``episode_NN``, max + 1)
  - ``POST /arms/connect``      -- open the SO-100 arms (teleop on) and start the live stream
  - ``POST /arms/disconnect``   -- release the arms (frees the serial ports for calibration)
  - ``POST /live/pause``        -- stop feeding frames into the live stream (teleop keeps
    working; the stream keeps its id, so resuming continues the SAME recording)
  - ``POST /live/resume``       -- feed frames into the live stream again
  - ``POST /start``             -- ``{"dataset": ..., "task": ...}``; the episode id is
    assigned server-side (pass ``"episode"`` to override, e.g. from the CLI)
  - ``POST /stop``              -- ``{"tag": "Good episode"}``: close the take file and
    register it into the catalog (the live stream keeps running throughout)
  - ``POST /episode/update``    -- ``{"dataset": ..., "episode": ..., "task": ..., "tag": ...}``:
    save an episode's properties. Defaults to the latest take; finished episodes get an
    ``edits`` catalog layer, the in-progress take is stamped directly.
  - ``POST /setup/start``       -- ``{"tool": "ping" | "calibrate" | "teleop"}``: run a setup
    CLI tool as a subprocess, streaming into the proxy (``calibrate`` chains leader ->
    follower)
  - ``POST /setup/next``        -- press Enter in the running setup tool
  - ``POST /setup/stop``        -- Ctrl-C the running setup tool

The arms are NOT opened at startup: calibration and teleop need exclusive serial-port
access, so the ports stay free until ``/arms/connect`` (or ``--fake`` camera-only mode).

While recording, every frame is fanned out to the always-on live stream AND a take
recording writing straight to ``recordings/<dataset>/<episode>.rrd`` -- the take never
touches the viewer. Episode name, task, and segment tag are stamped on as recording
properties -- they show up as ``property:...`` columns when querying the dataset. On
stop the file is compacted and registered, and the page opens it from the catalog.

Prefer the command line? ``pixi run record-episode`` records + registers without this
server (see ``tools/apps/record_episode.py``), and this API is curl-friendly.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Sequence  # noqa: F401 - Sequence is used in the cast below
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlparse

import rerun as rr
import tyro

from so100_hackathon.apis.log_arms import ArmSession, LogArmsConfig
from so100_hackathon.calibration import load_arm_kind
from so100_hackathon.cameras import CameraSource, RecordingFanout, require_camera_indices
from so100_hackathon.rerun_config import LiveViewerConfig
from so100_hackathon.setup_phases import PHASE_PREFIX
from so100_hackathon.takes import (
    APP_ID,
    SEGMENT_TAGS,
    begin_take,
    edits_path,
    episode_path,
    finish_take,
    next_episode,
    optimize_rrd,
    register_blueprint,
    register_edits,
    register_rrd,
    sanitize_name,
    save_dataset_blueprint,
    scan_blueprints,
    scan_edits,
    scan_recordings,
    stamp_properties,
    write_edits,
)

# The catalog client refuses localhost tokens unless we opt out of the host check.
os.environ.setdefault("RERUN_INSECURE_SKIP_HOST_CHECK", "1")
# Low-latency micro-batcher (8 ms flush, == ChunkBatcherConfig.LOW_LATENCY) for every
# recording in this process, the live preview, and the proxy subprocess (inherits env).
os.environ.setdefault("RERUN_FLUSH_TICK_SECS", "0.008")

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclasses.dataclass
class _NoViewerConfig(LiveViewerConfig):
    """A ``LiveViewerConfig`` whose ``__post_init__`` does nothing.

    The server owns all Rerun setup (recording ids, sinks, ports). The arm session must
    NOT spawn a native viewer or re-init the recording, which the normal
    ``LiveViewerConfig.__post_init__`` would do.
    """

    def __post_init__(self) -> None:
        pass


def require_port(port: int, what: str) -> None:
    """Fail fast (with a helpful hint) if a port the course site depends on is taken.

    SO_REUSEADDR matches how the real servers bind: without it, connections still in
    TIME_WAIT from the previous run would fail this check for ~30s after every restart,
    even though the port is actually available.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("localhost", port))
        except OSError:
            raise SystemExit(f"port {port} ({what}) is already in use -- is `pixi run so100-server` already running in another terminal?") from None


def free_port() -> int:
    """An OS-picked free TCP port (for the per-run setup proxies)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("localhost", 0))
        return sock.getsockname()[1]


def spawn_proxy(grpc_port: int) -> subprocess.Popen[bytes]:
    """A Rerun gRPC proxy server in its OWN process.

    ``rerun --serve-grpc`` is a *pure* proxy: unlike ``rr.serve_grpc()`` it needs no SDK
    recording, so it does not add an empty ghost recording to every connecting viewer.
    A tiny wrapper watches the parent pid and kills the proxy if the server dies without
    cleanup (SIGKILL, crash), so it can never orphan and squat the port.

    The wrapper runs the ``rerun`` *binary* directly: ``python -m rerun`` is a launcher
    that runs the binary as a grandchild, which SIGTERM would never reach -- the proxy
    would outlive its session and keep the port + buffered recordings alive.
    """
    import rerun_cli.__main__ as rerun_cli

    # Mirror rerun_cli.__main__'s binary resolution: on macOS the binary ships
    # inside an app bundle, elsewhere it sits next to the package.
    cli_dir = Path(rerun_cli.__file__).parent
    if binary := os.environ.get("RERUN_CLI_PATH"):
        pass
    else:
        bundled = cli_dir / "Rerun.app" / "Contents" / "MacOS" / "Rerun"
        if sys.platform == "darwin" and bundled.exists():
            binary = str(bundled)
        else:
            binary = rerun_cli.add_exe_suffix(str(cli_dir / "rerun"))
    code = "\n".join(
        (
            "import os, signal, subprocess, sys, time",
            f"proc = subprocess.Popen([{binary!r}, '--serve-grpc', '--port', '{grpc_port}'])",
            "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))",
            "parent = os.getppid()",
            "try:",
            "    while os.getppid() == parent and proc.poll() is None:",
            "        time.sleep(1.0)",
            "finally:",
            "    proc.terminate()",
        )
    )
    return subprocess.Popen([sys.executable, "-c", code])


def wait_for_port(port: int, timeout: float = 20.0) -> None:
    """Block until something accepts TCP connections on the port."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("localhost", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"proxy on port {port} did not come up within {timeout:.0f}s")


class Recorder:
    """Arms-on-demand data source + recording takes.

    Once connected, the source (arms or cameras) streams continuously into ONE always-on
    live recording -- the only thing the page's viewer ever watches (``recording_id``).
    The proxy's memory limit flushes its oldest data, so it never grows forever.

    ``start`` opens a per-take recording writing straight to disk (file sink only, no
    proxy -- the viewer never sees it) and tees the source's frames into live + file via
    :class:`~so100_hackathon.cameras.RecordingFanout`. ``stop`` points the source back at
    the live stream alone, closes the take file, then optimizes + registers it -- the
    page opens the fresh episode from the catalog while the live stream keeps running.
    ``update_episode`` rewrites a finished episode's metadata via an ``edits`` layer.
    """

    def __init__(self, catalog_uri: str, recordings_dir: Path, arm_fps: float, cameras: tuple[int, ...] | None = None) -> None:
        self._catalog_uri = catalog_uri
        self._recordings_dir = recordings_dir
        self._arm_fps = arm_fps
        self._cameras = cameras
        self._lock = threading.Lock()
        self._source: CameraSource | ArmSession | None = None
        self._fake = False
        self._recording = False
        self._current_file: Path | None = None
        self._live: rr.RecordingStream | None = None  # the always-on live stream (proxy sink)
        self._live_paused = False  # frames dropped instead of logged into the live stream
        self._take: rr.RecordingStream | None = None  # the in-progress take (file sink), if any
        self._last: dict[str, object] = {}  # summary of the most recent take
        self._last_take: dict[str, str] | None = None  # ids needed to update the last registered take
        # Per arms-session throwaway proxy (spawned on connect, killed on disconnect).
        self._proxy_proc: subprocess.Popen[bytes] | None = None
        self._proxy_port: int | None = None
        self._proxy_uri: str | None = None
        self._recording_id: str | None = None  # the live stream's id: what the viewer should show

    def state(self) -> dict[str, object]:
        arms = "disconnected"
        if self._source is not None:
            arms = "fake" if self._fake else "connected"
        return {
            "arms": arms,
            "running": self._recording,
            "live_paused": self._live_paused,
            "proxy_port": self._proxy_port,
            "recording_id": self._recording_id,
            "last": self._last,
            "tags": list(SEGMENT_TAGS),
        }

    # --- arms -----------------------------------------------------------------

    def connect_arms(self, *, fake: bool) -> dict[str, object]:
        with self._lock:
            if self._source is not None:
                return self.state()
            # Explicit --cameras indices are used verbatim (no auto-detect probe), so
            # check them here, before the proxy spawns -- a bad index must fail the
            # connect loudly, not a camera thread quietly after "connected".
            if self._cameras is not None:
                require_camera_indices(self._cameras)
            # This session's throwaway proxy: the page's viewer starts from a clean state.
            proxy_port = free_port()
            proxy = spawn_proxy(proxy_port)
            try:
                wait_for_port(proxy_port)
            except RuntimeError:
                proxy.terminate()
                raise
            self._proxy_proc = proxy
            self._proxy_port = proxy_port
            self._proxy_uri = f"rerun+http://localhost:{proxy_port}/proxy"

            # The ONE always-on live stream for this session. It runs until disconnect;
            # the proxy's memory limit drops its oldest data, so it flushes itself.
            live_id = f"live-{uuid.uuid4().hex[:8]}"
            rec = rr.RecordingStream(APP_ID, recording_id=live_id, batcher_config=rr.ChunkBatcherConfig.LOW_LATENCY())
            rec.set_sinks(rr.GrpcSink(url=self._proxy_uri))
            rec.send_recording_name("Live view")
            self._live = rec
            self._recording_id = live_id

            if fake:
                self._source = CameraSource(rec, cameras=self._cameras)
            else:
                self._source = ArmSession(LogArmsConfig(fps=self._arm_fps, teleop=True, cameras=self._cameras, rr_config=_NoViewerConfig()), rec)
            self._fake = fake
            self._source.start()
            self._source.begin(rec)  # blueprint + static geometry for the live stream
            return self.state()

    def disconnect_arms(self) -> dict[str, object]:
        with self._lock:
            if self._recording:
                raise RuntimeError("stop the recording first")
            source, self._source = self._source, None
            live, self._live = self._live, None
            self._live_paused = False
            proxy, self._proxy_proc = self._proxy_proc, None
            self._proxy_port = None
            self._proxy_uri = None
            self._recording_id = None
        if source is not None:
            source.close()
            print("[arms]      disconnected (serial ports released)", flush=True)
        if live is not None:
            live.disconnect()
        if proxy is not None:
            proxy.terminate()  # frees everything this session streamed to the viewer
        return self.state()

    def pause_live(self) -> dict[str, object]:
        """Stop routing frames into the live stream (they are dropped, not buffered).

        The source keeps running -- teleop still drives the follower -- and the live
        ``RecordingStream`` stays alive with the same recording id, so :meth:`resume_live`
        continues the SAME recording (the timeline just shows a gap).
        """
        with self._lock:
            if self._source is None or self._live is None:
                raise RuntimeError("connect the arms first (POST /arms/connect)")
            if self._recording:
                raise RuntimeError("stop the recording first -- the take needs every frame")
            if not self._live_paused:
                self._source.set_output(RecordingFanout())  # zero sinks: frames are dropped
                self._live_paused = True
                print("[live]      paused (frames dropped, stream id kept)", flush=True)
            return self.state()

    def resume_live(self) -> dict[str, object]:
        """Route frames back into the (same) live stream."""
        with self._lock:
            if self._source is None or self._live is None:
                raise RuntimeError("connect the arms first (POST /arms/connect)")
            if self._live_paused:
                self._source.set_output(self._live)
                self._live_paused = False
                print("[live]      resumed (same recording continues)", flush=True)
            return self.state()

    # --- takes ----------------------------------------------------------------

    def start(self, *, dataset: str, task: str, episode: str | None = None) -> dict[str, object]:
        with self._lock:
            if self._source is None or self._live is None:
                raise RuntimeError("connect the arms first (POST /arms/connect)")
            if self._recording:
                return self.state()

            # Episode ids are assigned here, not by the client: always the next free
            # episode_NN (max + 1, never reused -- see takes.next_episode).
            episode = episode or next_episode(self._recordings_dir, dataset)
            path = episode_path(self._recordings_dir, dataset, episode)
            # File sink ONLY: the take goes straight to disk, invisible to the viewer,
            # which keeps showing the live stream throughout.
            take = begin_take(path, episode=episode, dataset=sanitize_name(dataset), task=task, proxy_uri=None)
            # The take file needs its own copy of the statics (series names, URDF,
            # blueprint), then frames are teed into live + file.
            self._source.begin(take)
            # Starting a take also resumes a paused live stream: the fanout feeds both,
            # so the viewer shows what is being recorded.
            self._source.set_output(RecordingFanout(self._live, take))
            self._live_paused = False

            self._take = take
            self._current_file = path
            self._recording = True
            self._last = {"file": str(path), "episode": episode, "stem": path.stem, "status": "recording"}
            self._last_take = {"dataset": sanitize_name(dataset), "stem": path.stem, "task": task}
            print(f"[recording] started {path.stem} -> {path}", flush=True)
            return self.state()

    def stop(self, *, tag: str) -> dict[str, object]:
        with self._lock:
            if not self._recording:
                return self.state()
            self._recording = False
            path = self._current_file
            take, self._take = self._take, None
            episode = str(self._last.get("episode", path.stem if path else ""))
            task = str((self._last_take or {}).get("task", ""))
            # Back to the live stream alone -- it never paused, so the viewer just
            # keeps playing while the file is finalized below.
            source = self._source
            if source is not None and self._live is not None:
                source.set_output(self._live)

        assert take is not None and path is not None  # set by start() before _recording flips true
        # proxy_uri=None: stamp the final properties, flush, and close the file.
        finish_take(take, dataset=path.parent.name, task=task, tag=tag, proxy_uri=None)
        print(f"[recording] stopped -> {path} (tag: {tag})", flush=True)

        summary: dict[str, object] = {"file": str(path), "episode": episode, "stem": path.stem, "status": "stopped", "tag": tag}
        try:
            optimize_rrd(path)
            registration = register_rrd(self._catalog_uri, path.parent.name, path)
            summary["registration"] = registration
            summary["status"] = "registered"
            print(f"[catalog]   registered {path} in dataset '{path.parent.name}' (segments: {registration['segment_ids']})", flush=True)
            # Give the dataset the same blueprint as the live stream, so episodes opened
            # from the catalog don't fall back to the viewer's heuristic layout. One
            # blueprint per dataset: no-op once it exists (users may customize it).
            if isinstance(source, ArmSession):
                blueprint_file = save_dataset_blueprint(self._recordings_dir, path.parent.name, source.blueprint())
                if register_blueprint(self._catalog_uri, path.parent.name, blueprint_file):
                    print(f"[catalog]   default blueprint set for dataset '{path.parent.name}'", flush=True)
        except Exception as err:  # noqa: BLE001 - surface any failure to the UI
            summary["status"] = "register_failed"
            summary["error"] = f"{type(err).__name__}: {err}"
            with self._lock:
                self._last_take = None  # nothing in the catalog to update
            print(f"[catalog]   registration FAILED for {path}: {summary['error']}", flush=True)

        with self._lock:
            self._last = summary
        return self.state()

    def update_episode(self, *, task: str, tag: str, dataset: str | None = None, episode: str | None = None) -> dict[str, object]:
        """Save an episode's properties (task + tag).

        With no dataset/episode given, the most recent take is updated. While the
        episode is still being recorded, the properties are stamped straight into the
        live recording (baked into the ``.rrd``); once it is finished they are written
        as an ``edits`` catalog layer over the same segment.
        """
        with self._lock:
            take = self._last_take
        if dataset is None or episode is None:
            if take is None:
                raise RuntimeError("no episode to update -- record one first")
            dataset, episode = take["dataset"], take["stem"]
        name = sanitize_name(dataset)
        stem = sanitize_name(episode)

        with self._lock:
            take = self._take
            current = self._current_file
            mid_take = self._recording and current is not None and current.stem == stem and current.parent.name == name
        if mid_take:
            assert take is not None  # set by start() before _recording flips true
            stamp_properties(take, dataset=name, task=task, tag=tag)
            take.log("/task", rr.TextDocument(task), static=True)
            with self._lock:
                if self._last_take is not None:
                    self._last_take["task"] = task  # so stop() re-stamps the edited task, not the original
            print(f"[recording] updated {name}/{stem} mid-take (task: {task!r}, tag: {tag!r})", flush=True)
        else:
            if not (self._recordings_dir / name / f"{stem}.rrd").exists():
                raise RuntimeError(f"no episode '{stem}' in dataset '{name}'")
            path = edits_path(self._recordings_dir, name, stem)
            write_edits(path, recording_id=f"{name}-{stem}", task=task, tag=tag)
            register_edits(self._catalog_uri, name, [path])
            print(f"[catalog]   updated {name}/{stem} (task: {task!r}, tag: {tag!r})", flush=True)
        with self._lock:
            if self._last.get("episode") == stem:
                self._last = {**self._last, "tag": tag}
            return self.state()

    def close(self) -> None:
        if self._recording:
            self.stop(tag="Needs review")
        if self._source is not None:
            self.disconnect_arms()


def list_episodes(client: rr.catalog.CatalogClient, recordings_dir: Path, dataset: str) -> dict[str, object]:
    """One entry per registered episode (id, current properties, viewer deep link), sorted,
    plus the id the NEXT recording in this dataset will get.

    Property columns are list-typed (one value per catalog layer, ``edits`` overlaying the
    base recording), so the first element is the current value.
    """

    def first(value: object) -> str:
        if isinstance(value, str):
            return value
        try:
            return str(value[0]) if len(value) else ""  # pyrefly: ignore[bad-argument-type, unsupported-operation, bad-index]
        except TypeError:
            return ""

    name = sanitize_name(dataset)
    episodes: list[dict[str, object]] = []
    dataset_url: str | None = None
    if name in set(client.dataset_names()):
        ds = client.get_dataset(name=name)
        table = ds.segment_table().to_pandas()
        for _, row in table.iterrows():
            segment_id = str(row["rerun_segment_id"])
            stem = segment_id.removeprefix(f"{name}-")
            episodes.append(
                {
                    "episode": first(row.get("property:RecordingInfo:name")) or stem,
                    "stem": stem,
                    "task": first(row.get("property:episode:task")),
                    "tag": first(row.get("property:episode:tag")),
                    "segment_id": segment_id,
                    "viewer_url": ds.segment_url(segment_id),
                }
            )

        def sort_key(entry: dict[str, object]) -> tuple[int, int, str]:
            # Episode number first, then legacy collision suffix (episode_1-2 etc.).
            stem = str(entry["stem"])
            m = re.match(r"episode_(\d+)(?:-(\d+))?$", stem)
            if m is None:
                return (1 << 30, 0, stem)
            return (int(m.group(1)), int(m.group(2) or 1), stem)

        episodes.sort(key=sort_key)
        if episodes:
            # The viewer's URI parser accepts /entry/<id> (the dataset's catalog table
            # screen) but NOT a bare /dataset/<id> (only with a ?segment_id query).
            dataset_url = str(episodes[0]["viewer_url"]).split("?", 1)[0].replace("/dataset/", "/entry/")
    return {"dataset": name, "episodes": episodes, "next": next_episode(recordings_dir, name), "dataset_url": dataset_url}


class SetupRunner:
    """Runs the setup CLI tools (ping / calibrate / teleop) on behalf of the course site.

    Each tool is the *same* script a terminal user would run, spawned as a subprocess with
    ``--rr-config.connect`` so it streams into the proxy (and thus the embedded viewer).
    stdin is piped: ``next()`` types Enter for the user; ``stop()`` is Ctrl-C (SIGINT), so
    the tool's own cleanup (torque release) runs. Tools announce their current phase on
    stdout (see ``so100_hackathon.setup_phases``); a tail thread mirrors it into
    ``state()``. ``calibrate`` is two stages -- the follower starts when the leader
    finishes successfully.

    Every run gets its OWN throwaway gRPC proxy on a fresh port (exposed in ``state()``
    as ``proxy_port``): the embedded viewer connects to it and sees ONLY this run's
    recordings -- no clutter from earlier steps -- and killing the proxy when the run
    ends frees all of its memory. The main proxy stays reserved for the recorder.
    """

    def __init__(self, calibration_dir: Path) -> None:
        self._calibration_dir = calibration_dir
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._proxy_proc: subprocess.Popen[bytes] | None = None
        self._proxy_port: int | None = None
        self._stages: list[tuple[str | None, str, list[str]]] = []  # (arm kind, recording id, argv) per stage
        self._stage_index = 0
        self._tool: str | None = None
        self._arm: str | None = None
        self._recording_id: str | None = None
        self._phase: str | None = None
        self._error: str | None = None
        self._running = False
        self._stop_requested = False
        self._tail: deque[str] = deque(maxlen=30)  # recent output, for error reporting

    def _stages_for(self, tool: str, proxy_uri: str) -> list[tuple[str | None, str, list[str]]]:
        """(arm kind, recording id, argv) per stage. Each stage gets a fresh recording id
        that the server knows up front, so the course page can point its viewer at exactly
        this run's recording (the viewer does not reliably switch to new recordings on its
        own -- without this, the follower calibration stage plays out invisibly)."""

        def stage(arm: str | None, script: str, *args: str) -> tuple[str | None, str, list[str]]:
            recording_id = f"setup-{tool}{f'-{arm}' if arm else ''}-{uuid.uuid4().hex[:8]}"
            argv = [
                sys.executable,
                str(REPO_ROOT / "tools" / "apps" / script),
                *args,
                "--rr-config.connect",
                "--rr-config.connect-url",
                proxy_uri,
                "--rr-config.recording-id",
                recording_id,
            ]
            return arm, recording_id, argv

        if tool == "ping":
            return [stage(None, "log_so100.py")]
        if tool == "teleop":
            return [stage(None, "log_so100.py", "--teleop", "--fps", "60")]
        if tool == "calibrate":
            return [stage(kind, "calibrate_so100.py", kind) for kind in ("leader", "follower")]
        raise RuntimeError(f"unknown setup tool {tool!r} (expected ping, calibrate, or teleop)")

    def state(self) -> dict[str, object]:
        # Recomputed from disk every poll: reflects CLI calibrations too, survives restarts.
        calibrated = {"leader": False, "follower": False}
        for path in self._calibration_dir.glob("*.json"):
            kind = load_arm_kind(path)
            if kind in calibrated:
                calibrated[kind] = True
        with self._lock:
            return {
                "tool": self._tool,  # kept after exit so errors attribute to the right widget
                "arm": self._arm,
                "phase": self._phase,
                "recording_id": self._recording_id,  # what the page's viewer should show
                "proxy_port": self._proxy_port,  # where the page's viewer should connect
                "running": self._running,
                "calibrated": calibrated,
                "error": self._error,
            }

    def start(self, tool: str) -> None:
        with self._lock:
            if self._running:
                raise RuntimeError(f"setup tool '{self._tool}' is already running -- stop it first")
            # Reserve the runner before the (seconds-long) proxy spawn below.
            self._running = True
            self._tool = tool
            self._error = None
            self._stop_requested = False
            self._tail.clear()

        # A throwaway proxy just for this run: the viewer starts from a clean state.
        proxy_port = free_port()
        proxy = spawn_proxy(proxy_port)
        try:
            wait_for_port(proxy_port)
        except RuntimeError:
            proxy.terminate()
            with self._lock:
                self._running = False
            raise

        with self._lock:
            self._proxy_proc = proxy
            self._proxy_port = proxy_port
            self._stages = self._stages_for(tool, f"rerun+http://localhost:{proxy_port}/proxy")
            self._stage_index = 0
        self._spawn_stage()

    def _spawn_stage(self) -> None:
        arm, recording_id, argv = self._stages[self._stage_index]
        proc = subprocess.Popen(
            argv,
            cwd=REPO_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            # Unbuffered child stdout: pipes are block-buffered by default, which would
            # hold the phase markers back indefinitely.
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        with self._lock:
            self._proc = proc
            self._arm = arm
            self._recording_id = recording_id
            self._phase = None
        print(f"[setup]     started {self._tool}{f' ({arm})' if arm else ''} (pid {proc.pid})", flush=True)
        threading.Thread(target=self._watch, args=(proc,), daemon=True).start()

    def _watch(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n")
            if line.startswith(PHASE_PREFIX):
                with self._lock:
                    self._phase = line.removeprefix(PHASE_PREFIX).strip()
            elif line.strip():
                with self._lock:
                    self._tail.append(line)
        returncode = proc.wait()
        with self._lock:
            stopped = self._stop_requested
            has_next_stage = self._stage_index + 1 < len(self._stages)
            tail = list(self._tail)
        if returncode == 0 and not stopped and has_next_stage:
            self._stage_index += 1
            self._spawn_stage()
            return
        with self._lock:
            self._running = False
            self._proc = None
            self._arm = None
            self._recording_id = None
            self._phase = None
            proxy, self._proxy_proc = self._proxy_proc, None
            self._proxy_port = None
            if returncode != 0 and not stopped:
                self._error = tail[-1] if tail else f"exited with code {returncode}"
        if proxy is not None:
            proxy.terminate()  # frees everything this run streamed
        if returncode != 0 and not stopped:
            print(f"[setup]     {self._tool} FAILED (exit {returncode}):\n" + "\n".join(f"  | {line}" for line in tail[-10:]), flush=True)
        else:
            print(f"[setup]     {self._tool} finished", flush=True)

    def next(self) -> None:
        """Press Enter in the running tool (advance calibrate's middle/sweep steps)."""
        with self._lock:
            proc = self._proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("no setup tool is running")
        proc.stdin.write("\n")
        proc.stdin.flush()

    def stop(self, wait: float = 5.0) -> None:
        """Ctrl-C the running tool; its own cleanup (torque release) runs.

        Blocks until the tool has actually exited (up to ``wait`` seconds), so a caller
        can stop one tool and immediately start another without racing ``start()``'s
        already-running check."""
        with self._lock:
            if not self._running:
                return
            self._stop_requested = True
            proc = self._proc
        if proc is not None:
            proc.send_signal(signal.SIGINT)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            with self._lock:
                if not self._running:
                    return
            time.sleep(0.05)

    def close(self) -> None:
        with self._lock:
            self._stop_requested = True
            proc = self._proc
            proxy, self._proxy_proc = self._proxy_proc, None
        if proc is not None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if proxy is not None:
            proxy.terminate()


def make_handler(
    recorder: Recorder,
    setup: SetupRunner,
    catalog_client_factory: Callable[[], rr.catalog.CatalogClient],
    recordings_dir: Path,
) -> type[BaseHTTPRequestHandler]:
    def full_state() -> dict[str, object]:
        state = recorder.state()
        state["setup"] = setup.state()
        return state

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # silence request logging
            pass

        def _send_json(self, code: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            # The course site is served from a different origin (localhost:3000 or hosted).
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict[str, object]:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            try:
                parsed = json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}

        def do_OPTIONS(self) -> None:  # CORS preflight
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/status":
                self._send_json(200, full_state())
            elif path == "/datasets":
                try:
                    names = list(catalog_client_factory().dataset_names())
                except Exception as err:  # noqa: BLE001
                    self._send_json(500, {"error": f"{type(err).__name__}: {err}"})
                    return
                self._send_json(200, {"datasets": sorted(names)})
            elif path == "/episodes":
                dataset = (parse_qs(urlparse(self.path).query).get("dataset") or [""])[0].strip()
                if not dataset:
                    self._send_json(400, {"error": "missing ?dataset=<name>"})
                    return
                try:
                    payload = list_episodes(catalog_client_factory(), recordings_dir, dataset)
                except Exception as err:  # noqa: BLE001
                    self._send_json(500, {"error": f"{type(err).__name__}: {err}"})
                    return
                self._send_json(200, payload)
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            body = self._body()
            try:
                if path == "/arms/connect":
                    # The setup tools and the recorder both need the serial ports + cameras.
                    if setup.state()["running"]:
                        raise RuntimeError("a setup tool is running -- stop it on the Set up page first")
                    recorder.connect_arms(fake=bool(body.get("fake", False)))
                elif path == "/arms/disconnect":
                    recorder.disconnect_arms()
                elif path == "/live/pause":
                    recorder.pause_live()
                elif path == "/live/resume":
                    recorder.resume_live()
                elif path == "/start":
                    recorder.start(
                        dataset=str(body.get("dataset") or "my_dataset"),
                        task=str(body.get("task") or ""),
                        episode=str(body["episode"]) if body.get("episode") else None,
                    )
                elif path == "/stop":
                    recorder.stop(tag=str(body.get("tag") or "Needs review"))
                elif path == "/episode/update":
                    recorder.update_episode(
                        task=str(body.get("task") or ""),
                        tag=str(body.get("tag") or ""),
                        dataset=str(body["dataset"]) if body.get("dataset") else None,
                        episode=str(body["episode"]) if body.get("episode") else None,
                    )
                elif path == "/setup/start":
                    if recorder.state()["arms"] != "disconnected":
                        raise RuntimeError("the recorder holds the arms -- disconnect them on the Collect page first")
                    setup.start(str(body.get("tool") or ""))
                elif path == "/setup/next":
                    setup.next()
                elif path == "/setup/stop":
                    setup.stop()
                else:
                    self._send_json(404, {"error": "not found"})
                    return
                self._send_json(200, full_state())
            except (RuntimeError, SystemExit) as err:
                # e.g. "no SO-100 arms found", "connect the arms first" -- surface to the UI.
                state = full_state()
                state["error"] = str(err)
                self._send_json(200, state)

    return Handler


def main(config: Config) -> None:
    # SIGTERM (`pkill`, `kill`) must run the `finally` block below like Ctrl-C does --
    # otherwise the arms keep their torque and the proxy child is orphaned on port 9876.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    for port, what in ((config.grpc_port, "gRPC proxy"), (config.catalog_port, "catalog"), (config.control_port, "control API")):
        require_port(port, what)

    # 1) gRPC proxy server -- the live stream the Collect page's embedded viewer connects
    #    to. The setup tools use their own per-run throwaway proxies (see SetupRunner).
    proxy_uri = f"rerun+http://localhost:{config.grpc_port}/proxy"
    proxy_proc = spawn_proxy(config.grpc_port)
    print(f"gRPC proxy server:  {proxy_uri}")

    # 2) In-process catalog server, pre-loaded with everything already on disk:
    #    each recordings/<dataset>/ folder becomes a catalog dataset. This is what makes
    #    the server safe to shut down -- the next start re-registers it all.
    datasets = scan_recordings(config.recordings_dir)
    for name, files in datasets.items():
        print(f"[scan]      dataset '{name}': {len(files)} recording(s) from {config.recordings_dir / name}", flush=True)
    catalog_uri = f"rerun+http://localhost:{config.catalog_port}"
    # dict is invariant in its value type, hence the cast to Server's accepted union.
    server_datasets = cast("dict[str, str | os.PathLike[str] | Sequence[str | os.PathLike[str]]] | None", datasets or None)
    catalog_server = rr.server.Server(port=config.catalog_port, datasets=server_datasets)
    print(f"Catalog server:     {catalog_uri}")
    # Re-apply saved metadata edits (recordings/<dataset>/edits/*.rrd) as `edits` layers.
    for name, edit_files in scan_edits(config.recordings_dir).items():
        register_edits(catalog_uri, name, edit_files)
        print(f"[scan]      dataset '{name}': {len(edit_files)} metadata edit(s) re-applied", flush=True)
    # Re-apply saved default blueprints -- without one, the viewer opens catalog
    # episodes with its heuristic layout.
    for name, blueprint_file in scan_blueprints(config.recordings_dir).items():
        register_blueprint(catalog_uri, name, blueprint_file)
        print(f"[scan]      dataset '{name}': default blueprint re-applied", flush=True)

    recorder = Recorder(catalog_uri, config.recordings_dir, arm_fps=config.fps, cameras=config.cameras)
    setup = SetupRunner(calibration_dir=REPO_ROOT / "calibrations")
    httpd: ThreadingHTTPServer | None = None
    try:
        if config.fake:
            recorder.connect_arms(fake=True)

        # 3) Control server -- the JSON API the course site (and curl) talks to.
        handler = make_handler(recorder, setup, lambda: rr.catalog.CatalogClient(catalog_uri), config.recordings_dir)
        httpd = ThreadingHTTPServer(("localhost", config.control_port), handler)
        print(f"Control API:        http://localhost:{config.control_port}")
        print()
        print("Leave this running. Follow the course (`pixi run learn`) or record from the CLI (`pixi run record-episode`).")
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        setup.close()
        recorder.close()
        if httpd is not None:
            httpd.shutdown()
        catalog_server.shutdown()
        proxy_proc.terminate()
        try:
            proxy_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proxy_proc.kill()


@dataclasses.dataclass
class Config:
    """Ports and paths for the long-lived local data server."""

    grpc_port: int = 9876
    """Port for the Rerun gRPC proxy server (the embedded viewer connects here)."""

    catalog_port: int = 51234
    """Port for the local Rerun catalog server."""

    control_port: int = 8000
    """Port for the control API (arms + start/stop, used by the course site and curl)."""

    recordings_dir: Path = REPO_ROOT / "recordings"
    """Folder of ``<dataset>/<episode>.rrd`` files; scanned + re-registered on startup."""

    fake: bool = False
    """Connect only the camera(s) at startup, with no SO-100 arms (for testing without hardware)."""

    fps: float = 30.0
    """Target logging rate (arm poll rate) once the arms are connected."""

    cameras: tuple[int, ...] | None = None
    """Camera indices to record from (as printed by camera detection at connect time).
    Default: auto-detect — with arms this skips the Mac's built-in webcam and phone
    Continuity Cameras; ``--fake`` (no-hardware) mode keeps every camera, built-in
    included, since that may be all there is. Pass indices explicitly to override
    either (e.g. the built-in webcam as a second view when you're short a USB camera)."""


if __name__ == "__main__":
    main(tyro.cli(Config))
