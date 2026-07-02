"""Host a Rerun proxy, an OSS web viewer, an OSS catalog server, and a control server.

Four servers are started:

* ``--grpc-port``    (default 9876):  a Rerun gRPC *proxy* server (``rr.serve_grpc``)
  -- the live data source the viewer streams from.
* ``--viewer-port``  (default 9090):  the OSS Rerun web-viewer assets
  (``rr.start_web_viewer_server``) -- i.e. the viewer HTML/wasm hosted locally
  instead of from ``app.rerun.io``.
* ``--catalog-port`` (default 51234): the OSS catalog server (``rerun server``) --
  recordings written to disk get registered here on "stop".
* ``--control-port`` (default 8000):  a small stdlib HTTP "control server" that
  serves the combined HTML page and handles the ``start`` / ``stop`` buttons.

The combined HTML page (served by the control server at ``/``) embeds the OSS
web viewer in an iframe -- loaded from the local OSS server and connected to the
local proxy server (live) and the local catalog server (browse registered
recordings) -- in the top 80%, with a "start" / "stop" control bar in the
bottom 20%.

While recording, data is *teed* (``rr.set_sinks``) to two sinks at once:

* a ``GrpcSink`` pointing at the proxy server (so the viewer shows it live), and
* a ``FileSink`` writing an ``.rrd`` file into ``recordings/`` (repo root).

On "stop", the file sink is closed (footer flushed) and the ``.rrd`` file is
registered to the OSS catalog server.

Run it with::

    pixi run dataset-collector

then open http://localhost:8000
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import subprocess
import sys
import threading
import time
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


@dataclasses.dataclass
class Config:
    """Ports and paths for the servers."""

    grpc_port: int = 9876
    """Port for the Rerun gRPC proxy server."""

    viewer_port: int = 9090
    """Port for the OSS Rerun web-viewer assets."""

    catalog_port: int = 51234
    """Port for the OSS catalog server (``rerun server``)."""

    control_port: int = 8000
    """Port for the custom control server (also serves the HTML page)."""

    recordings_dir: Path = REPO_ROOT / "recordings"
    """Folder the ``.rrd`` files are written to (default: ``recordings/`` at repo root)."""

    open_browser: bool = True
    """Open the combined page in the default browser on startup."""


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
                segments = summary["registration"].get("segment_ids")  # type: ignore[union-attr]
                print(f"[catalog]   registered {path} in dataset '{DATASET_NAME}' (segments: {segments})", flush=True)
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
        return {
            "dataset": DATASET_NAME,
            "uri": path.resolve().as_uri(),
            "segment_ids": list(getattr(result, "segment_ids", []) or []),
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


def build_page(viewer_port: int, grpc_port: int, catalog_port: int) -> str:
    """HTML: top 80% embedded OSS viewer, bottom 20% controls."""

    # The OSS web viewer reads `?url=` (repeatable) and connects to those sources:
    # the proxy for the live stream, and the catalog to browse registered recordings.
    proxy_uri = f"rerun+http://localhost:{grpc_port}/proxy"
    catalog_uri = f"rerun+http://localhost:{catalog_port}"
    viewer_src = f"http://localhost:{viewer_port}/?url={proxy_uri}&url={catalog_uri}"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Rerun control</title>
  <style>
    html, body {{ margin: 0; height: 100%; font-family: system-ui, sans-serif; }}
    #app {{ display: flex; flex-direction: column; height: 100vh; }}
    #viewer {{ flex: 0 0 80%; border: 0; width: 100%; }}
    #controls {{
      flex: 0 0 20%;
      display: flex;
      align-items: center;
      gap: 1rem;
      padding: 0 1.5rem;
      background: #1b1b1f;
      color: #eee;
      box-sizing: border-box;
    }}
    button {{
      font-size: 1.25rem;
      padding: 0.6rem 2rem;
      border: 0;
      border-radius: 0.5rem;
      cursor: pointer;
      color: #fff;
    }}
    #start {{ background: #2e7d32; }}
    #stop  {{ background: #c62828; }}
    button:disabled {{ opacity: 0.4; cursor: default; }}
    #status {{ margin-left: auto; font-size: 0.9rem; opacity: 0.85; max-width: 55%; text-align: right; }}
  </style>
</head>
<body>
  <div id="app">
    <iframe id="viewer" src="{viewer_src}" allow="fullscreen"></iframe>
    <div id="controls">
      <button id="start">start</button>
      <button id="stop">stop</button>
      <span id="status">…</span>
    </div>
  </div>
  <script>
    const statusEl = document.getElementById("status");
    const startBtn = document.getElementById("start");
    const stopBtn = document.getElementById("stop");

    function render(state) {{
      const running = !!(state && state.running);
      startBtn.disabled = running;
      stopBtn.disabled = !running;
      const last = (state && state.last) || {{}};
      let msg = running ? "recording" : (last.status || "idle");
      if (last.file) msg += " · " + last.file.split("/").pop();
      if (last.error) msg += " · " + last.error;
      statusEl.textContent = msg;
    }}

    async function call(path) {{
      try {{
        const res = await fetch(path, {{ method: "POST" }});
        render(await res.json());
      }} catch (err) {{
        statusEl.textContent = "error: " + err;
      }}
    }}

    async function refresh() {{
      try {{
        const res = await fetch("/status");
        render(await res.json());
      }} catch (err) {{
        statusEl.textContent = "control server unreachable";
      }}
    }}

    startBtn.addEventListener("click", () => call("/start"));
    stopBtn.addEventListener("click", () => call("/stop"));
    refresh();
    setInterval(refresh, 2000);
  </script>
</body>
</html>
"""


def make_handler(recorder: Recorder, page: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:  # silence request logging
            pass

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_state(self, state: dict[str, object]) -> None:
            self._send(200, json.dumps(state).encode("utf-8"), "application/json")

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/status":
                self._send_state(recorder.state())
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

    # 2) OSS web-viewer assets -- the viewer HTML/wasm hosted locally.
    rr.start_web_viewer_server(port=config.viewer_port)
    print(f"OSS web viewer:     http://localhost:{config.viewer_port}")

    # 3) OSS catalog server -- recordings are registered here on stop.
    catalog_uri = f"rerun+http://localhost:{config.catalog_port}"
    catalog_proc = subprocess.Popen(
        [sys.executable, "-m", "rerun", "server", "--port", str(config.catalog_port)],
    )
    print(f"OSS catalog server: {catalog_uri}")
    time.sleep(2.0)  # give the subprocess servers a moment to bind before the viewer connects

    # 4) Control server -- serves the combined HTML page and the start/stop API.
    recorder = Recorder(proxy_uri, catalog_uri, config.recordings_dir)
    page = build_page(config.viewer_port, config.grpc_port, config.catalog_port)
    httpd = ThreadingHTTPServer(("localhost", config.control_port), make_handler(recorder, page))
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


if __name__ == "__main__":
    main(tyro.cli(Config))
