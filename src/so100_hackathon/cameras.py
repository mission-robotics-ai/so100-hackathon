"""Webcam capture threads that stream JPEG-compressed frames into Rerun.

Python equivalent of rerun-io/portugal ``src/camera.rs`` (one thread per
camera, wall-clock timestamps), using OpenCV instead of nokhwa.
"""

from __future__ import annotations

import threading
import time

import cv2
import rerun as rr

# The Mac's internal webcam is never a recording camera on this rig. The device type is
# authoritative ("...BuiltIn..." on macOS); names are a fallback for older reporting.
BUILTIN_NAME_HINTS = ("facetime", "built-in", "macbook")


def _cameras_in_opencv_order() -> list[tuple[str, bool]] | None:
    """(name, is_builtin) per camera, in OpenCV's index order. None if unavailable.

    OpenCV's AVFoundation backend (cap_avfoundation_mac.mm, checked at the installed
    version) enumerates devicesWithMediaType Video + Muxed and then SORTS BY uniqueID —
    replicate exactly that. Naive orderings mis-map: system_profiler, the raw
    devicesWithMediaType list, and discovery sessions all put the built-in first on this
    rig, while the uniqueID sort puts it last (USB uniqueIDs are "0x..." hex strings,
    which sort before the built-in's UUID).
    """
    try:
        import AVFoundation as av
    except ImportError:
        return None
    # pyobjc loads ObjC names lazily; pyrefly can't see them.
    devices = list(av.AVCaptureDevice.devicesWithMediaType_(av.AVMediaTypeVideo))  # pyrefly: ignore[missing-attribute]
    devices += list(av.AVCaptureDevice.devicesWithMediaType_(av.AVMediaTypeMuxed))  # pyrefly: ignore[missing-attribute]
    devices.sort(key=lambda device: str(device.uniqueID()))
    cameras: list[tuple[str, bool]] = []
    for device in devices:
        name = str(device.localizedName())
        builtin = "BuiltIn" in str(device.deviceType()) or any(hint in name.lower() for hint in BUILTIN_NAME_HINTS)
        cameras.append((name, builtin))
    return cameras


def detect_camera_indices(max_index: int = 4, *, include_builtin: bool = False) -> tuple[int, ...]:
    """Probe AVFoundation indices and return the ones that deliver frames, skipping the
    Mac's built-in webcam. Pass ``include_builtin=True`` (or ``--cameras`` explicitly) to
    keep it."""
    found: list[int] = []
    for index in range(max_index + 1):
        cap = cv2.VideoCapture(index)
        ok = cap.isOpened() and cap.read()[0]
        cap.release()
        if ok:
            found.append(index)

    cameras = _cameras_in_opencv_order()
    if cameras is None or include_builtin:
        return tuple(found)
    kept: list[int] = []
    for index in found:
        name, builtin = cameras[index] if index < len(cameras) else ("?", False)
        if builtin:
            print(f"camera {index} ({name}): built-in, skipping — pass --cameras {index} to use it anyway", flush=True)
        else:
            kept.append(index)
    return tuple(kept)


class CameraStreamer:
    """Capture one camera on a daemon thread and log frames under ``camera/cam<index>``."""

    def __init__(self, index: int, *, rec: rr.RecordingStream, timeline: str = "time", jpeg_quality: int = 75) -> None:
        self.index = index
        self.rec = rec
        """Recording to log into; reassign to redirect this thread's frames (the collector swaps takes)."""
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
