"""SO-100 dataset collector: record takes to disk and register them in a local Rerun catalog.

Three servers are started:

* ``--grpc-port``    (default 9876):  a Rerun gRPC *proxy* server (``rr.serve_grpc``,
  in its own process) -- the live data source the viewer streams from.
* ``--catalog-port`` (default 51234): the OSS catalog server (``rerun server``) --
  recordings written to disk get registered here on "stop".
* ``--control-port`` (default 8000):  a stdlib HTTP "control server" that serves the
  page, the ``@rerun-io/web-viewer`` assets, and the ``start`` / ``stop`` API.

The page (served at ``/``) bootstraps the Rerun web viewer itself using the
``@rerun-io/web-viewer`` npm package (fetched once into a gitignored cache and served
from the control server, so everything is same-origin and offline-friendly). The top
80% is the viewer, the bottom 20% is a "start" / "stop" control bar.

The recorded data is the real SO-100 arms + cameras (see ``apis/log_arms.py``), or just
the connected camera(s) with ``--fake`` (no arms).

While recording, data is *teed* (``rec.set_sinks``) to two sinks at once:

* a ``GrpcSink`` pointing at the proxy server (so the viewer shows it live), and
* a ``FileSink`` writing an ``.rrd`` file into ``recordings/`` (repo root).

On "stop", the file sink is closed (footer flushed), the ``.rrd`` is compacted with
``rerun rrd optimize``, then registered to the OSS catalog server -- and the running
viewer is told to ``open()`` the freshly registered recording (no reload).

Run it with::

    pixi run dataset-collector

then open http://localhost:8000
"""

from __future__ import annotations

import dataclasses
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import rerun as rr
import tyro

from so100_hackathon.apis.log_arms import ArmSession, LogArmsConfig
from so100_hackathon.cameras import CameraStreamer, detect_camera_indices
from so100_hackathon.rerun_config import LiveViewerConfig

# The catalog client refuses localhost tokens unless we opt out of the host check.
os.environ.setdefault("RERUN_INSECURE_SKIP_HOST_CHECK", "1")
# Low-latency micro-batcher (8 ms flush, == ChunkBatcherConfig.LOW_LATENCY) for every
# recording in this process, the live preview, and the proxy subprocess (inherits env).
os.environ.setdefault("RERUN_FLUSH_TICK_SECS", "0.008")

REPO_ROOT = Path(__file__).resolve().parents[2]

# TODO(calude): make these into command line args
APP_ID = "so-100"
DATASET_NAME = "recordings"

# The "export to LeRobot" button runs rerun-lerobot in an isolated `uvx` env (see
# Recorder._run_export). >=0.2.0 re-encodes our JPEG camera frames to h264.
LEROBOT_REQUIREMENT = "rerun-lerobot>=0.3.0"

# The web-viewer npm package is fetched once and served by the control server. Its
# version must match the installed rerun-sdk (the wasm-bindgen glue is build-specific).
WEB_VIEWER_VERSION = rr.__version__
WEB_VIEWER_FILES = ("index.js", "re_viewer.js", "re_viewer_bg.wasm")
NPM_TARBALL = "https://registry.npmjs.org/@rerun-io/web-viewer/-/web-viewer-{version}.tgz"


@dataclasses.dataclass
class _NoViewerConfig(LiveViewerConfig):
    """A ``LiveViewerConfig`` whose ``__post_init__`` does nothing.

    The dataset collector owns all Rerun setup (recording ids, sinks, ports). The arm
    session must NOT spawn a native viewer or re-init the recording, which the normal
    ``LiveViewerConfig.__post_init__`` would do.
    """

    def __post_init__(self) -> None:
        pass


def pick_port(preferred: int) -> int:
    """Return ``preferred`` if it is free, otherwise an OS-assigned free port."""
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("localhost", candidate))
                port = sock.getsockname()[1]
            except OSError:
                continue
        if candidate != preferred:
            print(f"port {preferred} is busy, using {port} instead", flush=True)
        return port
    raise RuntimeError("could not find a free port")


