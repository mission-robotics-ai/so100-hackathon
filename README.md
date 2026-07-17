# so100-hackathon

Teach an SO-100 arm a task, end to end: teleoperated data collection into
[Rerun](https://rerun.io) recordings, curation via a local catalog, export to LeRobot v3
for training, and replay back on the arm.


## Welcome

Everything runs through [Pixi](https://pixi.sh), a fast package manager that handles
both conda and PyPI dependencies (using [uv](https://docs.astral.sh/uv/) under the hood
for the latter) from a single lockfile — no separate conda/pip/venv setup, and every
command below is a `pixi run` task. Docs: <https://pixi.prefix.dev/latest/>. Install it,
then clone the repo and install its environment:

```bash
curl -fsSL https://pixi.sh/install.sh | sh
git clone https://github.com/mission-robotics-ai/so100-hackathon.git
cd so100-hackathon
pixi install
```

SDK fixes ship through the weekend — pull the latest with `git pull && pixi update newt`.

Plug in the arm(s) — they show up as `/dev/cu.usbmodem<USB_ID>`.

## START HERE

The guided course site is **the canonical path** — it walks you through the whole
loop (set up, collect, refine, prepare, deploy) from the browser:

```bash
pixi run learn            # then open http://localhost:3000
```

Follow it end to end; you should not need anything else. Everything below is the
compressed, CLI-only version of exactly those five course steps — a reference for
debugging or terminal-only use, not a second path to choose.

## Set up: test and calibrate your robot

Start the long-lived local data server first and leave it running all day (through
breaks, closed browser tabs, new datasets):

```bash
pixi run so100-server     # gRPC proxy :9876 + catalog :51234 + control API :8000
```

It does **not** hold the serial ports — arms attach on demand, so every tool below
works while it runs. Then, in order:

```bash
pixi run check-cameras                    # cameras only: probe every layer, stream what works
pixi run log-so100                        # smoke test: viewer + telemetry + cameras + animated URDF
pixi run calibrate-so100 leader           # move the LEADER arm; follow the prompts
pixi run calibrate-so100 follower         # same, for the follower
pixi run teleop-so100                     # follower mirrors the leader (torque ON, Ctrl-C releases)
```

If cameras are missing from the viewer, `check-cameras` names the failing layer and its
fix: plugged in but invisible to every app (macOS camera stack wedged — reboot), not on
USB at all (check the cable), permission denied (System Settings > Privacy & Security >
Camera), or only skipped cameras found (the built-in webcam and iPhones are never
auto-selected — plug in a recording webcam, or pass `--cameras <N>` to log-so100).

Calibration is two steps per arm: hold the **middle pose** (match the gray target URDF,
Enter), then **sweep every joint** through its range (Enter). It writes
`calibrations/<USB_ID>.json` — including which arm is the leader — so later runs need no
flags. Teleop clamps follower goals to the swept range, glides to the leader's pose on
start instead of jumping, and always releases torque on exit.

The same calibration is **dual-written in LeRobot's format** into the HF cache
(`~/.cache/huggingface/lerobot/calibration/robots/so101_follower/<USB_ID>.json`), so
LeRobot-ecosystem tools — including the `newt-starter-so101` deployment client — drive
the arm with exactly the calibration your datasets were recorded with (launch them with
`--robot.id=<USB_ID>`; no second `lerobot-calibrate` sweep). This matters because
calibration defines what "42°" means physically: if train-time and inference-time
calibrations differ, a trained policy commands the wrong poses. For arms calibrated
before dual-write existed, `pixi run export-calibration -- follower` emits the file
(arm plugged in — the homing offsets are read back from the servos' EEPROM).

Logged per arm: `<arm>/position` (calibrated degrees; gripper 0-100%), raw servo
telemetry (`position_raw`, `speed`, `load`, `current`, `voltage`, `temperature`), the
animated URDF, `camera/cam<N>` JPEG frames — plus `<follower>/goal` (the commanded pose,
i.e. the *action*) during teleop.

## Collect: record episodes to a dataset

The server from Set up stays running through all of this. On startup it re-registers
every `recordings/<dataset>/<episode>.rrd` found on disk, so restarting it loses
nothing.

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
curl -X POST localhost:8000/start -d '{"dataset":"my_task","episode":"episode_01","task":"Pick up the ball"}'
curl -X POST localhost:8000/stop  -d '{"tag":"Good episode"}'
curl -X POST localhost:8000/episode/update -d '{"task":"Pick up the ball","tag":"Bad episode"}'  # fix the last episode's metadata
curl -X POST localhost:8000/arms/disconnect     # frees the serial ports again
```

## Refine: enrich, query, curate

```bash
pixi run query-dataset                                      # list datasets in the catalog
pixi run query-dataset -- --dataset my_task                 # per-episode table: task, tag, duration, size
pixi run query-dataset -- --dataset my_task --tag "Good episode"
pixi run query-dataset -- --dataset my_task --episode episode_01 --entity follower/position
```

The metadata stamped at record time comes back as `property:...` columns on the
catalog's segment table — that's what the tag filter runs on (DataFusion), and the
entity query returns a pandas DataFrame.

## Prepare: export for training

Fine-tuning runs on New Theory's GPUs. One command does the whole handoff — it exports
the dataset to LeRobot v3 under `datasets/local/<task>/`, then launches `newt finetune`
on that folder (`--dry-run` to stop after the export):

```bash
pixi run finetune -- --dataset my_task
```

It trains on every usable episode regardless of tag (`--tag "Good episode"` to apply
your curation); episodes missing a camera or motion stream are reported and skipped.
See the [hackathon fine-tune guide](https://missionrobotics.ai/hackers/fine-tune).

Prefer Hugging Face / the broader LeRobot ecosystem? The manual export writes the same
LeRobot v3 dataset itself (only `"Good episode"` takes by default; `--tag ""` for all)
and can push it to the Hub:

```bash
pixi run export-lerobot -- --dataset my_task --repo-id <team>/my_task           # -> datasets/<team>/my_task
pixi run -e export hf auth login                                                # once
pixi run export-lerobot -- --dataset my_task --repo-id <team>/my_task --push    # + upload (private repo)
```

The first export solves the isolated `export` environment — LeRobot's rerun-sdk pin
conflicts with the repo's, so `tools/apps/export_lerobot.py` stages episodes from the
catalog and hands off to `_export_lerobot_writer.py` inside that env. Camera streams
export as `observation.images.top` / `.side` in cam-index order (`--camera-names` to
override).

Joint units are converted on export: recordings are in calibrated degrees, but the
dataset is written in **LeRobot's normalized wire units** (arm joints [-100, 100] over
each joint's calibrated range, gripper [0, 100]) using the follower's calibration —
that's the convention of the pooled SO-100/101 community data, the base checkpoint, and
any deploy client built on lerobot's `SO101Follower` (like the New Theory starter). Skip
the conversion with `--units degrees` if your training stack expects degrees; mixing the
two silently produces a model that commands wrong poses.

## Deploy: close the loop

Replay a recorded episode's action trajectory on the follower (leader not needed; make
sure the server isn't holding the arms):

```bash
pixi run replay-episode -- --dataset my_task --episode episode_01 --speed 0.5
```

It ramps gently to the starting pose, plays the trajectory, streams the replayed joints
to the live proxy, and releases torque when done. Keep a hand near the arm on the first
run.

Running a trained policy is NewTheory's side of the loop: once your `pixi run finetune`
run is live, the `newt` SDK drives the follower directly (`Robot(model="<your-tag>")`) —
see the [call-your-model guide](https://missionrobotics.ai/hackers/call-your-model).
An arm calibrated here is already set up for it (the dual-written calibration above);
just make sure nothing else is holding the follower's serial port.

## Agent skills

[Rerun's agent skills](https://github.com/rerun-io/rerun/tree/main/skills) are checked
in under `.agents/skills/` and recorded in `skills-lock.json`, with symlinks in
`.claude/skills/` — so Claude Code, Codex, and other agents pick them up automatically
when working in this repo.

To refresh the checked-in snapshot to the newest maintained versions, run:

```bash
npx skills update --project --yes
```

## Development

```bash
pixi run -e dev lint
pixi run -e dev typecheck
pixi run -e dev deadcode
```

Package layout follows the examples-monorepo conventions: Tyro configs + `main()` live in
`src/so100_hackathon/apis/`, `tools/apps/*.py` are thin shims, beartype instruments the
package when `PIXI_DEV_MODE=1` (dev env).
