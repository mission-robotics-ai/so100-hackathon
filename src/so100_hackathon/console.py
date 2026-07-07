"""Shared rich console and semantic log helpers.

Every human-facing terminal message in the project goes through here so styling
stays consistent. The helpers pass ``markup=False`` on purpose: messages carry
bracketed prefixes (``[catalog]``, ``[scan]`` …) and file paths that rich would
otherwise try to parse as style tags. Colour comes from a whole-line ``style=``,
so the brackets print literally.

Reach for ``console`` directly when you need a rich renderable (table, panel,
rule, ``Live`` …); use the ``info``/``note``/``success``/``warn``/``error``
helpers for one-line status output, picking the one that matches the message.

Note: this module (and the package) is not installed in the isolated ``export``
env, so ``tools/apps/_export_lerobot_writer.py`` cannot import it and stays on
plain ``print``.
"""

from rich import box
from rich.console import Console
from rich.table import Table

# One shared, terminal-width-aware console for all human-facing output (stdout).
console = Console()


def simple_table(title: str | None = None) -> Table:
    """A table in the project's house style (simple box, bold headers).

    Callers add their own columns/rows (plus any per-column ``justify`` or
    per-row ``style``); this only fixes the shared look.
    """
    return Table(box=box.SIMPLE, header_style="bold", title=title)


def info(message: str) -> None:
    """Neutral status / progress line (default style)."""
    console.print(message, markup=False)


def note(message: str) -> None:
    """Secondary hint or aside (dim)."""
    console.print(message, style="dim", markup=False)


def success(message: str) -> None:
    """Completion or confirmation (green)."""
    console.print(message, style="green", markup=False)


def warn(message: str) -> None:
    """Non-fatal caution the run continues past (yellow)."""
    console.print(message, style="yellow", markup=False)


def error(message: str) -> None:
    """Failure the user must notice (bold red)."""
    console.print(message, style="bold red", markup=False)


def enable_pretty_tracebacks() -> None:
    """Install rich's traceback handler for readable uncaught-exception output.

    Call once from an interactive CLI entry point. Do NOT call it from the setup
    server (it parses this process's stdout) or the export writer (whose isolated
    env has no rich). Clean ``raise SystemExit(msg)`` paths are unaffected.
    """
    from rich.traceback import install

    install(console=console, show_locals=False)
