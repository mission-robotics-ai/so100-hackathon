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

While recording, data is *teed* (``rr.set_sinks``) to two sinks at once:

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
import math
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import rerun as rr
import tyro

# The catalog client refuses localhost tokens unless we opt out of the host check.
os.environ.setdefault("RERUN_INSECURE_SKIP_HOST_CHECK", "1")

REPO_ROOT = Path(__file__).resolve().parents[2]

# TODO(calude): make these into command line args
APP_ID = "so-100"
DATASET_NAME = "recordings"
FLUSH_TICK_SECS = 0.01  # micro-batcher flush interval (10 ms)

# The web-viewer npm package is fetched once and served by the control server. Its
# version must match the installed rerun-sdk (the wasm-bindgen glue is build-specific).
WEB_VIEWER_VERSION = rr.__version__
WEB_VIEWER_FILES = ("index.js", "re_viewer.js", "re_viewer_bg.wasm")
NPM_TARBALL = "https://registry.npmjs.org/@rerun-io/web-viewer/-/web-viewer-{version}.tgz"


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


class Recorder:
    """Owns the tee'd recording: streams to the proxy + a file, registers on stop."""

    def __init__(self, proxy_uri: str, catalog_uri: str, recordings_dir: Path) -> None:
        self._proxy_uri = proxy_uri
        self._catalog_uri = catalog_uri
        self._recordings_dir = recordings_dir
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._counter = 0
        self._current_file: Path | None = None
        self._rec: rr.RecordingStream | None = None  # current recording stream
        self._last: dict[str, object] = {}  # summary of the most recent recording
        self._catalog = None  # lazily created rr.catalog.CatalogClient

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def state(self) -> dict[str, object]:
        return {"running": self.running, "last": self._last}

    def start(self) -> dict[str, object]:
        with self._lock:
            if self.running:
                return self.state()

            self._recordings_dir.mkdir(parents=True, exist_ok=True)
            self._counter += 1
            rec_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{self._counter:03d}"
            path = self._recordings_dir / f"{rec_id}.rrd"
            self._current_file = path

            # A fresh recording id per take, so each file is a distinct catalog segment.
            # A 10 ms micro-batcher flush keeps the live view low-latency.
            self._rec = rr.RecordingStream(
                APP_ID,
                recording_id=rec_id,
                make_default=True,
                batcher_config=rr.ChunkBatcherConfig(flush_tick=FLUSH_TICK_SECS),
            )

            # Tee: everything logged now goes to BOTH the proxy and the file.
            rr.set_sinks(rr.GrpcSink(url=self._proxy_uri), rr.FileSink(str(path)))

            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="recorder", daemon=True)
            self._thread.start()
            self._last = {"file": str(path), "status": "recording"}
            print(f"[recording] started {rec_id} -> {path}", flush=True)
            return self.state()

    def stop(self) -> dict[str, object]:
        with self._lock:
            if not self.running:
                return self.state()
            self._stop.set()
            thread = self._thread
            path = self._current_file

        if thread is not None:
            thread.join(timeout=2.0)

        # Drop the FileSink (flushes the footer) but keep streaming to the proxy.
        rr.set_sinks(rr.GrpcSink(url=self._proxy_uri))
        print(f"[recording] stopped -> {path}", flush=True)

        summary: dict[str, object] = {"file": str(path) if path else None, "status": "stopped"}
        if path is not None:
            try:
                self._optimize(path)
                summary["registration"] = self._register(path)
                summary["status"] = "registered"
                registration = summary["registration"]
                print(
                    f"[catalog]   registered {path} in dataset '{DATASET_NAME}' "
                    f"(segments: {registration['segment_ids']})",  # type: ignore[index]
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
        return {
            "dataset": DATASET_NAME,
            "uri": path.resolve().as_uri(),
            "segment_ids": segment_ids,
            # Deep links the running web viewer can `open()` directly.
            "viewer_urls": [dataset.segment_url(seg) for seg in segment_ids],
        }

    def _run(self) -> None:
        step = 0
        while not self._stop.is_set():
            t = step * 0.05
            rr.set_time("step", sequence=step)

            # A spinning point cloud so there is something to look at.
            angles = np.linspace(0.0, 2.0 * math.pi, 64, endpoint=False) + t
            radius = 1.0 + 0.25 * math.sin(t)
            positions = np.column_stack(
                [
                    radius * np.cos(angles),
                    radius * np.sin(angles),
                    0.15 * np.sin(3.0 * angles + t),
                ]
            )
            colors = np.column_stack(
                [
                    (0.5 + 0.5 * np.sin(angles)) * 255,
                    (0.5 + 0.5 * np.cos(angles)) * 255,
                    np.full_like(angles, 200.0),
                ]
            ).astype(np.uint8)
            rr.log("world/points", rr.Points3D(positions, colors=colors, radii=0.05))

            # A scalar plot.
            rr.log("plot/sine", rr.Scalars(math.sin(t)))

            step += 1
            time.sleep(1.0 / 30.0)


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
    #start { background: #2e7d32; }
    #stop  { background: #c62828; }
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
      <span id="status">…</span>
    </div>
  </div>
  <script type="module">
    import { WebViewer } from "/viewer/index.js";

    const PROXY_URI = "__PROXY_URI__";
    const statusEl = document.getElementById("status");
    const startBtn = document.getElementById("start");
    const stopBtn = document.getElementById("stop");

    const viewer = new WebViewer();
    const viewerReady = viewer
      .start(PROXY_URI, document.getElementById("viewer"),
             { width: "100%", height: "100%", hide_welcome_screen: true })
      .catch((err) => { statusEl.textContent = "viewer error: " + err; });

    function render(state) {
      const running = !!(state && state.running);
      startBtn.disabled = running;
      stopBtn.disabled = !running;
      const last = (state && state.last) || {};
      let msg = running ? "recording" : (last.status || "idle");
      if (last.file) msg += " · " + last.file.split("/").pop();
      if (last.error) msg += " · " + last.error;
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
            else:
                self._send(404, b"not found", "text/plain")

    return Handler


def main(config: Config) -> None:
    rr.init(APP_ID)

    # Web-viewer assets (served same-origin from the control server, see below).
    assets = ensure_web_viewer_assets(WEB_VIEWER_VERSION)

    # 1) gRPC proxy server -- the live data source the viewer streams from.
    #    It runs in its OWN process: `set_sinks` in this process would otherwise
    #    tear down an in-process `serve_grpc` server (the server is the sink),
    #    breaking the tee. As a separate process it survives our sink swaps.
    proxy_uri = f"rerun+http://localhost:{config.grpc_port}/proxy"
    proxy_proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import rerun as rr, time; rr.init('{APP_ID}'); rr.serve_grpc(grpc_port={config.grpc_port}); time.sleep(1e9)",
        ],
    )
    print(f"gRPC proxy server:  {proxy_uri}")

    # 2) OSS catalog server -- recordings are registered here on stop.
    catalog_uri = f"rerun+http://localhost:{config.catalog_port}"
    catalog_proc = subprocess.Popen(
        [sys.executable, "-m", "rerun", "server", "--port", str(config.catalog_port)],
    )
    print(f"OSS catalog server: {catalog_uri}")
    time.sleep(2.0)  # give the subprocess servers a moment to bind before the viewer connects

    # 3) Control server -- serves the page, the web-viewer assets, and the start/stop API.
    #    The viewer's `index.js` dynamically imports `./re_viewer` (extensionless) and
    #    fetches `./re_viewer_bg.wasm`, both relative to itself -- hence the `/viewer/` routes.
    asset_routes = {
        "/viewer/index.js": (assets["index.js"], "text/javascript"),
        "/viewer/re_viewer": (assets["re_viewer.js"], "text/javascript"),
        "/viewer/re_viewer.js": (assets["re_viewer.js"], "text/javascript"),
        "/viewer/re_viewer_bg.wasm": (assets["re_viewer_bg.wasm"], "application/wasm"),
    }
    recorder = Recorder(proxy_uri, catalog_uri, config.recordings_dir)
    page = build_page(proxy_uri).encode("utf-8")
    httpd = ThreadingHTTPServer(("localhost", config.control_port), make_handler(recorder, page, asset_routes))
    page_url = f"http://localhost:{config.control_port}"
    print(f"Control server:     {page_url}  (open this)")
    print()

    if config.open_browser:
        import webbrowser

        webbrowser.open(page_url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        recorder.stop()
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

    open_browser: bool = True
    """Open the page in the default browser on startup."""


if __name__ == "__main__":
    main(tyro.cli(Config))
