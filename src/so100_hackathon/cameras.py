"""Webcam capture threads that stream JPEG-compressed frames into Rerun.

Python equivalent of rerun-io/portugal ``src/camera.rs`` (one thread per
camera, wall-clock timestamps), using OpenCV instead of nokhwa.
"""

from __future__ import annotations

import threading
import time

import cv2
import rerun as rr


def detect_camera_indices(max_index: int = 4) -> tuple[int, ...]:
    """Probe AVFoundation indices and return the ones that deliver frames."""
    found: list[int] = []
    for index in range(max_index + 1):
        cap = cv2.VideoCapture(index)
        ok = cap.isOpened() and cap.read()[0]
        cap.release()
        if ok:
            found.append(index)
    return tuple(found)


class CameraStreamer:
    """Capture one camera on a daemon thread and log frames under ``camera/cam<index>``."""

    def __init__(self, index: int, *, timeline: str = "time", jpeg_quality: int = 75) -> None:
        self.index = index
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
                # set_time is thread-local, so this timeline value only affects this thread's logs.
                rr.set_time(self.timeline, timestamp=time.time())
                frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
                rr.log(self.entity_path, rr.Image(frame_rgb).compress(jpeg_quality=self.jpeg_quality))
        except Exception as error:  # a crashed feed must be visible, not a silent thread death
            print(f"camera {self.index}: streaming stopped: {error}", flush=True)
        finally:
            cap.release()
