# Agent notes

SO-100/101 arm, end to end: teleop data collection → Rerun recordings → local catalog →
LeRobot v3 export → New Theory fine-tune → replay. The canonical walkthrough is the
`pixi run learn` course site (`web/content/*.md`); the README is its compressed
CLI-only mirror — keep the two consistent, and learn wins on conflict.

- **pixi only** (no pip/uv). Deps in `pyproject.toml`, run via `pixi run <task>`.
  The `export` env is isolated on purpose (LeRobot's rerun-sdk pin conflicts); `newt`
  tracks git main on purpose — don't pin it.
- **Layout**: library code in `src/so100_hackathon/`, Tyro configs + `main()` in
  `apis/`, `tools/apps/*.py` are thin shims wired to pixi tasks.
- **Checks**: `pixi run -e dev lint`, `typecheck`, `deadcode`; format with `py-fmt`.
- **Gotchas**: one process per arm serial port (the server doesn't hold arms; tools
  do). Recordings are calibrated degrees, LeRobot export is normalized wire units —
  mixing them trains a policy that commands wrong poses.
- **Skills**: Rerun agent skills live in `.agents/skills/` (`.claude/skills/`
  symlinks), locked by `skills-lock.json`. Read `rerun-data-model` before converting
  robot data. Refresh: `npx skills update --project --yes`.
