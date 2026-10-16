"""Omarchy palette reader/renderer/stamper for the GPU-safe terminal style.

Pure-Python: a fake $HOME under tmp_path, no browser, no network.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ouroboros import omarchy_theme as ot  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
THEME_SCRIPT = '<script src="/static/theme.js"></script>'

pytestmark = pytest.mark.skipif(ot.tomllib is None, reason="no TOML parser available")

VALID = '''
background = "#1a1b26"
foreground = "#C0CAF5"
accent = "#7aa2f7"
'''


def _write(home: pathlib.Path, text: str | bytes, rel: pathlib.Path = ot.THEME_DIRS[0]) -> pathlib.Path:
    folder = home / rel
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "colors.toml"
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def test_valid_file_yields_lowercased_validated_palette(tmp_path):
    _write(tmp_path, VALID + 'green = "#9ECE6A"\nbrown = "not-read"\n')
    palette = ot.read_palette(tmp_path)
    assert palette == {
        "mode": "dark",
        "background": "#1a1b26",
        "foreground": "#c0caf5",  # upper-case input is normalised
        "accent": "#7aa2f7",
        "green": "#9ece6a",
    }
    assert "brown" not in palette  # unknown keys are ignored, never an error


def test_find_colors_file_prefers_first_dir_and_falls_back(tmp_path):
    assert ot.find_colors_file(tmp_path) is None
    second = _write(tmp_path, VALID, ot.THEME_DIRS[1])
    assert ot.find_colors_file(tmp_path) == second
    first = _write(tmp_path, VALID, ot.THEME_DIRS[0])
    assert ot.find_colors_file(tmp_path) == first


def test_first_directory_is_authoritative_even_when_broken(tmp_path):
    _write(tmp_path, "this is = = not toml", ot.THEME_DIRS[0])
    _write(tmp_path, VALID, ot.THEME_DIRS[1])
    assert ot.read_palette(tmp_path) is None  # never falls through to a stale theme


def test_missing_file_and_directory_in_its_place_yield_none(tmp_path):
    assert ot.read_palette(tmp_path) is None
    (tmp_path / ot.THEME_DIRS[0] / "colors.toml").mkdir(parents=True)
    assert ot.read_palette(tmp_path) is None  # exists() but not is_file()


def test_unresolvable_home_yields_none(monkeypatch):
    def boom():
        raise RuntimeError("no home")

    monkeypatch.setattr(ot.pathlib.Path, "home", staticmethod(boom))
    assert ot.read_palette() is None
    assert ot.omarchy_palette_style() == ""


@pytest.mark.parametrize("body", [
    "background = ",                      # truncated
    'background = "#1a1b26"\n[[[',        # garbage table header
    "this is = = not toml",
])
def test_malformed_toml_yields_none(tmp_path, body):
    _write(tmp_path, body)
    assert ot.read_palette(tmp_path) is None


def test_non_utf8_and_oversized_files_yield_none(tmp_path):
    _write(tmp_path, VALID.encode() + b"\xff\xfe")
    assert ot.read_palette(tmp_path) is None
    _write(tmp_path, VALID + "# " + "x" * ot.MAX_BYTES + "\n")
    assert ot.read_palette(tmp_path) is None


@pytest.mark.parametrize("bad", [
    '"#fff"', '"red"', '"#12345g"', '"#1a1b26ff"', '"1a1b26"', "123",
    '"#1a1b26; } body { display:none"',  # the style-injection shape the hex gate exists for
    '"</style><script>x</script>"',
])
def test_non_hex_values_are_rejected_for_required_and_optional_keys(tmp_path, bad):
    _write(tmp_path, f'background = {bad}\nforeground = "#c0caf5"\naccent = "#7aa2f7"\n')
    assert ot.read_palette(tmp_path) is None
    _write(tmp_path, VALID + f"red = {bad}\n")
    assert ot.read_palette(tmp_path) is None


@pytest.mark.parametrize("missing", ot.REQUIRED_KEYS)
def test_missing_required_key_yields_none(tmp_path, missing):
    lines = [ln for ln in VALID.strip().splitlines() if not ln.startswith(missing)]
    _write(tmp_path, "\n".join(lines))
    assert ot.read_palette(tmp_path) is None


def test_missing_optional_key_is_just_absent(tmp_path):
    _write(tmp_path, VALID)
    palette = ot.read_palette(tmp_path)
    assert palette is not None and "selection" not in palette


@pytest.mark.parametrize("mode", ['"light"', '"bogus"', "1"])
def test_light_or_off_contract_mode_yields_none(tmp_path, mode):
    _write(tmp_path, f"mode = {mode}\n" + VALID)
    assert ot.read_palette(tmp_path) is None
    assert ot.omarchy_palette_style(tmp_path) == ""


def test_explicit_dark_mode_is_accepted(tmp_path):
    _write(tmp_path, 'mode = "dark"\n' + VALID)
    assert ot.read_palette(tmp_path)["mode"] == "dark"


def test_palette_css_emits_colour_and_rgb_channels_in_contract_order():
    css = ot.palette_css({
        "mode": "dark", "background": "#1a1b26", "foreground": "#c0caf5",
        "accent": "#7aa2f7", "dark_background": "#16161e",
    })
    assert css == (
        ":root{--omarchy-color-scheme:dark"
        ";--omarchy-background:#1a1b26;--omarchy-background-rgb:26, 27, 38"
        ";--omarchy-foreground:#c0caf5;--omarchy-foreground-rgb:192, 202, 245"
        ";--omarchy-accent:#7aa2f7;--omarchy-accent-rgb:122, 162, 247"
        ";--omarchy-dark-background:#16161e;--omarchy-dark-background-rgb:22, 22, 30}"
    )  # underscores in keys become hyphens; absent keys are skipped


def test_palette_style_wraps_css_in_the_identified_style_element(tmp_path):
    _write(tmp_path, VALID)
    block = ot.omarchy_palette_style(tmp_path)
    assert block.startswith(f'<style id="{ot.STYLE_ELEMENT_ID}">:root{{')
    assert block.endswith("}</style>")
    assert block.count("<") == 2  # only the open and close tag: values cannot add markup


def test_stamp_sets_attribute_once_and_injects_before_theme_script(tmp_path):
    _write(tmp_path, VALID)
    html = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'{THEME_SCRIPT}<link rel="stylesheet" href="/static/ui.css"></head>'
        "<body><html></body></html>"
    )
    out = ot.stamp_terminal_document(html, tmp_path)
    assert '<html data-ui-style="terminal" lang="en">' in out
    assert out.count('data-ui-style="terminal"') == 1  # first <html only
    style = out.index(f'<style id="{ot.STYLE_ELEMENT_ID}">')
    assert out.index("<meta charset") < style < out.index(THEME_SCRIPT) < out.index("ui.css")
    assert out.count(THEME_SCRIPT) == 1


def test_stamp_falls_back_to_head_close_without_theme_script(tmp_path):
    _write(tmp_path, VALID)
    out = ot.stamp_terminal_document("<html><head><title>x</title></head><body></body></html>", tmp_path)
    assert out.index("</style>") < out.index("</head>")
    assert out.index("<title>") < out.index("<style")


@pytest.mark.parametrize("setup", ["missing", "light", "malformed"])
def test_stamp_without_a_palette_adds_only_the_attribute(tmp_path, setup):
    if setup == "light":
        _write(tmp_path, 'mode = "light"\n' + VALID)
    elif setup == "malformed":
        _write(tmp_path, "= = =")
    html = f"<html><head>{THEME_SCRIPT}</head></html>"
    assert ot.stamp_terminal_document(html, tmp_path) == (
        f'<html data-ui-style="terminal"><head>{THEME_SCRIPT}</head></html>'
    )


@pytest.mark.parametrize("name", ["index.html", "onboarding_template.html"])
def test_stamp_on_real_documents_lands_before_every_stylesheet(tmp_path, name):
    _write(tmp_path, VALID)
    source = (REPO_ROOT / "web" / name).read_text(encoding="utf-8")
    out = ot.stamp_terminal_document(source, tmp_path)
    assert 'data-ui-style="terminal"' in out
    assert out.index("<style id=") < out.index(THEME_SCRIPT) < out.index('rel="stylesheet"')
    assert 'data-ui-style' not in source  # the on-disk template is never pre-stamped