def ensure_web_viewer_assets(version: str) -> dict[str, Path]:
    """Fetch the ``@rerun-io/web-viewer`` assets into a gitignored cache, return their paths."""
    cache = REPO_ROOT / ".web-viewer" / version
    targets = {name: cache / name for name in WEB_VIEWER_FILES}
    if all(path.exists() for path in targets.values()):
        return targets

    cache.mkdir(parents=True, exist_ok=True)
    url = NPM_TARBALL.format(version=version)
    print(f"Fetching @rerun-io/web-viewer@{version} ...", flush=True)
    with urllib.request.urlopen(url) as resp:  # noqa: S310 - trusted npm registry URL
        data = resp.read()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for name, dest in targets.items():
            member = tar.extractfile(f"package/{name}")
            if member is None:
                raise RuntimeError(f"web-viewer tarball is missing package/{name}")
            dest.write_bytes(member.read())
    return targets


class CameraSource:
    """The ``--fake`` source: just the connected camera(s), no arms.

    Same always-on interface as :class:`~so100_hackathon.apis.log_arms.ArmSession`. Each
    ``CameraStreamer`` runs its own thread and logs into the current recording (:meth:`begin`
    redirects them to a take's recording), so recording takes just tees those frames to disk.
    """

    def __init__(self, rec: rr.RecordingStream, jpeg_quality: int = 75) -> None:
        indices = detect_camera_indices(include_builtin=True)
        self.streamers = [CameraStreamer(index, rec=rec, jpeg_quality=jpeg_quality) for index in indices]
        print(f"data source:        {len(self.streamers)} camera(s) (--fake, no arms)", flush=True)

    def start(self) -> None:
        for streamer in self.streamers:
            streamer.start()

    def begin(self, rec: rr.RecordingStream) -> None:
        # Redirect the camera threads into rec (they snapshot it per frame).
        for streamer in self.streamers:
            streamer.rec = rec

    def close(self) -> None:
        for streamer in self.streamers:
            streamer.stop()


