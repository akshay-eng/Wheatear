"""Startup banner for the interactive wizard.

Visual shape (boxed welcome line, big block-letter logo, boxed notes) is a
familiar CLI pattern; the colors are Wheatear's own -- Waypoint Amber and
Tideline Slate from DESIGN.md, not borrowed from anyone else's palette.
"""

from __future__ import annotations

from pathlib import Path

import pyfiglet
from PIL import Image
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich_pixels import Pixels

AMBER = "#E2924B"  # waypoint-amber
SLATE = "#7C92A6"  # tideline-slate

LOGO_FONT = "ansi_shadow"
MARK_PATH = Path(__file__).parent / "assets" / "logo.png"
MARK_RESIZE = (28, 20)  # half-cell rendering -> 10 terminal rows tall
# Below this alpha, treat a pixel as fully transparent rather than letting
# faint anti-aliased edge noise render as a stray opaque (often near-black)
# block -- rich-pixels otherwise treats any alpha > 0 as fully opaque.
ALPHA_CUTOFF = 40


def _render_mark() -> Pixels | None:
    if not MARK_PATH.exists():
        return None
    img = Image.open(MARK_PATH).convert("RGBA")
    r, g, b, a = img.split()
    a = a.point(lambda v: 255 if v >= ALPHA_CUTOFF else 0)
    clean = Image.merge("RGBA", (r, g, b, a))
    return Pixels.from_image(clean, resize=MARK_RESIZE)


def _logo_lockup(console_width: int) -> Table | Text:
    figlet_text = pyfiglet.figlet_format("WHEATEAR", font=LOGO_FONT).rstrip("\n")
    figlet_width = max((len(line) for line in figlet_text.splitlines()), default=0)
    wordmark = Text(figlet_text, style=f"bold {AMBER}", no_wrap=True, overflow="crop")

    mark = _render_mark()
    if mark is None:
        return wordmark

    mark_width = MARK_RESIZE[0]
    side_by_side_width = mark_width + 2 + figlet_width

    grid = Table.grid(padding=(0, 0, 0, 0) if side_by_side_width > console_width else (0, 2, 0, 0))
    if side_by_side_width <= console_width:
        # Room to sit beside the wordmark. Pixels doesn't report a real
        # __rich_measure__, so the mark column gets an explicit width --
        # otherwise Table guesses and the wordmark column gets squeezed.
        grid.add_column(justify="center", vertical="middle", width=mark_width)
        grid.add_column(vertical="middle", width=figlet_width)
        grid.add_row(mark, wordmark)
    else:
        # Too narrow -- stack instead of cropping the wordmark.
        grid.add_column(justify="center", vertical="middle")
        grid.add_row(mark)
        grid.add_row(wordmark)
    return grid


def print_banner(console: Console) -> None:
    console.print(
        Panel(
            "[bold]Welcome to Wheatear[/bold] -- migrate AI agents between orchestration platforms.",
            border_style=AMBER,
            box=box.ROUNDED,
            padding=(0, 2),
        )
    )

    console.print(_logo_lockup(console.width))

    console.print(
        Panel(
            "[bold]Notes:[/bold]\n\n"
            "1. [bold]Wheatear is early-stage[/bold]\n"
            "   When a review-manifest.yaml is generated, read it before importing.\n\n"
            "2. [bold]Only the Translate stage uses AI[/bold]\n"
            "   Extract, Map, Validate, and Export are deterministic and auditable.\n\n"
            "3. [bold]Your API key stays local[/bold]\n"
            "   Used for this session only -- never written to disk.",
            title="[bold]Before you start[/bold]",
            border_style=SLATE,
            box=box.ROUNDED,
            padding=(1, 2),
        )
    )
