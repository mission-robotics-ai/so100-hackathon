"""Animated SO-100 URDF, driven by calibrated joint values.

Follows rerun's ``examples/python/animated_urdf`` dual-arm pattern: each arm
loads the same URDF with its own entity/frame prefix, then re-logs joint
transforms every frame. The URDF (from the rerun repo, TheRobotStudio
SO-ARM100 model) names its revolute joints "1".."6" — exactly the bus motor
ids.

Joint values come from ``MotorCalibration.calibrated_from_raw``:

- DEGREE joints: degrees relative to the calibration reference pose, which is
  each joint's mid-limits URDF angle (the pose captured by ``calibrate-so100``).
  Uncalibrated arms fall back to raw-centered degrees with assumed sign.
- LINEAR joints (gripper): 0..100% mapped across the URDF jaw limits.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import rerun as rr

from so100_hackathon.calibration import CalibMode, MotorCalibration

FOLLOWER_URDF_PATH = Path(__file__).parents[2] / "data" / "so100" / "so100.urdf"
LEADER_URDF_PATH = Path(__file__).parents[2] / "data" / "so101_leader" / "so101_leader.urdf"

# Both real arms are black plastic; keep enough albedo that shading still shows shape.
MATTE_BLACK = (0.16, 0.16, 0.17)

# Both models are z-up, but they face different ways and their shoulder-pan axes are
# offset from the model origin (values from FK over the URDFs). These corrections make
# every arm face +x with its pan axis through the anchor point: an arm "position" is
# where its pan axis meets the ground, and side-by-side arms are spaced along y.
# (roll/pitch/yaw in degrees applied as Rz@Ry@Rx, then translation added to the arm position)
MODEL_CORRECTIONS: dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]] = {
    FOLLOWER_URDF_PATH.name: ((0.0, 0.0, 90.0), (-0.0452, 0.0, 0.0)),
    LEADER_URDF_PATH.name: ((0.0, 0.0, 0.0), (0.1242, 0.1681, -0.0301)),
}

# Per-joint display sign for calibrated DEGREE values. The two models define opposite
# positive directions for shoulder_pan (leader pan axis -z in world, follower +z; the other
# joints agree — FK over both URDFs), while calibrated values are physically consistent
# across arms (raw+ pans both real arms the same way; teleop passthrough relies on that).
# The leader model matches the real arms' convention, so the follower model flips pan.
JOINT_DISPLAY_SIGNS: dict[str, tuple[float, float, float, float, float, float]] = {
    FOLLOWER_URDF_PATH.name: (-1.0, 1.0, 1.0, 1.0, 1.0, 1.0),
    LEADER_URDF_PATH.name: (1.0, 1.0, 1.0, 1.0, 1.0, 1.0),
}


def _rpy_matrix(roll_deg: float, pitch_deg: float, yaw_deg: float) -> list[list[float]]:
    roll, pitch, yaw = (math.radians(v) for v in (roll_deg, pitch_deg, yaw_deg))
    cr, sr, cp, sp, cy, sy = math.cos(roll), math.sin(roll), math.cos(pitch), math.sin(pitch), math.cos(yaw), math.sin(yaw)
    return [
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ]


@dataclass
class UrdfArm:
    name: str
    joints: list[rr.urdf.UrdfJoint]
    calib_modes: list[CalibMode]
    center_angles_rad: list[float]
    """URDF joint angle at calibrated 0 deg (the calibration reference pose)."""
    joint_signs: tuple[float, float, float, float, float, float]
    """Display sign per joint (see JOINT_DISPLAY_SIGNS)."""
    collision_geometries_path: str
    """Entity path of this model's collision meshes (for hiding in blueprints)."""
    tree: rr.urdf.UrdfTree
    """Parsed URDF, retained so the static geometry can be (re-)logged per recording."""
    urdf_path: Path
    color: tuple[float, float, float] | None
    translation: tuple[float, float, float]

    @classmethod
    def create(
        cls,
        name: str,
        calibration: list[MotorCalibration],
        *,
        urdf_path: Path = FOLLOWER_URDF_PATH,
        translation: tuple[float, float, float] = (0.0, 0.0, 0.0),
        center_angles_deg: tuple[float, ...] | None = None,
        color: tuple[float, float, float] | None = None,
    ) -> UrdfArm:
        tree = rr.urdf.UrdfTree.from_file_path(urdf_path, entity_path_prefix=name, frame_prefix=f"{name}/")
        joints = sorted((j for j in tree.joints() if j.joint_type == "revolute"), key=lambda j: int(j.name))
        if center_angles_deg is not None:
            center_angles_rad = [math.radians(deg) for deg in center_angles_deg]
        else:
            center_angles_rad = [(j.limit_lower + j.limit_upper) / 2.0 for j in joints]
        arm = cls(
            name=name,
            joints=joints,
            calib_modes=[calib.calib_mode for calib in calibration],
            center_angles_rad=center_angles_rad,
            joint_signs=JOINT_DISPLAY_SIGNS[urdf_path.name],
            collision_geometries_path=f"{name}/{tree.name}/collision_geometries",
            tree=tree,
            urdf_path=urdf_path,
            color=color,
            translation=translation,
        )
        arm.log_static()
        return arm

    def log_static(self) -> None:
        """(Re-)log the URDF's static geometry into the current recording.

        The meshes are static data keyed by recording id, so this must run once per
        recording (e.g. the dataset collector re-logs it for every take)."""
        self.tree.log_urdf_to_recording()
        if self.color is not None:
            # Tint every visual mesh, overriding the URDF's material colors.
            links = [self.tree.root_link()] + [self.tree.get_joint_child(joint) for joint in self.tree.joints()]
            for link in links:
                for visual_path in self.tree.get_visual_geometry_paths(link):
                    rr.log(visual_path, rr.Asset3D.from_fields(albedo_factor=[*self.color, 1.0]), static=True)
        # The URDF's frames form an island: anchor its root frame to the entity tree
        # (parent_frame defaults to the implicit root frame here), or nothing renders.
        root_frame = f"{self.name}/{self.tree.root_link().name}"
        rpy, offset = MODEL_CORRECTIONS[self.urdf_path.name]
        rr.log(
            self.name,
            rr.Transform3D(
                translation=tuple(t + o for t, o in zip(self.translation, offset, strict=True)),
                mat3x3=_rpy_matrix(*rpy),
                child_frame=root_frame,
            ),
            static=True,
        )

    def joint_angle_rad(self, joint_index: int, calibrated: float) -> float:
        """Calibrated value (deg, or % for LINEAR) -> clamped URDF joint angle."""
        joint = self.joints[joint_index]
        if self.calib_modes[joint_index] == "LINEAR":
            angle = joint.limit_lower + calibrated / 100.0 * (joint.limit_upper - joint.limit_lower)
        else:
            angle = self.center_angles_rad[joint_index] + self.joint_signs[joint_index] * math.radians(calibrated)
        # Clamp ourselves: compute_transform(clamp=True) warns on every out-of-limit
        # angle, which floods stdout on uncalibrated arms.
        return min(max(angle, joint.limit_lower), joint.limit_upper)

    def log_joints(self, calibrated_values: list[float]) -> None:
        for joint in self.joints:
            joint_index = int(joint.name) - 1
            angle = self.joint_angle_rad(joint_index, calibrated_values[joint_index])
            rr.log(f"{self.name}/joint_transforms", joint.compute_transform(angle))

    def log_pose(self, angles_rad: list[float]) -> None:
        """Log explicit joint angles (rad), e.g. a calibration target pose."""
        for joint, angle in zip(self.joints, angles_rad, strict=True):
            rr.log(f"{self.name}/joint_transforms", joint.compute_transform(angle, clamp=True))
