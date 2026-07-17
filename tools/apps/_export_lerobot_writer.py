"""LeRobot-side half of the export (see export_lerobot.py, which spawns this).

Runs inside the isolated ``export`` pixi environment -- the only place ``lerobot`` is
installed (its rerun-sdk pin conflicts with the repo's). Reads the staged episodes
(action/state .npy + camera JPEGs + manifest.json) and writes a LeRobot v3 dataset;
``--push`` uploads it to the Hugging Face Hub as a private repo.

Not meant to be run by hand.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import av  # pyrefly: ignore[missing-import] - export env only
import numpy as np

# Encoder banners/stats drown the real progress lines. Per-process state, so it must sit
# at module level for the per-camera encoder subprocesses too (they re-import this module).
# SVT-AV1's config banner bypasses libav logging and needs its own env knob; the libav
# side needs the callback pinned because encode_video_frames restores the unfiltered
# stderr callback when it returns, and codecs emit teardown stats after that restore.
os.environ.setdefault("SVT_LOG", "1")  # errors only (2 still prints per-encode preset warnings)
av.logging.set_level(av.logging.ERROR)
av.logging.restore_default_callback = lambda: None


def build(stage: Path, root: Path) -> tuple[str, Path]:
    from lerobot.datasets.lerobot_dataset import LeRobotDataset  # pyrefly: ignore[missing-import] - export env only
    from tqdm import tqdm  # pyrefly: ignore[missing-import] - export env only

    manifest = json.loads((stage / "manifest.json").read_text())
    repo_id: str = manifest["repo_id"]
    motor_names: list[str] = manifest["motor_names"]
    cameras: list[str] = manifest["cameras"]
    max_height: int = manifest.get("max_height", 0)

    def load_jpeg(path: Path) -> np.ndarray:
        import cv2

        image = cv2.imread(str(path))
        if image is None:
            raise RuntimeError(f"failed to decode staged frame {path}")
        if max_height and image.shape[0] > max_height:
            # Even dimensions: the videos encode as yuv420p, which requires them.
            width = round(image.shape[1] * max_height / image.shape[0]) // 2 * 2
            image = cv2.resize(image, (width, max_height), interpolation=cv2.INTER_AREA)
        return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)

    features: dict = {
        "action": {"dtype": "float32", "shape": (len(motor_names),), "names": motor_names},
        "observation.state": {"dtype": "float32", "shape": (len(motor_names),), "names": motor_names},
    }
    for camera in cameras:
        first = next((stage / manifest["episodes"][0]["dir"] / camera).glob("*.jpg"))
        height, width, channels = load_jpeg(first).shape
        features[f"observation.images.{camera}"] = {
            "dtype": "video",
            "shape": (height, width, channels),
            "names": ["height", "width", "channels"],
        }

    dataset = LeRobotDataset.create(
        repo_id=repo_id,
        fps=manifest["fps"],
        features=features,
        root=root / repo_id,
        robot_type="so100_follower",
        use_videos=True,
    )

    episodes = manifest["episodes"]
    for number, episode in enumerate(episodes, start=1):
        episode_dir = stage / episode["dir"]
        action = np.load(episode_dir / "action.npy")
        state = np.load(episode_dir / "state.npy")
        label = f"[{number}/{len(episodes)}] {episode['name']}"
        for index in tqdm(range(len(action)), desc=f"{label}: buffering frames", unit="frame", leave=False):
            frame = {
                "action": action[index],
                "observation.state": state[index],
                "task": episode["task"],
            }
            for camera in cameras:
                frame[f"observation.images.{camera}"] = load_jpeg(episode_dir / camera / f"{index:06d}.jpg")
            dataset.add_frame(frame)
        print(f"{label}: encoding {len(cameras)} camera video(s)...", flush=True)
        start = time.perf_counter()
        dataset.save_episode()
        print(f"{label}: wrote {len(action)} frames (encoded in {time.perf_counter() - start:.0f}s)", flush=True)

    print("finalizing dataset...", flush=True)
    dataset.finalize()
    return repo_id, root / repo_id


def push(repo_id: str, root: Path) -> None:
    from lerobot.datasets.lerobot_dataset import LeRobotDataset  # pyrefly: ignore[missing-import] - export env only

    dataset = LeRobotDataset(repo_id, root=root / repo_id)
    print(f"pushing to https://huggingface.co/datasets/{repo_id} (private) ...")
    dataset.push_to_hub(private=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--stage", type=Path, default=None)
    parser.add_argument("--repo-id", default=None)
    parser.add_argument("--push", action="store_true")
    args = parser.parse_args()

    if args.stage is not None:
        repo_id, _ = build(args.stage, args.root)
    else:
        repo_id = args.repo_id
        if repo_id is None:
            raise SystemExit("--repo-id is required without --stage")
    if args.push:
        push(repo_id, args.root)


if __name__ == "__main__":
    main()
