---
title: "Refine: enrich, query, curate"
order: 3
---

Everything you just recorded is queryable right now, through the catalog served by
`pixi run so100-server` at `rerun+http://localhost:51234`. On disk, each episode is a
file at `recordings/<dataset>/<episode>.rrd` in your kit clone. The task and tag you
typed on the Collect page are real columns.

## List what you have

```bash
pixi run query-dataset
```

lists the datasets in the catalog. Point it at one to see its episodes:

```bash
pixi run query-dataset -- --dataset <name>
```

This prints the **segment table** — one row per episode with its name, task, tag,
duration, and size. The metadata lives in `property:...` columns:

| column | content |
| --- | --- |
| `property:RecordingInfo:name` | the episode name |
| `property:episode:task` | the task (= future LeRobot task) |
| `property:episode:tag` | your curation tag |

## Curate: filter by tag

```bash
pixi run query-dataset -- --dataset <name> --tag "Good episode"
```

Only the episodes you tagged as good — this same filter is what the Prepare page's export
uses by default, so tagging *is* curating. Tagged something wrong? Just re-record; storage
is cheap, and bad episodes stay useful as counterexamples.

## Dig into one episode

Pull a joint-position series into pandas (find entity paths in the viewer's tree, e.g.
`follower/position`):

```bash
pixi run query-dataset -- --dataset <name> --episode episode_01 --entity follower/position
```

The same, from Python — the catalog speaks DataFusion, so this scales from one episode to
the whole dataset:

```python
import os
os.environ["RERUN_INSECURE_SKIP_HOST_CHECK"] = "1"
import rerun as rr
from datafusion import col, lit

client = rr.catalog.CatalogClient("rerun+http://localhost:51234")
dataset = client.get_dataset(name="my_task")

# One row per episode, with your metadata as columns:
segments = dataset.segment_table()
good = segments.filter(col('property:episode:tag')[0] == lit("Good episode"))
print(good.to_pandas())
```

When you're happy with the set of good episodes, move on to Train.
