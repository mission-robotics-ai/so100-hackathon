"""Query the local recording catalog from the command line (the Refine step).

Talks to the catalog served by ``pixi run so100-server`` at
``rerun+http://localhost:51234``. The metadata stamped on each take (episode name,
task, curation tag) comes back as ``property:...`` columns::

    pixi run query-dataset                             # list datasets
    pixi run query-dataset -- --dataset my_task        # one row per episode
    pixi run query-dataset -- --dataset my_task --tag "Good episode"
    pixi run query-dataset -- --dataset my_task --episode episode_1 --entity follower/position
"""

from __future__ import annotations

import dataclasses
import os
from typing import Any

import pandas as pd
import tyro
from datafusion import col, lit

from so100_hackathon.console import console, enable_pretty_tracebacks, info, note, simple_table

os.environ.setdefault("RERUN_INSECURE_SKIP_HOST_CHECK", "1")

import rerun as rr  # noqa: E402 - the env var above must be set before use

SEGMENT_COLUMNS = {
    "rerun_segment_id": "segment_id",
    "property:RecordingInfo:name": "episode",
    "property:episode:task": "task",
    "property:episode:tag": "tag",
    "rerun_size_bytes": "size",
}


def _flatten(value: Any) -> Any:
    """Property columns are list-typed (one value per layer); show the first."""
    if isinstance(value, str) or value is None:
        return value
    try:
        return value[0] if len(value) else ""
    except TypeError:
        return value


def _print_frame(df: pd.DataFrame, *, max_cell: int | None = None) -> None:
    """Render a DataFrame as a terminal-width-aware table (pandas' to_string ignores it)."""
    table = simple_table()
    for column in df.columns:
        table.add_column(str(column), overflow="fold")
    for row in df.itertuples(index=False):
        cells = (str(value) for value in row)
        table.add_row(*(cell[:max_cell] + "…" if max_cell and len(cell) > max_cell else cell for cell in cells))
    console.print(table)


def _human_size(size_bytes: float) -> str:
    size = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def list_datasets(client: rr.catalog.CatalogClient) -> None:
    names = sorted(client.dataset_names())
    if not names:
        note("no datasets yet -- record one first (Collect page or `pixi run record-episode`)")
        return
    table = simple_table(title=f"{len(names)} dataset(s) in the catalog")
    table.add_column("dataset")
    table.add_column("episodes", justify="right")
    for name in names:
        count = len(client.get_dataset(name=name).segment_ids())
        table.add_row(name, str(count))
    console.print(table)
    note("details: pixi run query-dataset -- --dataset <name>")


def show_segment_table(dataset: rr.catalog.DatasetEntry, tag: str | None) -> pd.DataFrame:
    table = dataset.segment_table()
    if tag:
        table = table.filter(col("property:episode:tag")[0] == lit(tag))
    df = table.to_pandas()
    if df.empty:
        note(f"no episodes{f' tagged {tag!r}' if tag else ''} in dataset '{dataset.name}'")
        return df

    view = df[[column for column in SEGMENT_COLUMNS if column in df.columns]].rename(columns=SEGMENT_COLUMNS)
    for column in ("episode", "task", "tag"):
        if column in view.columns:
            view[column] = view[column].map(_flatten)
    if {"time:start", "time:end"} <= set(df.columns):
        seconds = (df["time:end"] - df["time:start"]).dt.total_seconds()  # pyrefly: ignore[missing-attribute]
        view["duration"] = seconds.map(lambda s: f"{s:.1f}s")
    if "size" in view.columns:
        view["size"] = view["size"].map(_human_size)
    view = view.sort_values("episode").reset_index(drop=True)
    info(f"dataset '{dataset.name}'{f', tag {tag!r}' if tag else ''}: {len(view)} episode(s)")
    _print_frame(view)
    return view


def show_entity_series(dataset: rr.catalog.DatasetEntry, *, episode: str | None, tag: str | None, entity: str) -> None:
    view = dataset
    if episode is not None:
        table = dataset.segment_table().to_pandas()
        names = table["property:RecordingInfo:name"].map(_flatten)
        matches = table.loc[names == episode, "rerun_segment_id"].tolist()
        if not matches:
            raise SystemExit(f"no episode named '{episode}' in dataset '{dataset.name}' (see `--dataset {dataset.name}` for the list)")
        view = view.filter_segments(matches)
    elif tag:
        view = view.filter_segments(dataset.segment_table().filter(col("property:episode:tag")[0] == lit(tag)))

    df = view.filter_contents([entity]).reader(index="time").to_pandas()
    data_columns = [column for column in df.columns if column not in ("rerun_segment_id", "log_time", "log_tick")]
    if len(data_columns) <= 1:  # only the index column came back
        entities = ", ".join(str(path) for path in dataset.schema().entity_paths())
        raise SystemExit(f"entity '{entity}' has no data here; entities in this dataset: {entities}")

    scope = f"episode '{episode}'" if episode else (f"episodes tagged {tag!r}" if tag else "all episodes")
    info(f"'{entity}' across {scope}: {len(df)} rows")
    _print_frame(df[data_columns].head(10), max_cell=48)
    numeric = df[data_columns].select_dtypes("number")
    if not numeric.empty:
        note("summary:")
        _print_frame(numeric.describe().rename_axis("stat").reset_index())


@dataclasses.dataclass
class Config:
    dataset: str | None = None
    """Catalog dataset to inspect. Omit to list all datasets."""

    tag: str | None = None
    """Only episodes with this curation tag (e.g. "Good episode")."""

    episode: str | None = None
    """Zoom into one episode by name (as shown in the episode column)."""

    entity: str | None = None
    """Entity path to pull as a series (e.g. ``follower/position``), printed via pandas."""

    catalog_port: int = 51234
    """so100-server catalog port."""


def main(config: Config) -> None:
    client = rr.catalog.CatalogClient(f"rerun+http://localhost:{config.catalog_port}")
    if config.dataset is None:
        list_datasets(client)
        return
    dataset = client.get_dataset(name=config.dataset)
    if config.entity is not None:
        show_entity_series(dataset, episode=config.episode, tag=config.tag, entity=config.entity)
    else:
        show_segment_table(dataset, config.tag)


if __name__ == "__main__":
    enable_pretty_tracebacks()
    try:
        main(tyro.cli(Config))
    except ConnectionError as error:
        raise SystemExit(f"cannot reach the catalog -- is `pixi run so100-server` running? ({error})") from None
