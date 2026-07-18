"""Webcam capture threads that stream JPEG-compressed frames into Rerun.

Python equivalent of rerun-io/portugal ``src/camera.rs`` (one thread per
camera, wall-clock timestamps), using OpenCV instead of nokhwa.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import cv2
import rerun as rr

# Cameras that are never recording cameras on this rig: the Mac's internal webcam, and
# iPhone/iPad Continuity Cameras (macOS auto-adds any signed-in phone that is nearby, so
# anyone running this with an iPhone would silently record their phone camera).
# ``isContinuityCamera``/``modelID`` and the "...BuiltIn..." device type are
# authoritative; names are a fallback for older reporting (a phone's camera is named
# after the phone, e.g. "Gavrelina Camera", so names alone can't catch it).
BUILTIN_NAME_HINTS = ("facetime", "built-in", "macbook")
PHONE_NAME_HINTS = ("iphone", "ipad", "continuity")


def _cameras_in_opencv_order() -> list[tuple[str, str | None]] | None:
    """(name, skip_reason) per camera, in OpenCV's index order. None if unavailable.
    ``skip_reason`` is None for regular (usable) cameras.

    OpenCV's AVFoundation backend (cap_avfoundation_mac.mm, checked at the installed
    version) enumerates devicesWithMediaType Video + Muxed and then SORTS BY uniqueID —
    replicate exactly that. Naive orderings mis-map: system_profiler, the raw
    devicesWithMediaType list, and discovery sessions all put the built-in first on this
    rig, while the uniqueID sort puts it last (USB uniqueIDs are "0x..." hex strings,
    which sort before the built-in's UUID).
    """
    try:
        import AVFoundation as av  # pyrefly: ignore[missing-import]  # macOS-only, guarded by except
    except ImportError:
        return None
    # pyobjc loads ObjC names lazily; pyrefly can't see them.
    devices = list(av.AVCaptureDevice.devicesWithMediaType_(av.AVMediaTypeVideo))  # pyrefly: ignore[missing-attribute]
    devices += list(av.AVCaptureDevice.devicesWithMediaType_(av.AVMediaTypeMuxed))  # pyrefly: ignore[missing-attribute]
    devices.sort(key=lambda device: str(device.uniqueID()))
    cameras: list[tuple[str, str | None]] = []
    for device in devices:
        name = str(device.localizedName())
        device_type = str(device.deviceType())
        lowered = name.lower()
        # A Continuity Camera reports deviceType "External" (same as USB webcams);
        # isContinuityCamera is the reliable flag, modelID ("iPhone14,7") the backstop.
        continuity = bool(device.isContinuityCamera()) if hasattr(device, "isContinuityCamera") else False
        model = str(device.modelID()).lower()
        skip: str | None = None
        if continuity or model.startswith(("iphone", "ipad")) or any(hint in lowered for hint in PHONE_NAME_HINTS):
            skip = "phone (Continuity Camera)"
        elif "BuiltIn" in device_type or any(hint in lowered for hint in BUILTIN_NAME_HINTS):
            skip = "built-in"
        cameras.append((name, skip))
    return cameras


def detect_camera_indices(max_index: int = 4, *, include_all: bool = False) -> tuple[int, ...]:
    """Probe AVFoundation indices and return the ones that deliver frames, skipping the
    Mac's built-in webcam and iPhone/iPad Continuity Cameras. Every detected camera is
    printed by name, so it's always visible what got picked up. Pass ``include_all=True``
    (or ``--cameras`` explicitly) to keep the skipped ones."""
    found: list[int] = []
    for index in range(max_index + 1):
        cap = cv2.VideoCapture(index)
        ok = cap.isOpened() and cap.read()[0]
        cap.release()
        if ok:
            found.append(index)

    cameras = _cameras_in_opencv_order()
    if cameras is None:  # non-macOS: no device metadata to classify with
        return tuple(found)
    kept: list[int] = []
    for index in found:
        name, skip = cameras[index] if index < len(cameras) else ("?", None)
        if skip and not include_all:
            print(f"camera {index} ({name}): {skip}, skipping — pass --cameras {index} to use it anyway", flush=True)
        else:
            print(f"camera {index} ({name}): selected", flush=True)
            kept.append(index)
    return tuple(kept)


class RecordingFanout:
    """The tiny slice of the RecordingStream API the frame loops use (``set_time`` +
    ``log``), fanned out to several recordings. Lets a source log every frame into the
    always-on live stream *and* a take file at the same time."""

    def __init__(self, *recs: rr.RecordingStream) -> None:
        self.recs = recs

    def set_time(self, *args: Any, **kwargs: Any) -> None:
        for rec in self.recs:
            rec.set_time(*args, **kwargs)

    def log(self, *args: Any, **kwargs: Any) -> None:
        for rec in self.recs:
            rec.log(*args, **kwargs)


FrameSink = rr.RecordingStream | RecordingFanout
"""What the per-frame loops log into: a single recording or a fan-out over several."""


def require_camera_indices(indices: tuple[int, ...]) -> None:
    """Fail loudly if an explicitly requested camera index cannot deliver a frame.

    Auto-detection only returns cameras that answered a probe, but explicit indices
    (``--cameras``) are used verbatim -- a typo'd or unplugged index would otherwise
    only fail later, inside a background :class:`CameraStreamer` thread, after the
    server already reported itself connected and recordable.
    """
    for index in indices:
        cap = cv2.VideoCapture(index)
        ok = cap.isOpened() and cap.read()[0]
        cap.release()
        if not ok:
            raise RuntimeError(f"camera {index} (from --cameras) is not delivering frames -- run `pixi run check-cameras` to see what is live")


class CameraSource:
    """A camera-only data source (no arms), for testing the collection loop without hardware.

    Same always-on interface as :class:`~so100_hackathon.apis.log_arms.ArmSession`. Each
    :class:`CameraStreamer` runs its own thread and logs into the current sink;
    :meth:`set_output` redirects them (e.g. to a live+file fan-out while a take records).
    """

    def __init__(self, rec: rr.RecordingStream, jpeg_quality: int = 75, cameras: tuple[int, ...] | None = None) -> None:
        indices = cameras if cameras is not None else detect_camera_indices(include_all=True)
        self.streamers = [CameraStreamer(index, rec=rec, jpeg_quality=jpeg_quality) for index in indices]
        print(f"data source:        {len(self.streamers)} camera(s) (fake, no arms)", flush=True)

    def start(self) -> None:
        for streamer in self.streamers:
            streamer.start()

    def begin(self, rec: rr.RecordingStream) -> None:
        # Cameras have no static data to (re-)log; just point the threads at rec.
        self.set_output(rec)

    def set_output(self, rec: FrameSink) -> None:
        # Redirect the camera threads (they snapshot their sink per frame).
        for streamer in self.streamers:
            streamer.rec = rec

    def close(self) -> None:
        for streamer in self.streamers:
            streamer.stop()


class CameraStreamer:
    """Capture one camera on a daemon thread and log frames under ``camera/cam<index>``."""

    def __init__(self, index: int, *, rec: FrameSink, timeline: str = "time", jpeg_quality: int = 75) -> None:
        self.index = index
        self.rec: FrameSink = rec
        """Sink to log into; reassign to redirect this thread's frames (the collector swaps takes)."""
        self.timeline = timeline
        self.jpeg_quality = jpeg_quality
        self.entity_path = f"camera/cam{index}"
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"camera-{index}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        cap = cv2.VideoCapture(self.index)
        if not cap.isOpened():
            print(f"camera {self.index}: failed to open, skipping", flush=True)
            return
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        print(f"camera {self.index}: streaming {width}x{height} -> {self.entity_path}", flush=True)
        try:
            while not self._stop.is_set():
                ok, frame_bgr = cap.read()
                if not ok:
                    time.sleep(0.1)
                    continue
                rec = self.rec  # snapshot: the collector may swap it between frames
                # set_time is thread-local, so this timeline value only affects this thread's logs.
                rec.set_time(self.timeline, timestamp=time.time())
                frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
                rec.log(self.entity_path, rr.Image(frame_rgb).compress(jpeg_quality=self.jpeg_quality))
        except Exception as error:  # a crashed feed must be visible, not a silent thread death
            print(f"camera {self.index}: streaming stopped: {error}", flush=True)
        finally:
            cap.release()
