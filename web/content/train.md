---
title: "Train: prepare for training"
order: 4
---

Training stacks (LeRobot, and most policies built on it) consume the **LeRobot dataset
format**. The export tool reads your episodes from the local catalog and writes a LeRobot
v3 dataset — by default only the episodes tagged *Good episode*, so the curation you did on
the Refine page carries straight through.

## Export locally

With `pixi run so100-server` still running:

```bash
pixi run export-lerobot -- --dataset my_task --repo-id <your-hf-user>/my_task
```

This writes a complete LeRobot v3 dataset to `datasets/<your-hf-user>/my_task/` (parquet
episodes + MP4-encoded camera videos + metadata). Include everything regardless of tag with
`--tag ""`.

The mapping out of your recordings:

| LeRobot | from the recording |
| --- | --- |
| `observation.state` | the follower's joint positions (`.../position`), converted to LeRobot's normalized units (arm ±100 over the calibrated range, gripper 0–100 — the convention the SO-100/101 checkpoints train in; `--units degrees` to skip) |
| `action` | the goals the leader commanded (`.../goal`), same units |
| `task` | the task you typed when recording |
| `observation.images.top` / `.side` | one video stream per `camera/cam*`, renamed in cam-index order (`--camera-names` to override) |

## Push to the Hugging Face Hub

Log in once (creates a token at huggingface.co/settings/tokens if you don't have one):

```bash
pixi run -e export hf auth login
```

then export with `--push`:

```bash
pixi run export-lerobot -- --dataset my_task --repo-id <your-hf-user>/my_task --push
```

Your dataset is now public infrastructure: loadable by anyone (including a training job on
a GPU box) with `LeRobotDataset("<your-hf-user>/my_task")`.

## Fine-tune with NewTheory

Fine-tuning is NewTheory's side of the loop: their `newt` CLI takes the LeRobot dataset
you just exported and fine-tunes the NT-0 SO-101 base model on it, on their GPUs — your
laptop is done working. It is *language-conditioned*: the task sentence you typed while
recording becomes the command the trained model responds to.

Everything you need — installing the `newt` CLI, your API key, launching the run, and
watching it train — is in their guide:

**→ [How fine-tuning works](https://newtheory-docs.vercel.app/docs/nt-0/how-finetune-works)**

While it cooks, head to Deploy to see how motion gets back onto the robot.