class Recorder:
    """Controls recording *takes*: the source streams continuously; start creates a fresh
    recording (redirecting the source to it) teed to proxy+file, stop drops the file sink
    back to proxy-only, then optimizes + registers the file.
    """

    def __init__(
        self,
        proxy_uri: str,
        catalog_uri: str,
        recordings_dir: Path,
        source: CameraSource | ArmSession,
        lerobot_dir: Path,
        fps: float,
    ) -> None:
        self._proxy_uri = proxy_uri
        self._catalog_uri = catalog_uri
        self._recordings_dir = recordings_dir
        self._source = source
        self._lerobot_dir = lerobot_dir
        self._fps = fps
        self._lock = threading.Lock()
        self._recording = False
        self._counter = 0
        self._current_file: Path | None = None
        self._rec: rr.RecordingStream | None = None  # current recording stream
        self._last: dict[str, object] = {}  # summary of the most recent recording
        self._catalog = None  # lazily created rr.catalog.CatalogClient
        self._export: dict[str, object] = {}  # status of the most recent LeRobot export

    @property
    def running(self) -> bool:
        return self._recording

    def state(self) -> dict[str, object]:
        return {"running": self._recording, "last": self._last, "export": self._export}

    def start(self) -> dict[str, object]:
        with self._lock:
            if self._recording:
                return self.state()

            self._recordings_dir.mkdir(parents=True, exist_ok=True)
            self._counter += 1
            rec_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{self._counter:03d}"
            path = self._recordings_dir / f"{rec_id}.rrd"
            self._current_file = path

            # A fresh recording id per take, so each file is a distinct catalog segment.
            # Low-latency batcher so the live view keeps up while we also write to disk.
            self._rec = rr.RecordingStream(
                APP_ID,
                recording_id=rec_id,
                batcher_config=rr.ChunkBatcherConfig.LOW_LATENCY(),
            )
            # Tee: everything logged now goes to BOTH the proxy and the file.
            self._rec.set_sinks(rr.GrpcSink(url=self._proxy_uri), rr.FileSink(str(path)))
            # Redirect the always-on source into this take's recording and (re-)log the
            # static series + URDF + blueprint into it.
            self._source.begin(self._rec)

            self._recording = True
            self._last = {"file": str(path), "status": "recording"}
            print(f"[recording] started {rec_id} -> {path}", flush=True)
            return self.state()

    def stop(self) -> dict[str, object]:
        with self._lock:
            if not self._recording:
                return self.state()
            self._recording = False
            path = self._current_file
            assert self._rec is not None  # set by start() before _recording flips true

        # Drop the FileSink (flushes the footer) but keep streaming to the proxy.
        self._rec.set_sinks(rr.GrpcSink(url=self._proxy_uri))
        print(f"[recording] stopped -> {path}", flush=True)

        summary: dict[str, object] = {"file": str(path) if path else None, "status": "stopped"}
        if path is not None:
            try:
                self._optimize(path)
                summary["registration"] = self._register(path)
                summary["status"] = "registered"
                registration = summary["registration"]
                print(
                    f"[catalog]   registered {path} in dataset '{DATASET_NAME}' (segments: {registration['segment_ids']})",  # type: ignore[index]
                    flush=True,
                )
            except Exception as err:  # noqa: BLE001 - surface any failure to the UI
                summary["status"] = "register_failed"
                summary["error"] = f"{type(err).__name__}: {err}"
                print(f"[catalog]   registration FAILED for {path}: {summary['error']}", flush=True)

        with self._lock:
            self._last = summary
        return self.state()

    def _optimize(self, path: Path) -> None:
        """Compact the recording's chunks in place via `rerun rrd optimize`."""
        tmp = path.with_name(path.name + ".tmp")
        proc = subprocess.run(
            [sys.executable, "-m", "rerun", "rrd", "optimize", str(path), "-o", str(tmp)],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"rerun rrd optimize failed: {proc.stderr.strip()}")
        os.replace(tmp, path)
        print(f"[optimize]  compacted {path}", flush=True)

    def _register(self, path: Path) -> dict[str, object]:
        if self._catalog is None:
            self._catalog = rr.catalog.CatalogClient(self._catalog_uri)
        dataset = self._catalog.create_dataset(DATASET_NAME, exist_ok=True)
        handle = dataset.register([path.resolve().as_uri()])
        result = handle.wait()
        segment_ids = list(getattr(result, "segment_ids", []) or [])
        registration: dict[str, object] = {
            "dataset": DATASET_NAME,
            "uri": path.resolve().as_uri(),
            "segment_ids": segment_ids,
            # Deep links the running web viewer can `open()` directly.
            "viewer_urls": [dataset.segment_url(seg) for seg in segment_ids],
        }
        registration.update(self._verify_registration(segment_ids))
        return registration

    def _verify_registration(self, expected_segments: list[str]) -> dict[str, object]:
        """Ask the catalog server (fresh query) to confirm the recording landed in the dataset."""
        assert self._catalog is not None
        # A) all datasets.
        dataset_names = list(self._catalog.dataset_names())
        print(f"[catalog]   datasets on server: {dataset_names}", flush=True)
        # B) all recordings in our dataset -- fresh handle, so it reflects server state.
        server_segments = list(self._catalog.get_dataset(DATASET_NAME).segment_ids())
        print(f"[catalog]   '{DATASET_NAME}' has {len(server_segments)} recording(s): {server_segments}", flush=True)
        missing = [seg for seg in expected_segments if seg not in server_segments]
        if missing:
            print(f"[catalog]   ERROR: {missing} was registered but is NOT in the dataset on the server!", flush=True)
        return {"server_recordings": server_segments, "missing": missing}

    # --- LeRobot export -------------------------------------------------------

    def _lerobot_specs(self) -> dict[str, object]:
        """Derive the ``rerun-lerobot`` column/video specs from the live data source.

        ``action`` is the follower's commanded goal, ``state`` its measured position, and each
        camera becomes a video stream. Raises ``RuntimeError`` if the source has no arms (e.g.
        ``--fake``), since a LeRobot dataset needs an action + state.
        """
        source = self._source
        # rerun-lerobot resolves fully-qualified, absolute entity paths (leading slash).
        videos = [(f"cam{streamer.index}", f"/{streamer.entity_path}") for streamer in getattr(source, "streamers", [])]

        warnings: list[str] = []
        if not isinstance(source, ArmSession) or not source.arms:
            raise RuntimeError("no arm data to export (running with --fake?); connect the SO-100 arms and record a take first")

        follower = source.follower
        if follower is not None:
            state_arm = follower.name
            action = f"/{follower.name}/goal:Scalars:scalars"
        else:
            # No teleop -> no /goal channel. Fall back to a (degenerate) position-only export.
            state_arm = source.arms[0].name
            action = f"/{state_arm}/position:Scalars:scalars"
            warnings.append("no teleop follower: exporting position as both action and state")

        return {
            "action": action,
            "state": f"/{state_arm}/position:Scalars:scalars",
            "videos": videos,
            "warnings": warnings,
        }

    def export(self) -> dict[str, object]:
        """Kick off a background LeRobot conversion of the recorded ``.rrd`` files."""
        with self._lock:
            if self._recording:
                self._export = {"status": "error", "error": "stop recording before exporting"}
                return self._export
            if self._export.get("status") == "running":
                return self._export
            try:
                specs = self._lerobot_specs()
            except RuntimeError as err:
                self._export = {"status": "error", "error": str(err)}
                return self._export
            self._export = {"status": "running", "warnings": specs["warnings"]}
            threading.Thread(target=self._run_export, args=(specs,), name="lerobot-export", daemon=True).start()
            return self._export

    def _run_export(self, specs: dict[str, object]) -> None:
        out = self._lerobot_dir / f"{DATASET_NAME}-{time.strftime('%Y%m%d-%H%M%S')}"
        # Run rerun-lerobot in an isolated uv tool env, NOT our pixi env: it drags in torch,
        # opencv, wandb, ... (all core lerobot deps). `uvx` keeps that heavy dependency tree
        # out of the app env.
        cmd = [
            "uvx",
            f"--from={LEROBOT_REQUIREMENT}",
            "rerun-lerobot",
            f"--rrd-dir={self._recordings_dir}",
            f"--output={out}",
            f"--dataset-name={DATASET_NAME}",
            f"--repo-id={DATASET_NAME}",
            f"--fps={int(round(self._fps))}",
            "--index=time",
            f"--action={specs['action']}",
            f"--state={specs['state']}",
        ]
        for key, path in specs["videos"]:  # type: ignore[union-attr]
            cmd += [f"--video={key}:{path}"]

        print(f"[export]    {' '.join(cmd)}", flush=True)
        summary: dict[str, object] = {"output": str(out), "warnings": specs["warnings"]}
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError as err:
            summary["status"] = "error"
            summary["error"] = f"could not run uvx: {err} (is `uv` installed? run `pixi install`)"
        else:
            if proc.returncode == 0:
                summary["status"] = "done"
                print(f"[export]    LeRobot dataset written to {out}", flush=True)
            else:
                output = (proc.stderr or proc.stdout).strip()
                tail = output.splitlines()[-1] if output else "conversion failed"
                summary["status"] = "error"
                summary["error"] = tail
                print(f"[export]    FAILED:\n{output}", flush=True)

        with self._lock:
            self._export = summary


