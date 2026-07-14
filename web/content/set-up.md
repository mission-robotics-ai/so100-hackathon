---
title: "Set up"
order: 1
---


You're reading this from `pixi run learn`, so the install already happened — every
command in this dataset loop is a `pixi run ...` task from the repo root, and every step
on this page runs in the embedded [Rerun](https://rerun.io/) viewer. (Setting up a
teammate's machine? The bootstrap commands are at the top of the repo's `README.md`.)

<aside class="highlight">
<p>Prefer doing everything from the terminal without this site? The repo's `README.md` is a
compressed, CLI-only version of exactly these steps.</p>
</aside>


## First: Start the local data server

One process owns all your data today: it stores every episode and makes it queryable — powered by [Rerun](https://rerun.io/)'s open-source server. In a terminal at the repo root:

```bash
pixi run so100-server
```

Leave it running for the rest of the day. It hosts:

* a live stream this site's embedded viewers connect to (port 9876)
* a catalog of all your recordings (port 51234)
* a small control API that powers the buttons on this page and the Collect page (port 8000)

It does
**not** hold the arms — the serial ports stay free, so the setup tools below (and your own
terminal runs) can grab them anytime.


## Set up your SO-100 arms

If you've worked with SO-100 arms before you know they should be calibrated before using
them to collect data. The card below walks you through it in three steps, all sharing one
embedded [Rerun](https://rerun.io) viewer — only one tool runs at a time:

1. **Ping** — see if your machine recognizes the arms at all.
2. **Calibrate** — walk through calibration, leader arm then follower.
3. **Verify teleop** — test that teleoperation works as expected.

<div data-setup></div>

Prefer the terminal? The same four tools, in order:

```bash
pixi run so100-server              # the local data server (keep it running)
pixi run log-so100                 # ping: stream live arm telemetry
pixi run calibrate-so100 leader    # calibrate the leader
pixi run calibrate-so100 follower  # calibrate the follower
pixi run teleop-so100              # verify teleop: follower mirrors the leader
```
