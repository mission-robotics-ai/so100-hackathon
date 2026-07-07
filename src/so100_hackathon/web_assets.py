"""Fetch the ``@rerun-io/web-viewer`` npm assets into a gitignored cache.

The embedded web viewer on the course site is served same-origin from these files, so
everything works offline once cached. The package version must match the installed
rerun-sdk (the wasm-bindgen glue is build-specific).
"""

from __future__ import annotations

import io
import tarfile
import urllib.request
from pathlib import Path

import rerun as rr

from so100_hackathon.console import console, success

REPO_ROOT = Path(__file__).resolve().parents[2]

WEB_VIEWER_VERSION = rr.__version__
WEB_VIEWER_FILES = ("index.js", "re_viewer.js", "re_viewer_bg.wasm")
NPM_TARBALL = "https://registry.npmjs.org/@rerun-io/web-viewer/-/web-viewer-{version}.tgz"

# Route -> (asset name, content type). The viewer's `index.js` dynamically imports
# `./re_viewer` (extensionless) and fetches `./re_viewer_bg.wasm`, both relative to
# itself -- hence the multiple routes per file.
VIEWER_ROUTES: dict[str, tuple[str, str]] = {
    "/viewer/index.js": ("index.js", "text/javascript"),
    "/viewer/re_viewer": ("re_viewer.js", "text/javascript"),
    "/viewer/re_viewer.js": ("re_viewer.js", "text/javascript"),
    "/viewer/re_viewer_bg.wasm": ("re_viewer_bg.wasm", "application/wasm"),
}


def ensure_web_viewer_assets(version: str = WEB_VIEWER_VERSION) -> dict[str, Path]:
    """Fetch the ``@rerun-io/web-viewer`` assets into a gitignored cache, return their paths."""
    cache = REPO_ROOT / ".web-viewer" / version
    targets = {name: cache / name for name in WEB_VIEWER_FILES}
    if all(path.exists() for path in targets.values()):
        return targets

    cache.mkdir(parents=True, exist_ok=True)
    url = NPM_TARBALL.format(version=version)
    with console.status(f"fetching @rerun-io/web-viewer@{version} ..."):
        with urllib.request.urlopen(url) as resp:  # noqa: S310 - trusted npm registry URL
            data = resp.read()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for name, dest in targets.items():
                member = tar.extractfile(f"package/{name}")
                if member is None:
                    raise RuntimeError(f"web-viewer tarball is missing package/{name}")
                dest.write_bytes(member.read())
    success(f"cached @rerun-io/web-viewer@{version} ({len(targets)} files)")
    return targets
