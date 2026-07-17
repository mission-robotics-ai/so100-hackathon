---
title: "Prepare: export for training"
order: 4
---

Training stacks (LeRobot, and most policies built on it) consume the **LeRobot dataset
format**. The export tool reads your episodes from the local catalog and writes a LeRobot
v3 dataset — by default only the episodes tagged *Good episode*, so the curation you did on
the Refine page carries straight through.

## Export locally

With `pixi run so100-server` still running:

```bash
pixi run export-lerobot -- --dataset <task> --repo-id <team>/<task>
```

This writes a complete LeRobot v3 dataset to `datasets/<team>/<task>/` (parquet episodes +
MP4-encoded camera videos + metadata). Include everything regardless of tag with `--tag ""`.
The repo-id is just a name pair — give it a team-scoped name (`<team>/<task>`) so your
dataset can't collide with another team's.

The mapping out of your recordings:

| LeRobot | from the recording |
| --- | --- |
| `observation.state` | the follower's joint positions (`.../position`), converted to LeRobot's normalized units (arm ±100 over the calibrated range, gripper 0–100 — the convention the SO-100/101 checkpoints train in; `--units degrees` to skip) |
| `action` | the goals the leader commanded (`.../goal`), same units |
| `task` | the task you typed when recording |
| `observation.images.top` / `.side` | one video stream per `camera/cam*`, renamed in cam-index order (`--camera-names` to override) |

## Send it to New Theory

Fine-tuning runs on New Theory's GPUs — your laptop is done the moment the export finishes.
The run is *language-conditioned*: the task sentence you typed while recording becomes the
command your trained model answers. Point the `newt` CLI at the folder you just wrote:

```bash
pixi run newt finetune --dataset ./datasets/<team>/<task>
```

It uploads the dataset, launches a fine-tune of the NT-0 SO-101 base model, and prints a job
handle. To watch the run — and everything around it, from your API key to the trained
checkpoint:

**→ [Fine-tune on the hackathon guide](https://missionrobotics.ai/hackers/fine-tune)**

## Prefer Hugging Face?

For a public dataset or the broader LeRobot ecosystem — not needed for the New Theory path.
Log in once (creates a token at huggingface.co/settings/tokens if you don't have one):

```bash
pixi run -e export hf auth login
```

then export with `--push`:

```bash
pixi run export-lerobot -- --dataset <task> --repo-id <team>/<task> --push
```

Your dataset is now public infrastructure: loadable by anyone (including a training job on
a GPU box) with `LeRobotDataset("<team>/<task>")`.

While it cooks, head to Deploy to see how motion gets back onto the robot.
