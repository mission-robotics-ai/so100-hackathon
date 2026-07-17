---
title: "Prepare: export for training"
order: 4
---

Fine-tuning runs on New Theory's GPUs — your laptop is done the moment your episodes leave
it. One command does the whole handoff: it converts your recording into the **LeRobot dataset
format** training stacks consume, then launches the run.

## Send it to New Theory

With `pixi run so100-server` still running:

```bash
pixi run finetune -- --dataset <task>
```

`<task>` is the dataset name you chose when you recorded — the only name you type. The command
exports your episodes to a LeRobot v3 dataset under `datasets/local/<task>/` and hands that
folder straight to a fine-tune of the NT-0 SO-101 base model, printing a job handle. It trains
on every usable episode; any that are missing a camera or motion stream get reported and
skipped, not silently dropped.

The run is *language-conditioned*: the task sentence you typed while recording becomes the
command your trained model answers.

**→ [Fine-tune on the hackathon guide](https://missionrobotics.ai/hackers/fine-tune)**

What lands in the dataset, out of your recordings:

| LeRobot | from the recording |
| --- | --- |
| `observation.state` | the follower's joint positions (`.../position`), converted to LeRobot's normalized units (arm ±100 over the calibrated range, gripper 0–100 — the convention the SO-100/101 checkpoints train in) |
| `action` | the goals the leader commanded (`.../goal`), same units |
| `task` | the task you typed when recording |
| `observation.images.top` / `.side` | one video stream per `camera/cam*`, renamed in cam-index order |

While it cooks, head to Deploy to see how motion gets back onto the robot.

## Prefer Hugging Face?

For a public dataset or the broader LeRobot ecosystem — not needed for the New Theory path.
This is the manual export: it writes the LeRobot dataset to disk on its own (so you can inspect
it), and with `--push` uploads it to the Hub. Log in once (creates a token at
huggingface.co/settings/tokens if you don't have one):

```bash
pixi run -e export hf auth login
```

then export with a repo id:

```bash
pixi run export-lerobot -- --dataset <task> --repo-id <team>/<task> --push
```

`<team>` is a name you make up on the spot — your team name (nothing to register; it's a
folder prefix that keeps your dataset from colliding with another team's). This path exports
only the episodes tagged *Good episode* by default, so the curation you did on the Refine page
carries through; add `--tag ""` to include everything.

Your dataset is now public infrastructure: loadable by anyone (including a training job on
a GPU box) with `LeRobotDataset("<team>/<task>")`.
