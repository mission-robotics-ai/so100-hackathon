# so100-hackathon

Teach an SO-100 arm a task, end to end: teleoperated data collection into
[Rerun](https://rerun.io) recordings, curation via a local catalog, export to LeRobot v3
for training, and replay back on the arm.


## Welcome

Everything runs through [Pixi](https://pixi.sh) — install it, then clone the repo and
install its environment:

```bash
curl -fsSL https://pixi.sh/install.sh | sh
git clone https://github.com/rerun-io/so100-hackathon.git
cd so100-hackathon
pixi install
```

Plug in the arm(s) — they show up as `/dev/cu.usbmodem<USB_ID>`.

## START HERE

```bash 
pixi run learn → http://localhost:3000
```

## CLI Set up: test and calibrate your robot

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

## Train: prepare for training

Export to LeRobot v3 (only `"Good episode"` takes by default; `--tag ""` for all):

```bash
pixi run export-lerobot -- --dataset my_task --repo-id <hf-user>/my_task           # -> datasets/<hf-user>/my_task
pixi run -e export hf auth login                                                   # once
pixi run export-lerobot -- --dataset my_task --repo-id <hf-user>/my_task --push    # + upload (private repo)
```

The first run solves the isolated `export` environment — LeRobot's rerun-sdk pin
conflicts with the repo's, so `tools/apps/export_lerobot.py` stages episodes from the
catalog and hands off to `_export_lerobot_writer.py` inside that env. Camera streams
export as `observation.images.top` / `.side` in cam-index order (`--camera-names` to
override). From here, fine-tuning runs on NewTheory's infrastructure via their `newt`
CLI — see [how fine-tuning works](https://newtheory-docs.vercel.app/docs/nt-0/how-finetune-works).

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

Running a trained policy is NewTheory's side of the loop: once your `newt finetune` run
is live, the `newt` SDK drives the follower directly (`Robot(model="<your-tag>")`) —
see [how fine-tuning works](https://newtheory-docs.vercel.app/docs/nt-0/how-finetune-works).
An arm calibrated here is already set up for it (the dual-written calibration above);
just make sure nothing else is holding the follower's serial port.

## Development

```bash
pixi run -e dev lint
pixi run -e dev typecheck
pixi run -e dev deadcode
```

Package layout follows the examples-monorepo conventions: Tyro configs + `main()` live in
`src/so100_hackathon/apis/`, `tools/apps/*.py` are thin shims, beartype instruments the
package when `PIXI_DEV_MODE=1` (dev env).