# Served at `/`. The viewer is bootstrapped from the `@rerun-io/web-viewer` package
# (served from `/viewer/`), so we can drive it at runtime -- `viewer.open(url)` opens
# the freshly registered recording without reloading. `__PROXY_URI__` is substituted in.
PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>SO-100 dataset collector</title>
  <style>
    html, body { margin: 0; height: 100%; font-family: system-ui, sans-serif; }
    #app { display: flex; flex-direction: column; height: 100vh; }
    #viewer { flex: 0 0 80%; width: 100%; position: relative; overflow: hidden; }
    #controls {
      flex: 0 0 20%;
      display: flex;
      align-items: center;
      gap: 1rem;
      padding: 0 1.5rem;
      background: #1b1b1f;
      color: #eee;
      box-sizing: border-box;
    }
    button {
      font-size: 1.25rem;
      padding: 0.6rem 2rem;
      border: 0;
      border-radius: 0.5rem;
      cursor: pointer;
      color: #fff;
    }
    #start  { background: #2e7d32; }
    #stop   { background: #c62828; }
    #export { background: #1565c0; }
    button:disabled { opacity: 0.4; cursor: default; }
    #status { margin-left: auto; font-size: 0.9rem; opacity: 0.85; max-width: 55%; text-align: right; }
  </style>
