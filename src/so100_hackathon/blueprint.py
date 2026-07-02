"""Rerun blueprint: 3D arms + camera feeds on the left, per-arm telemetry columns on the right."""

from __future__ import annotations

import rerun.blueprint as rrb


def _telemetry_column(name: str, time_ranges: rrb.VisibleTimeRange) -> rrb.Vertical:
    return rrb.Vertical(
        rrb.TimeSeriesView(name=f"{name} position", origin=f"{name}/position", time_ranges=time_ranges),
        rrb.Horizontal(
            rrb.TimeSeriesView(name=f"{name} current (mA)", origin=f"{name}/current", time_ranges=time_ranges),
            rrb.TimeSeriesView(name=f"{name} temperature (C)", origin=f"{name}/temperature", time_ranges=time_ranges),
        ),
        row_shares=[2, 1],
    )


def create_blueprint(
    arm_names: list[str],
    *,
    camera_paths: list[str] | None = None,
    collision_paths: list[str] | None = None,
    show_urdf: bool = False,
    window_seconds: float = 10.0,
) -> rrb.Blueprint:
    """Sliding-window layout for realtime viewing.

    Telemetry-only when ``show_urdf`` is off and there are no cameras; otherwise a
    spatial column (3D arms over camera feeds) next to the telemetry columns.
    """
    time_ranges = rrb.VisibleTimeRange(
        "time",
        start=rrb.TimeRangeBoundary.cursor_relative(seconds=-window_seconds),
        end=rrb.TimeRangeBoundary.cursor_relative(),
    )
    telemetry = rrb.Horizontal(*[_telemetry_column(name, time_ranges) for name in arm_names])

    spatial_views: list[rrb.Spatial3DView | rrb.Horizontal] = []
    if show_urdf:
        spatial_views.append(
            rrb.Spatial3DView(
                name="arms",
                origin="/",
                # Camera images have no pinhole, so they don't belong in the 3D view.
                contents=["+ $origin/**", "- /camera/**"],
                # Scalar entities are skipped by the 3D view; just hide the URDF collision meshes.
                overrides={path: rrb.EntityBehavior(visible=False) for path in (collision_paths or [])},
            )
        )
    if camera_paths:
        spatial_views.append(rrb.Horizontal(*[rrb.Spatial2DView(name=path.rsplit("/", 1)[-1], origin=path) for path in camera_paths]))

    if not spatial_views:
        return rrb.Blueprint(telemetry, collapse_panels=True)
    spatial = rrb.Vertical(*spatial_views, row_shares=[2, 1]) if len(spatial_views) == 2 else spatial_views[0]
    return rrb.Blueprint(rrb.Horizontal(spatial, telemetry, column_shares=[3, 2]), collapse_panels=True)
