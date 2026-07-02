# so100-hackathon

Realtime SO-100 arm logging into [Rerun](https://rerun.io): joint telemetry, webcam streams,
and a live-animated URDF per arm. Python port of the internal Rust setup in
[rerun-io/portugal](https://github.com/rerun-io/portugal), using the modern Rerun SDK
(`rr.urdf.UrdfTree`, coordinate frames, blueprints).

<https://github.com/rerun-io/rerun> `examples/python/animated_urdf` is the reference for the
URDF animation; the SO-ARM100 model in `data/so100/` comes from that example's data.

## Setup

```bash
pixi install
```

Plug in the arm(s) — they show up as `/dev/cu.usbmodem<USB_ID>`.

## Stream

```bash
pixi run log-so100                        # spawn a viewer, stream everything live
pixi run log-so100 --rr-config.connect    # stream to an already-open viewer
pixi run log-so100 --rr-config.save out.rrd   # ALSO record to a .rrd while viewing
```

What gets logged, per arm (named by USB id unless you pass `--names leader follower`):

| entity | content |
| --- | --- |
| `<arm>/position` | calibrated degrees (gripper: 0-100%), one series per joint |
| `<arm>/position_raw`, `speed`, `load`, `current`, `voltage`, `temperature` | raw servo telemetry |
| `<arm>/so_arm100/...` | URDF meshes, animated from live joints |
| `camera/cam<N>` | JPEG-compressed webcam frames (threads, wall-clock timestamps) |

Useful flags: `--fps 60`, `--cameras 1 2` (pick specific cameras; `--cameras` alone disables),
`--no-urdf`, `--seconds 10`, `--rr-config.headless` (required in shells without a display,
otherwise logging wedges when the viewer can't spawn).

## Calibrate

Uncalibrated arms plot raw-centered degrees and animate the URDF with guessed offsets.
Calibration follows the standard lerobot procedure (`lerobot-calibrate`), one arm per run
in either order — each arm's calibration is independent, keyed by its USB id:

```bash
pixi run calibrate-so100 leader --rr-config.connect     # then wiggle the LEADER arm
pixi run calibrate-so100 follower --rr-config.connect   # then wiggle the follower arm
```

The `leader`/`follower` argument is required — which arm you're calibrating is always
explicit, and it's stamped into the calibration file so `log-so100` never guesses.

With several arms plugged in, the tool asks you to **wiggle a joint on the arm you want**
and selects its port automatically (no need to know which `/dev/cu.usbmodem*` is which;
`--port` still works as an override).

The viewer shows two URDF arms: **gray = target pose**, the other (black leader / white
follower) = live view of your arm. Torque is off, so you can move the arm freely by hand.

1. **Middle pose** — move the arm to the middle of its range of motion (match the gray
   target), hold it still, press Enter. This pose defines 0° for every joint. Like
   lerobot's half-turn homing, the offset is **written to each servo's EEPROM** so the
   middle reads ~2047 ticks — the 0/4095 tick wrap ends up half a turn away and can never
   be crossed during normal motion (an arm whose joints happen to sit near the wrap
   otherwise gets ±360° jumps).
2. **Range sweep** — move every joint except `wrist_roll` (full-turn joint, auto 0-4095)
   through its full range of motion, including fully closing/opening the gripper (leader:
   squeeze/release the trigger). A live min/pos/max table shows progress. Press Enter when
   done. The swept range is also written to the servos' position-limit registers.

Joint directions aren't calibrated per arm — like lerobot, they follow the standard
assembly convention (flip an entry in `DRIVE_SIGNS` in `apis/calibrate.py` if a
non-standard build mirrors a joint).

Writes `calibrations/<USB_ID>.json` (portugal / lerobot-v0 format, plus recorded
`range_min`/`range_max` and `kind`), which `log-so100` picks up automatically on the next
run — including which arm is the leader, so the black leader model and white follower model
appear without any flags, leader placed leftmost. If left/right looks mirrored from your
vantage point, pass `--arm-spacing -0.4`. Verify by moving the arms and checking the URDFs
mirror them; positions in the plots are now physical degrees relative to the middle pose
(gripper/trigger: 0-100%).

## Teleoperate

Once both arms are calibrated, the follower can mirror the leader (same scheme as
portugal's `follow.rs` and `lerobot-teleoperate`: leader positions normalized through its
calibration, denormalized through the follower's, written as `Goal_Position` — no IK):

```bash
pixi run teleop-so100                          # log-so100 --teleop --fps 60
pixi run teleop-so100 --rr-config.connect      # stream into an already-open viewer
```

Move the leader by hand; the follower tracks it. Everything from `log-so100` still runs
(telemetry, URDFs, cameras), plus the commanded pose is plotted as `<follower>/goal` on
top of the measured position, so tracking lag is directly visible.

Safety, since teleop turns the follower's torque ON:

- goals are clamped to the follower's recorded range-of-motion sweep, so it can't be
  commanded past the limits found during calibration
- on start the follower **glides** to the leader's pose over ~1.5 s instead of jumping —
  still, roughly match the arm poses before starting
- torque is released on exit (Ctrl-C included); if the leader disconnects mid-run the
  follower simply holds its last pose until the leader is back
- `--max-relative-target 5` additionally caps each per-tick goal change to ±5° (off by
  default, matching lerobot)

## Development

```bash
pixi run -e dev lint
pixi run -e dev typecheck
pixi run -e dev deadcode
```

Package layout follows the examples-monorepo conventions: Tyro configs + `main()` live in
`src/so100_hackathon/apis/`, `tools/apps/*.py` are thin shims, beartype instruments the
package when `PIXI_DEV_MODE=1` (dev env).
