"""Active Omarchy palette for the GPU-safe terminal style.

When ``OUROBOROS_GPU_SAFE`` is on, the gateway stamps ``data-ui-style="terminal"``
on the host document and inlines the desktop's current Omarchy colours as
``--omarchy-*`` custom properties. ``web/ui.css`` maps those onto the app tokens
with bundled Tokyo Night fallbacks, so this module only ever *adds* colour: a
missing, unreadable, malformed or off-contract ``colors.toml`` yields nothing
and the page paints Tokyo Night. Values are ``#RRGGBB`` only, which is also what
makes inlining them into a ``<style>`` element safe.
"""

from __future__ import annotations

import pathlib
import re

try:
    import tomllib
except ImportError:  # pragma: no cover - 3.10 venvs may ship tomli
    try:
        import tomli as tomllib
    except ImportError:  # neither: no Omarchy palette, Tokyo Night paints
        tomllib = None

STYLE_ELEMENT_ID = "omarchy-palette"
# Searched in order under $HOME; the first directory holding colors.toml is
# authoritative, so a broken current theme never falls through to a stale one.
THEME_DIRS = (
    pathlib.Path(".config/omarchy/current/theme"),
    pathlib.Path(".local/state/omarchy/current/theme"),
)
MAX_BYTES = 64 * 1024
REQUIRED_KEYS = ("background", "foreground", "accent")
OPTIONAL_KEYS = (
    "selection",
    "muted",
    "dark_background",
    "lighter_background",
    "dark_foreground",
    "light_foreground",
    "bright_foreground",
    "red",
    "yellow",
    "orange",
    "green",
    "cyan",
    "blue",
    "magenta",
    "bright_red",
    "bright_yellow",
    "bright_green",
    "bright_cyan",
    "bright_blue",
)
_HEX = re.compile(r"#[0-9a-fA-F]{6}")
_MODES = ("dark", "light")
_THEME_SCRIPT = '<script src="/static/theme.js"></script>'


def find_colors_file(home: pathlib.Path | None = None) -> pathlib.Path | None:
    base = pathlib.Path.home() if home is None else home
    for rel in THEME_DIRS:
        candidate = base / rel / "colors.toml"
        if candidate.exists():  # follows Omarchy's current/ symlink
            return candidate
    return None


def read_palette(home: pathlib.Path | None = None) -> dict[str, str] | None:
    """Return validated Omarchy colours keyed by their colors.toml name, or None.

    None on: no TOML parser, no colors.toml, a non-file, unreadable, oversized
    or non-UTF-8 file, a TOML error, a missing required key, any read key that
    is not a ``#RRGGBB`` string, or a ``mode`` other than dark/light. A missing
    optional key is just absent, so ui.css keeps Tokyo Night for that token.
    Unknown keys (``brown``, future additions) are ignored.
    """
    if tomllib is None:
        return None
    try:
        path = find_colors_file(home)
        if path is None or not path.is_file():
            return None
        with path.open("rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return None
        data = tomllib.loads(raw.decode("utf-8"))
    except (OSError, RuntimeError, ValueError):
        # RuntimeError: Path.home() without a resolvable home.
        # ValueError: UnicodeDecodeError and TOMLDecodeError both subclass it.
        return None

    mode = data.get("mode", "dark")
    if not isinstance(mode, str) or mode != "dark":
        # Terminal style forces dark (theme.js lock); a light Omarchy theme
        # yields no palette so Tokyo Night dark paints (plan slot_1:f2).
        return None
    palette = {"mode": mode}
    for key in REQUIRED_KEYS + OPTIONAL_KEYS:
        if key not in data:
            if key in REQUIRED_KEYS:
                return None
            continue
        value = data[key]
        if not isinstance(value, str) or not _HEX.fullmatch(value):
            return None
        palette[key] = value.lower()
    return palette


def _rgb(value: str) -> str:
    return ", ".join(str(int(value[i : i + 2], 16)) for i in (1, 3, 5))


def palette_css(palette: dict[str, str]) -> str:
    """Render ``:root{--omarchy-*}`` for a palette from ``read_palette``.

    Every colour also gets an ``-rgb`` channel triple so ui.css can build the
    alpha tints (``--accent-10`` and friends) from the same source.
    """
    decls = [f"--omarchy-color-scheme:{palette['mode']}"]
    for key in REQUIRED_KEYS + OPTIONAL_KEYS:
        if key in palette:
            name = "--omarchy-" + key.replace("_", "-")
            decls.append(f"{name}:{palette[key]}")
            decls.append(f"{name}-rgb:{_rgb(palette[key])}")
    return ":root{" + ";".join(decls) + "}"


def omarchy_palette_style(home: pathlib.Path | None = None) -> str:
    """The inline ``<style>`` for the active palette, or "" on any failure."""
    palette = read_palette(home)
    if palette is None:
        return ""
    return f'<style id="{STYLE_ELEMENT_ID}">{palette_css(palette)}</style>'


def stamp_terminal_document(html: str, home: pathlib.Path | None = None) -> str:
    """Mark a host document for the terminal style and inline the palette.

    Same first-occurrence rewrite as the Telegram host hint in
    ``server_web.make_index_page``. The palette goes in front of theme.js (so
    before every stylesheet, and after ``<meta charset>``), else before
    ``</head>``. Without a palette only the attribute is added.
    """
    html = html.replace("<html", '<html data-ui-style="terminal"', 1)
    block = omarchy_palette_style(home)
    if not block:
        return html
    if _THEME_SCRIPT in html:
        return html.replace(_THEME_SCRIPT, block + _THEME_SCRIPT, 1)
    return html.replace("</head>", block + "</head>", 1)
