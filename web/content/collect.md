---
title: "Collect"
order: 2
---

Make sure `pixi run so100-server` is still running, then work straight from this page.

- **Livestream** — operate the robot in real time. It is one continuous stream for the whole session, kept only in memory and flushed oldest-first when memory runs low. Nothing is stored and nothing piles up. 
- **Recording** — once you're ready to collect hit that record button. It will be stored in the local server from where you could query it later when curating dataset for training.

<div data-collect></div>

## The episode panel

- **Dataset** — a named collection of episodes; on disk it's the folder
  `recordings/<dataset>/`, in the catalog it's a dataset of the same name. 
- **Episode id** — assigned by the server, never editable: `episode_01`, `episode_02`, …
  always the highest existing number plus one, so an id refers to one take forever. The
  mark next to the id shows its state: nothing (not recorded yet), a red dot (recording
  right now), a green check (saved to the catalog).
- **Task** — the natural-language task description, e.g. *"Pick up the ball and place it
  in the box"*. It becomes the LeRobot **task** string when you later export — and for a
  language-conditioned policy like NewTheory's NT-0 it *is* the command the trained model
  responds to, so write a real imperative sentence, not `task1`.
- **Tag** — a curation label: *Good episode*, *Bad episode*, or *Needs review*. You'll
  filter by it on the Refine page.

**Start recording** writes the take straight to disk while the viewer keeps showing the
livestream — you watch the robot, not a file. On **Stop current recording** the file is
compacted, registered into the local catalog, and the viewer opens the fresh episode
**straight from the catalog** for review; 

## Camera placement

Place your two cameras as **third-person views**: one **top** camera looking down at the
workspace, one **side** camera looking across it. The first detected camera (`cam0`)
exports as `top`, the second (`cam1`) as `side` — check the livestream to confirm which
is which before you record, and keep the placement fixed for the whole dataset (and, for
the hackathon, as close to the other rigs as you can — pooled training assumes consistent
viewpoints). Avoid wrist-mounted cameras: the SO-101 base model these fine-tunes start
from was trained on third-person views only.

Only external USB cameras are picked up automatically — the Mac's built-in webcam and
iPhone Continuity Cameras are skipped (the server prints each skipped camera and its
index at connect time). Short a USB camera? You can opt one in by starting the server
with its index, e.g. `pixi run so100-server --cameras 0 2` — but remember the built-in
camera moves with the laptop lid, so the view your policy trains on only stays valid if
the laptop stays put.

## Prefer the terminal?

The standalone CLI records + registers without touching this page (it opens the arms
itself, so if you started the livestream above, press **Stop the feed** first — serial
ports are exclusive):

```bash
pixi run record-episode -- --dataset my_task --task "Pick up the ball" --tag "Good episode"
```

Enter stops the take (or pass `--seconds 15`). The episode id defaults to the next free
`episode_NN`, exactly like the panel. And the control API this page uses is plain JSON
over HTTP, so `curl` works too:

```bash
curl -X POST localhost:8000/arms/connect
curl -X POST localhost:8000/live/pause      # and /live/resume: same stream, gap in between
curl -X POST localhost:8000/start -d '{"dataset": "my_task", "task": "Pick up the ball"}'
curl -X POST localhost:8000/stop  -d '{"tag": "Good episode"}'
curl localhost:8000/episodes?dataset=my_task
curl -X POST localhost:8000/episode/update \
  -d '{"dataset": "my_task", "episode": "episode_01", "task": "Pick up the ball", "tag": "Bad episode"}'
```

Either way the result is the same file in `recordings/<dataset>/`, registered to the same
catalog — which is what we'll query next.
