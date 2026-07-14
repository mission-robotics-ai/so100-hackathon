"""Camera health check: is every plugged-in webcam actually reaching OpenCV?

Compares three layers and reports where a camera drops out:

1. USB — video-class devices the kernel enumerated (ioreg), i.e. what is
   physically plugged in and powered.
2. AVFoundation — capture devices macOS publishes to apps. A camera present on
   USB but missing here is a macOS problem (the UVC camera daemon), not this
   repo's code.
3. OpenCV — whether each AVFoundation device opens and delivers frames (this is
   exactly what log-so100 / record-episode use).

Every camera that delivers frames is then streamed into a Rerun viewer for a few
seconds so you can visually confirm the picture::

    pixi run check-cameras                 # probe + live view for 5 seconds
    pixi run check-cameras -- --seconds 30
    pixi run check-cameras -- --rr-config.headless --rr-config.save check.rrd
"""

from __future__ import annotations

import contextlib
import plistlib
import subprocess
import time
from dataclasses import dataclass, field

import cv2
import rerun.blueprint as rrb
import tyro

from so100_hackathon.cameras import CameraStreamer, _cameras_in_opencv_order
from so100_hackathon.rerun_config import LiveViewerConfig

USB_VIDEO_INTERFACE_CLASS = 14
"""bInterfaceClass of USB Video Class (UVC) interfaces — what webcams expose."""

AUTHORIZATION_STATUS = {0: "not determined (never asked)", 1: "restricted", 2: "DENIED", 3: "authorized"}
"""AVAuthorizationStatus values for camera access, per AVFoundation."""


@dataclass
class CheckCamerasConfig:
    rr_config: LiveViewerConfig = field(default_factory=LiveViewerConfig)
    seconds: float = 5.0
    """How long to stream the working cameras into the viewer."""
    max_index: int = 4
    """Highest OpenCV index to probe when AVFoundation enumeration is unavailable (non-macOS)."""
    include_skipped: bool = False
    """Also stream the cameras log-so100 skips (built-in webcam, phones); they are always probed and reported."""


def usb_video_cameras() -> list[str] | None:
    """Names of USB devices exposing a video-class interface, straight from the kernel's
    USB tree (so independent of macOS's camera daemons). None when ioreg is unavailable."""
    try:
        out = subprocess.run(
            ["/usr/sbin/ioreg", "-r", "-c", "IOUSBHostDevice", "-a", "-l"],
            capture_output=True,
            check=True,
        ).stdout
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    found: dict[object, str] = {}  # keyed by locationID: ioreg -r repeats nested devices

    def visit(node: dict) -> None:
        children = node.get("IORegistryEntryChildren", [])
        name = node.get("USB Product Name")
        if name is not None and any(
            child.get("IOObjectClass") == "IOUSBHostInterface" and child.get("bInterfaceClass") == USB_VIDEO_INTERFACE_CLASS for child in children
        ):
            found[node.get("locationID", name)] = str(name)
        for child in children:
            visit(child)

    for entry in plistlib.loads(out):
        visit(entry)
    return sorted(found.values())


def camera_authorization() -> str | None:
    """This process's camera permission (TCC) status, or None off-macOS."""
    try:
        import AVFoundation as av  # pyrefly: ignore[missing-import]  # macOS-only, guarded by except
    except ImportError:
        return None
    status = int(av.AVCaptureDevice.authorizationStatusForMediaType_(av.AVMediaTypeVideo))  # pyrefly: ignore[missing-attribute]
    return AUTHORIZATION_STATUS.get(status, f"unknown ({status})")


def main(config: CheckCamerasConfig) -> None:
    usb = usb_video_cameras()
    if usb is not None:
        print(f"USB video-class devices ({len(usb)}):", flush=True)
        for name in usb:
            print(f"  - {name}", flush=True)

    avf = _cameras_in_opencv_order()
    if avf is not None:
        print(f"AVFoundation capture devices ({len(avf)}, in OpenCV index order):", flush=True)
        for index, (name, skip) in enumerate(avf):
            note = f" [{skip} — log-so100 skips this one by default]" if skip else ""
            print(f"  {index}: {name}{note}", flush=True)

    authorization = camera_authorization()
    if authorization is not None:
        print(f"camera permission for this process: {authorization}", flush=True)

    # The layer diff this tool exists for: plugged in (USB) but invisible to apps
    # (AVFoundation) means macOS's UVC camera daemon isn't publishing the device —
    # no amount of code in this repo can see it.
    if usb is not None and avf is not None:
        external = [name for name, skip in avf if skip != "built-in"]
        if len(usb) > len(external):
            print(
                f"\nPROBLEM: {len(usb)} camera(s) on USB but only {len(external)} published to apps by macOS.\n"
                "  The missing ones are invisible to every app (not just this repo).\n"
                "  Fixes, in order: unplug/replug the cameras (or their hub); then\n"
                "  `sudo launchctl kickstart -k system/com.apple.cmio.uvcassistantextension`; then reboot.",
                flush=True,
            )

    # Probe what log-so100 actually uses: OpenCV. Indices map 1:1 to the
    # AVFoundation list; without it (non-macOS) probe 0..max_index blind.
    indices = range(len(avf)) if avf is not None else range(config.max_index + 1)
    working: list[int] = []
    for index in indices:
        cap = cv2.VideoCapture(index)
        ok = cap.isOpened() and cap.read()[0]
        size = f"{int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}" if ok else "-"
        cap.release()
        print(f"OpenCV index {index}: {'delivers frames (' + size + ')' if ok else 'NO frames'}", flush=True)
        if ok:
            working.append(index)
    if avf and not working and authorization not in (None, "authorized"):
        print(
            "\nPROBLEM: macOS publishes cameras but none open — likely a permission issue.\n"
            "  Grant your terminal Camera access: System Settings > Privacy & Security > Camera.",
            flush=True,
        )
    if not working:
        raise SystemExit("no camera delivered a frame; nothing to stream")

    # Stream what log-so100 would use: skip built-in/phone cameras unless asked.
    streamed = working
    if avf is not None and not config.include_skipped:
        streamed = [index for index in working if index >= len(avf) or avf[index][1] is None]
        for index in sorted(set(working) - set(streamed)):
            print(f"not streaming index {index} ({avf[index][0]}): {avf[index][1]} — pass --include-skipped to stream it", flush=True)
    if not streamed:
        raise SystemExit("only skipped cameras delivered frames; pass --include-skipped to stream them")

    rec = config.rr_config.rec
    rec.send_recording_name("Camera check")
    streamers = [CameraStreamer(index, rec=rec) for index in streamed]
    rec.send_blueprint(
        rrb.Blueprint(rrb.Grid(*[rrb.Spatial2DView(origin=streamer.entity_path, name=streamer.entity_path) for streamer in streamers])),
        make_active=True,
    )
    for streamer in streamers:
        streamer.start()
    print(f"streaming {len(streamers)} camera(s) for {config.seconds:.0f}s...", flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        time.sleep(config.seconds)
    for streamer in streamers:
        streamer.stop()
    print("done.", flush=True)


if __name__ == "__main__":
    main(tyro.cli(CheckCamerasConfig))