</head>
<body>
  <div id="app">
    <div id="viewer"></div>
    <div id="controls">
      <button id="start">start</button>
      <button id="stop">stop</button>
      <button id="export">export to LeRobot</button>
      <span id="status">…</span>
    </div>
  </div>
  <script type="module">
    import { WebViewer } from "/viewer/index.js";

    const PROXY_URI = "__PROXY_URI__";
    const statusEl = document.getElementById("status");
    const startBtn = document.getElementById("start");
    const stopBtn = document.getElementById("stop");
    const exportBtn = document.getElementById("export");

    const viewer = new WebViewer();
    const viewerReady = viewer
      .start(PROXY_URI, document.getElementById("viewer"),
             { width: "100%", height: "100%", hide_welcome_screen: true })
      .catch((err) => { statusEl.textContent = "viewer error: " + err; });

    function render(state) {
      const running = !!(state && state.running);
      const exp = (state && state.export) || {};
      const exporting = exp.status === "running";
      startBtn.disabled = running || exporting;
      stopBtn.disabled = !running;
      exportBtn.disabled = running || exporting;
      const last = (state && state.last) || {};
      let msg = running ? "recording" : (last.status || "idle");
      if (last.file) msg += " · " + last.file.split("/").pop();
      if (last.error) msg += " · " + last.error;
      if (exp.status === "running") msg += " · exporting to LeRobot…";
      else if (exp.status === "done") msg += " · exported to " + exp.output.split("/").pop();
      else if (exp.status === "error") msg += " · export error: " + exp.error;
      statusEl.textContent = msg;
    }

    async function refresh() {
      try {
        render(await (await fetch("/status")).json());
      } catch (err) {
        statusEl.textContent = "control server unreachable";
      }
    }

    startBtn.addEventListener("click", async () => {
      try {
        render(await (await fetch("/start", { method: "POST" })).json());
      } catch (err) {
        statusEl.textContent = "error: " + err;
      }
    });

    stopBtn.addEventListener("click", async () => {
      try {
        const state = await (await fetch("/stop", { method: "POST" })).json();
        render(state);
        const urls = (state.last && state.last.registration && state.last.registration.viewer_urls) || [];
        await viewerReady;
        for (const url of urls) {
          try { viewer.open(url); } catch (err) { console.error("viewer.open failed", url, err); }
        }
      } catch (err) {
        statusEl.textContent = "error: " + err;
      }
    });

    exportBtn.addEventListener("click", async () => {
      try {
        render(await (await fetch("/export", { method: "POST" })).json());
      } catch (err) {
        statusEl.textContent = "error: " + err;
      }
    });

    refresh();
    setInterval(refresh, 2000);
  </script>
