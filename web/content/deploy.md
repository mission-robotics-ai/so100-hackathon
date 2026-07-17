---
title: "Deploy: close the loop"
order: 5
---

The last step is getting motion *back onto* the robot. The simplest deployment is replay:
drive the follower through the action trajectory of an episode you recorded — no policy
needed, and it proves the whole chain (dataset → actions → servos) works.

<div data-viewer></div>

## Replay an episode

With `pixi run so100-server` running and the follower plugged in (leader not needed —
but **disconnect the arms** on the Collect page first if you connected them there, the
serial port is exclusive):

```bash
pixi run replay-episode -- --dataset my_task --episode episode_01
```

The tool queries the episode's action series from the catalog, ramps the follower gently to
the starting pose, then plays the trajectory at recorded speed (`--speed 0.5` for half).
The replayed joints stream to the viewer above, so you can compare against the recording.
Torque is released when it finishes. Keep a hand near the arm on first replay.

## Deploy your fine-tune

A trained policy is the same loop with the trajectory computed live instead of read from
the catalog: observe (joint state + camera frames) → infer an action chunk → drive the
follower.

Deployment is NewTheory's side of the loop. Once your fine-tune is live, the `newt` SDK
runs that loop against your arm directly — `Robot(model="<your-tag>")` and the follower
executes the task sentence you trained on (try the exact strings you recorded with
first, then paraphrases). The guide picks up exactly where the training run finishes:
calling your model by name and checking its action chunks, then on to the arm moving:

**→ [Call your model on the hackathon guide](https://missionrobotics.ai/hackers/call-your-model)**

One thing you do *not* need to redo: calibration. When you calibrated on the Set-up
page, the same calibration was also written in LeRobot's format where the newt client
reads it — so the joint angles your checkpoint trained on and the ones it commands at
inference mean the same pose. Do **disconnect the arms** on the Collect page before
running the policy: the serial port is exclusive.

## Close the loop

That's the whole cycle you now own end to end:

**collect** → **refine** → **train** → **deploy** → watch where the policy struggles →
**collect** exactly those cases into a new dataset → repeat.

The server keeps running through all of it; every dataset you add is just another folder
under `recordings/` and another name in the catalog. Go collect the data your robot is
missing.
