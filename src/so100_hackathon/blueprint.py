"""Rerun blueprint: 3D arms + camera feeds on top; below, one position tab per arm
(the training-relevant series) plus a diagnostics grid with the servo health metrics."""

from __future__ import annotations

import rerun.blueprint as rrb

# entity subpath -> plot title, for the diagnostics tab. Everything here is logged but
# irrelevant to lerobot training (which only consumes positions + images) — it's rig health.
DIAGNOSTICS: dict[str, str] = {
    "current": "current (mA)",
    "temperature": "temperature (C)",
    "voltage": "voltage (V)",
    "load": "load (%)",
    "speed": "speed (ticks/s)",
}


def create_blueprint(
    arm_names: list[str],
    *,
    leader_name: str | None = None,
    camera_paths: list[str] | None = None,
    visual_paths: list[str] | None = None,
    show_urdf: bool = False,
    window_seconds: float = 10.0,
) -> rrb.Blueprint:
    """Sliding-window layout for realtime viewing.

    Tabs-only when ``show_urdf`` is off and there are no cameras; otherwise the 3D arms
    and camera feeds sit side by side above the tabs.
    """
    time_ranges = rrb.VisibleTimeRange(
        "time",
        start=rrb.TimeRangeBoundary.cursor_relative(seconds=-window_seconds),
        end=rrb.TimeRangeBoundary.cursor_relative(),
    )

    def label(name: str) -> str:
        if leader_name is None or name in ("leader", "follower"):
            return name  # arms named by role need no suffix
        return f"{name} (leader)" if name == leader_name else f"{name} (follower)"

    # <name>/goal only exists in teleop on the follower (the commanded pose derived from
    # the leader) — it is lerobot's "action", while the follower's measured position is
    # "observation.state". Overlaying them makes tracking lag/error directly visible.
    position_tabs = [
        rrb.TimeSeriesView(
            name=f"{label(name)} position",
            origin=name,
            contents=["+ $origin/position", "+ $origin/goal"],
            time_ranges=time_ranges,
        )
        for name in arm_names
    ]
    # One row of health plots per arm; none of this feeds training, hence its own tab.
    diagnostics = rrb.Grid(
        *[
            rrb.TimeSeriesView(name=f"{label(name)} {title}", origin=f"{name}/{subpath}", time_ranges=time_ranges)
            for name in arm_names
            for subpath, title in DIAGNOSTICS.items()
        ],
        grid_columns=len(DIAGNOSTICS),
        name="diagnostics",
    )
    # Open on the follower: its position (observation.state) + goal (action) are what training consumes.
    active = next((i for i, name in enumerate(arm_names) if name != leader_name), 0)
    tabs = rrb.Tabs(*position_tabs, diagnostics, active_tab=active)

    arms_3d = (
        rrb.Spatial3DView(
            name="arms",
            origin="/",
            # Include ONLY the URDF visual meshes: no cameras, collision meshes, or
            # transform/scalar entities cluttering the view's entity tree. Ancestor
            # transforms still apply — contents filters visibility, not the hierarchy.
            contents=[f"+ /{path}/**" for path in visual_paths or []],
        )
        if show_urdf
        else None
    )
    camera_views = [rrb.Spatial2DView(name=path.rsplit("/", 1)[-1], origin=path) for path in camera_paths or []]

    # Cameras stack vertically so the 3D view keeps most of the width (3:2 split).
    cameras = rrb.Vertical(*camera_views) if len(camera_views) > 1 else (camera_views[0] if camera_views else None)
    panes = [pane for pane in (arms_3d, cameras) if pane is not None]
    if not panes:
        return rrb.Blueprint(tabs, collapse_panels=True)
    spatial = rrb.Horizontal(*panes, column_shares=[3, 2]) if len(panes) == 2 else panes[0]
    return rrb.Blueprint(rrb.Vertical(spatial, tabs, row_shares=[2, 1]), collapse_panels=True)