</body>
</html>
"""


def build_page(proxy_uri: str) -> str:
    return PAGE_TEMPLATE.replace("__PROXY_URI__", proxy_uri)


def make_handler(
    recorder: Recorder,
    page: bytes,
    asset_routes: dict[str, tuple[Path, str]],
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # silence request logging
            pass

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path: Path, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(path.stat().st_size))
            self.end_headers()
            with path.open("rb") as file:
                shutil.copyfileobj(file, self.wfile)

        def _send_state(self, state: dict[str, object]) -> None:
            self._send(200, json.dumps(state).encode("utf-8"), "application/json")

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, page, "text/html; charset=utf-8")
            elif path == "/status":
                self._send_state(recorder.state())
            elif path in asset_routes:
                file_path, content_type = asset_routes[path]
                self._send_file(file_path, content_type)
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/start":
                self._send_state(recorder.start())
            elif path == "/stop":
                self._send_state(recorder.stop())
            elif path == "/export":
                recorder.export()
                self._send_state(recorder.state())
            else:
                self._send(404, b"not found", "text/plain")

    return Handler


def main(config: Config) -> None:
    # The live-preview recording, used when not recording (until the first take, and between
    # takes). Recorder.start() swaps in a fresh per-take recording (and redirects the source
    # to it) on "start"; stop() drops back to this "live" recording.
    rec = rr.RecordingStream(APP_ID, recording_id="live")

    # Web-viewer assets (served same-origin from the control server, see below).
    assets = ensure_web_viewer_assets(WEB_VIEWER_VERSION)

    # Fall back to a free port if a preferred one is already taken.
    grpc_port = pick_port(config.grpc_port)
    catalog_port = pick_port(config.catalog_port)
    control_port = pick_port(config.control_port)

    # 1) gRPC proxy server -- the live data source the viewer streams from.
    #    It runs in its OWN process: `set_sinks` in this process would otherwise
    #    tear down an in-process `serve_grpc` server (the server is the sink),
    #    breaking the tee. As a separate process it survives our sink swaps.
    proxy_uri = f"rerun+http://localhost:{grpc_port}/proxy"
    proxy_proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import rerun as rr, time; rr.init('{APP_ID}'); rr.serve_grpc(grpc_port={grpc_port}); time.sleep(1e9)",
        ],
    )
    print(f"gRPC proxy server:  {proxy_uri}")

    # 2) OSS catalog server -- recordings are registered here on stop.
    catalog_uri = f"rerun+http://localhost:{catalog_port}"
    catalog_proc = subprocess.Popen(
        [sys.executable, "-m", "rerun", "server", "--port", str(catalog_port)],
    )
    print(f"OSS catalog server: {catalog_uri}")

    recorder: Recorder | None = None
    source: CameraSource | ArmSession | None = None
    httpd: ThreadingHTTPServer | None = None
    try:
        time.sleep(2.0)  # give the subprocess servers a moment to bind

        # Stream to the proxy before the first recording, so the viewer shows a live preview
        # (and static data logged when the source opens goes to a real sink, not a dropped buffer).
        rec.set_sinks(rr.GrpcSink(url=proxy_uri))

        # The data source: just the camera(s) (--fake), or the real SO-100 arms + cameras
        # (with the follower mirroring the leader via teleop, unless --no-teleop).
        if config.fake:
            source = CameraSource(rec)
        else:
            source = ArmSession(LogArmsConfig(fps=config.fps, teleop=config.teleop, rr_config=_NoViewerConfig()), rec)
            print("data source:        real SO-100 arms", flush=True)
        source.start()  # start the always-on source thread (and arm teleop) now that a sink exists
        source.begin(rec)  # blueprint + static geometry for the live preview (before any take)

        # 3) Control server -- serves the page, the web-viewer assets, and the start/stop API.
        #    The viewer's `index.js` dynamically imports `./re_viewer` (extensionless) and
        #    fetches `./re_viewer_bg.wasm`, both relative to itself -- hence the `/viewer/` routes.
        asset_routes = {
            "/viewer/index.js": (assets["index.js"], "text/javascript"),
            "/viewer/re_viewer": (assets["re_viewer.js"], "text/javascript"),
            "/viewer/re_viewer.js": (assets["re_viewer.js"], "text/javascript"),
            "/viewer/re_viewer_bg.wasm": (assets["re_viewer_bg.wasm"], "application/wasm"),
        }
        recorder = Recorder(proxy_uri, catalog_uri, config.recordings_dir, source, config.lerobot_dir, config.fps)
        page = build_page(proxy_uri).encode("utf-8")
        httpd = ThreadingHTTPServer(("localhost", control_port), make_handler(recorder, page, asset_routes))
        page_url = f"http://localhost:{control_port}"
        print(f"Control server:     {page_url}  (open this)")
        print()

        if config.open_browser:
            import webbrowser

            webbrowser.open(page_url)

        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        if recorder is not None:
            recorder.stop()
        if source is not None:
            source.close()
        if httpd is not None:
            httpd.shutdown()
        proxy_proc.terminate()
        catalog_proc.terminate()


@dataclasses.dataclass
class Config:
    """Ports and paths for the servers."""

    grpc_port: int = 9876
    """Port for the Rerun gRPC proxy server."""

    catalog_port: int = 51234
    """Port for the OSS catalog server (``rerun server``)."""

    control_port: int = 8000
    """Port for the control server (serves the page, web-viewer assets, and start/stop API)."""

    recordings_dir: Path = REPO_ROOT / "recordings"
    """Folder the ``.rrd`` files are written to (default: ``recordings/`` at repo root)."""

    lerobot_dir: Path = REPO_ROOT / "lerobot"
    """Folder LeRobot exports are written to (a timestamped subfolder per export)."""

    fake: bool = False
    """Capture only the connected camera(s), with no SO-100 arms (for testing without hardware)."""

    teleop: bool = True
    """Drive the follower to mirror the leader while collecting (needs two calibrated arms)."""

    fps: float = 30.0
    """Target logging rate (arm poll rate when recording real arms)."""

    open_browser: bool = True
    """Open the page in the default browser on startup."""


if __name__ == "__main__":
    main(tyro.cli(Config))
