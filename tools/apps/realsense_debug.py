"""Stream Intel RealSense color + depth into a Rerun viewer (standalone debug tool).

Runs in the isolated ``realsense`` pixi environment (pyrealsense2 from conda-forge,
no arms needed). The depth sensor is treated as the origin; the color camera hangs
off it via the factory extrinsics, so the 3D view fuses the depth cloud with color::

    pixi run -e realsense realsense-debug                     # spawn a viewer, Ctrl-C to stop
    pixi run -e realsense realsense-debug -- --seconds 10
    pixi run -e realsense realsense-debug -- --rr-config.save realsense.rrd

macOS 12+ denies libusb access to the camera for non-root processes ("failed to set
power state"), so on Mac use ``pixi run -e realsense realsense-debug-sudo``. To make
that passwordless, run ``pixi run -e realsense realsense-sudo-setup`` once (see
realsense_sudo_setup.py).
"""

from __future__ import annotations

import dataclasses
import time

import numpy as np
import pyrealsense2 as rs
import rerun as rr
import rerun.blueprint as rrb
import tyro

from so100_hackathon.rerun_config import LiveViewerConfig

ROOT_ENTITY = "realsense"
DEPTH_ENTITY = f"{ROOT_ENTITY}/depth/image"
RGB_ENTITY = f"{ROOT_ENTITY}/rgb/image"
RGB_CAMERA_ENTITY = f"{ROOT_ENTITY}/rgb"


@dataclasses.dataclass
class Config:
    rr_config: LiveViewerConfig = dataclasses.field(default_factory=LiveViewerConfig)
    """Rerun viewer/save/connect wiring."""

    width: int = 640
    """Stream width for both color and depth."""

    height: int = 480
    """Stream height for both color and depth."""

    fps: int = 30
    """Frames per second requested from the camera."""

    jpeg_quality: int = 90
    """JPEG quality for the logged color frames."""

    seconds: float | None = None
    """Stop after this many seconds (default: run until Ctrl-C)."""

    max_depth: float = 5.0
    """Discard depth readings beyond this many meters (0 disables the filter)."""

    image_plane_distance: float = 0.5
    """How far from the camera the pinhole frustums draw their image plane, in meters."""

    serial: str | None = None
    """Serial number of the device to open (default: first RealSense found)."""


def _require_device(serial: str | None) -> rs.device:
    devices = list(rs.context().query_devices())
    if not devices:
        raise SystemExit(
            "no RealSense device found -- check the USB connection (use the camera's own USB 3 "
            "cable, avoid hubs) and verify it shows up with an Intel vendor id in "
            "`ioreg -p IOUSB -w0`"
        )
    for device in devices:
        if serial is None or device.get_info(rs.camera_info.serial_number) == serial:
            return device
    known = ", ".join(d.get_info(rs.camera_info.serial_number) for d in devices)
    raise SystemExit(f"no RealSense with serial {serial!r} (connected: {known})")


def _blueprint() -> rrb.Blueprint:
    """3D fusion view beside the two image streams (the heuristic layout starts the
    3D eye inside the point cloud, which makes orbit/zoom feel broken)."""
    return rrb.Blueprint(
        rrb.Horizontal(
            rrb.Spatial3DView(origin=ROOT_ENTITY, name="fusion"),
            rrb.Vertical(
                rrb.Spatial2DView(origin=RGB_ENTITY, name="rgb"),
                rrb.Spatial2DView(origin=DEPTH_ENTITY, name="depth"),
            ),
            column_shares=[3, 2],
        )
    )


def _log_camera_models(rec: rr.RecordingStream, profile: rs.pipeline_profile, image_plane_distance: float) -> None:
    """Log the static scene: view coordinates, both pinholes, and the depth->color extrinsics."""
    rec.log(ROOT_ENTITY, rr.ViewCoordinates.RDF, static=True)

    depth_profile = profile.get_stream(rs.stream.depth).as_video_stream_profile()
    rgb_profile = profile.get_stream(rs.stream.color).as_video_stream_profile()

    for entity, stream in ((DEPTH_ENTITY, depth_profile), (RGB_ENTITY, rgb_profile)):
        intrinsics = stream.get_intrinsics()
        rec.log(
            entity,
            rr.Pinhole(
                resolution=[intrinsics.width, intrinsics.height],
                focal_length=[intrinsics.fx, intrinsics.fy],
                principal_point=[intrinsics.ppx, intrinsics.ppy],
                image_plane_distance=image_plane_distance,
            ),
            static=True,
        )

    # The depth sensor is the origin of the `realsense` space, so only the color
    # camera needs extrinsics.
    rgb_from_depth = depth_profile.get_extrinsics_to(rgb_profile)
    rec.log(
        RGB_CAMERA_ENTITY,
        rr.Transform3D(
            translation=rgb_from_depth.translation,
            # rs2_extrinsics stores the rotation column-major; a default (row-major)
            # reshape would silently log the transpose.
            mat3x3=np.reshape(rgb_from_depth.rotation, (3, 3), order="F"),
            relation=rr.TransformRelation.ChildFromParent,
        ),
        static=True,
    )


def main(config: Config) -> None:
    rec = config.rr_config.rec
    device = _require_device(config.serial)
    name = device.get_info(rs.camera_info.name)
    serial = device.get_info(rs.camera_info.serial_number)
    print(f"streaming from {name} (serial {serial}) at {config.width}x{config.height}@{config.fps}")

    rs_config = rs.config()
    rs_config.enable_device(serial)
    rs_config.enable_stream(rs.stream.depth, config.width, config.height, rs.format.z16, config.fps)
    rs_config.enable_stream(rs.stream.color, config.width, config.height, rs.format.rgb8, config.fps)

    pipeline = rs.pipeline()
    profile = pipeline.start(rs_config)
    # min_dist=0 keeps near-field readings; the filter's default would drop depth <0.15m.
    threshold = rs.threshold_filter(min_dist=0.0, max_dist=config.max_depth) if config.max_depth > 0 else None
    frame_count = 0
    try:
        rec.send_blueprint(_blueprint())
        _log_camera_models(rec, profile, config.image_plane_distance)
        deadline = None if config.seconds is None else time.monotonic() + config.seconds
        while deadline is None or time.monotonic() < deadline:
            frames = pipeline.wait_for_frames()
            rec.set_time("time", timestamp=time.time())

            depth_frame = frames.get_depth_frame()
            if depth_frame:
                units = depth_frame.get_units()
                if threshold is not None:
                    depth_frame = threshold.process(depth_frame)
                depth = np.asanyarray(depth_frame.get_data())
                # `meter` is depth units per meter: z16 at the default 1mm scale -> 1000.
                rec.log(DEPTH_ENTITY, rr.DepthImage(depth, meter=1.0 / units))

            color_frame = frames.get_color_frame()
            if color_frame:
                color = np.asanyarray(color_frame.get_data())
                rec.log(RGB_ENTITY, rr.Image(color).compress(jpeg_quality=config.jpeg_quality))

            frame_count += 1
            if frame_count % (config.fps * 5) == 0:
                print(f"{frame_count} frames logged")
    except KeyboardInterrupt:
        print(f"\nstopped after {frame_count} frames")
    finally:
        pipeline.stop()


if __name__ == "__main__":
    main(tyro.cli(Config))
