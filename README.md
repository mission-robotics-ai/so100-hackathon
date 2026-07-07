# so100-hackathon

Teach an SO-100 arm a task, end to end: teleoperated data collection into
[Rerun](https://rerun.io) recordings, curation via a local catalog, export to LeRobot v3
for training, and replay back on the arm. Ported from the internal Rust setup in
[rerun-io/portugal](https://github.com/rerun-io/portugal); the SO-ARM100 URDF in
`data/so100/` comes from rerun's `examples/python/animated_urdf`.

**Start with the course site** — it walks through everything below step by step, with an
embedded live viewer and a recording UI, starting from cloning this repo:

> **https://so100-hackathon.vercel.app** *(not deployed yet — serve it locally with
> `pixi run learn` → http://localhost:3000)*

Prefer the terminal? This README is the terse, CLI-only mirror of the same six steps —
pick either path, they produce the same datasets.

## Welcome

Everything runs through [Pixi](https://pixi.sh) — install it, then the repo:

```bash
curl -fsSL https://pixi.sh/install.sh | sh
pixi install
```

Plug in the arm(s) — they show up as `/dev/cu.usbmodem<USB_ID>`.

## Set up: test and calibrate your robot

```bash
pixi run log-so100                        # smoke test: viewer + telemetry + cameras + animated URDF
pixi run calibrate-so100 leader           # move the LEADER arm; follow the prompts
pixi run calibrate-so100 follower         # same, for the follower
pixi run teleop-so100                     # follower mirrors the leader (torque ON, Ctrl-C releases)
```

Calibration is two steps per arm: hold the **middle pose** (match the gray target URDF,
Enter), then **sweep every joint** through its range (Enter). It writes
`calibrations/<USB_ID>.json` — including which arm is the leader — so later runs need no
flags. Teleop clamps follower goals to the swept range, glides to the leader's pose on
start instead of jumping, and always releases torque on exit.

Logged per arm: `<arm>/position` (calibrated degrees; gripper 0-100%), raw servo
telemetry (`position_raw`, `speed`, `load`, `current`, `voltage`, `temperature`), the
animated URDF, `camera/cam<N>` JPEG frames — plus `<follower>/goal` (the commanded pose,
i.e. the *action*) during teleop.

## Collect: record episodes to a dataset

Start the long-lived local data server once and leave it running (through breaks, closed
browser tabs, new datasets):

```bash
pixi run so100-server     # gRPC proxy :9876 + catalog :51234 + control API :8000
```

On startup it re-registers every `recordings/<dataset>/<episode>.rrd` found on disk, so
restarting it loses nothing. It does **not** hold the serial ports — arms attach on
demand, so calibration/teleop work while it runs.

Record an episode from the CLI (`tools/apps/record_episode.py` — it opens the arms
itself, so the server must not be holding them):

```bash
pixi run record-episode -- --dataset my_task --task "Pick up the ball" --tag "Good episode"
```

Teleop runs while it records; Enter stops (or `--seconds N`). The episode name defaults
to `episode_<N>`, auto-incremented. The take is written to
`recordings/<dataset>/<episode>.rrd` with the name, task, and tag stamped on as
recording properties, then registered to the catalog (or, if the server is down, picked
up by its next startup scan).

Alternatively drive the server's control API — this is exactly what the course site's
Collect page does:

```bash
curl -X POST localhost:8000/arms/connect
curl -X POST localhost:8000/live/pause          # pause the live stream (and /live/resume: same stream continues)
curl -X POST localhost:8000/start -d '{"dataset":"my_task","episode":"episode_1","task":"Pick up the ball"}'
curl -X POST localhost:8000/stop  -d '{"tag":"Good episode"}'
curl -X POST localhost:8000/episode/update -d '{"task":"Pick up the ball","tag":"Bad episode"}'  # fix the last episode's metadata
curl -X POST localhost:8000/arms/disconnect     # frees the serial ports again
```

## Refine: enrich, query, curate

```bash
pixi run query-dataset                                      # list datasets in the catalog
pixi run query-dataset -- --dataset my_task                 # per-episode table: task, tag, duration, size
pixi run query-dataset -- --dataset my_task --tag "Good episode"
pixi run query-dataset -- --dataset my_task --episode episode_1 --entity follower/position
```

The metadata stamped at record time comes back as `property:...` columns on the
catalog's segment table — that's what the tag filter runs on (DataFusion), and the
entity query returns a pandas DataFrame.

## Train: prepare for training

Export to LeRobot v3 (only `"Good episode"` takes by default; `--tag ""` for all):

```bash
pixi run export-lerobot -- --dataset my_task --repo-id <hf-user>/my_task           # -> datasets/<hf-user>/my_task
pixi run -e export hf auth login                                                   # once
pixi run export-lerobot -- --dataset my_task --repo-id <hf-user>/my_task --push    # + upload (private repo)
```

The first run solves the isolated `export` environment — LeRobot's rerun-sdk pin
conflicts with the repo's, so `tools/apps/export_lerobot.py` stages episodes from the
catalog and hands off to `_export_lerobot_writer.py` inside that env. Then train with
LeRobot as usual (`lerobot-train --policy.type=act --dataset.repo_id=<hf-user>/my_task ...`).

## Deploy: close the loop

Replay a recorded episode's action trajectory on the follower (leader not needed; make
sure the server isn't holding the arms):

```bash
pixi run replay-episode -- --dataset my_task --episode episode_1 --speed 0.5
```

It ramps gently to the starting pose, plays the trajectory, streams the replayed joints
to the live proxy, and releases torque when done. Keep a hand near the arm on the first
run. From here, a trained policy is the same loop with actions computed live — see the
course's Deploy page.

## Development

```bash
pixi run -e dev py-fmt        # autofix lints + format (ruff) — the only task that edits files
pixi run -e dev py-fmt-check  # check formatting + lints (ruff) — CI runs this
pixi run -e dev py-lint       # typecheck (pyrefly) — CI runs this
pixi run -e dev py-deadcode   # find dead code (vulture)
```

Package layout: Tyro configs + `main()` live in `src/so100_hackathon/apis/`,
`tools/apps/*.py` are thin shims, and beartype instruments the package when
`PIXI_DEV_MODE=1` (dev env).
