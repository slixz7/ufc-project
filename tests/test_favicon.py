"""
The browser-tab icon: "UFC" in the site's own typeface and colours (not an emoji), passed to Streamlit as an image file. These checks keep the file a small, square, on-palette PNG, and keep an emoji
from being put back.

The daily job installs only the pipeline's packages, which do NOT include the image library Pillow (the web app's Streamlit brings it in). So the checks that need Pillow skip when it is missing, and the
only one that guards the icon's format everywhere (it has an alpha channel) reads the PNG header with the standard library. A test that needs a package the daily job lacks must skip, never crash.
"""
import re
import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ICON = ROOT / "app" / "assets" / "favicon.png"
APP = (ROOT / "app" / "streamlit_app.py").read_text(encoding="utf-8")


def png_size(data: bytes):
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return struct.unpack(">II", data[16:24])


def test_the_icon_is_a_small_square_png_big_enough_for_high_density_screens():
    data = ICON.read_bytes()
    w, h = png_size(data)
    assert w == h and 128 <= w <= 512, (w, h)
    assert len(data) < 30_000, len(data)                                                                                     # it is sent to every visitor


def test_the_icon_is_saved_with_an_alpha_channel_so_streamlit_serves_the_png_instead_of_re_encoding_it_as_a_blurry_jpeg():
    data = ICON.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    bit_depth, colour_type = struct.unpack(">BB", data[24:26])                                                                  # the PNG header, read without any image library
    assert (bit_depth, colour_type) == (8, 6), "colour type 6 is RGBA: an opaque RGB file is turned into a JPEG by Streamlit (found by asking a real browser)"


def test_the_alpha_channel_is_fully_opaque_a_solid_tile_with_no_transparent_corners():
    Image = pytest.importorskip("PIL.Image", reason="Pillow is installed with the web app, not in the daily job")
    im = Image.open(ICON)
    assert im.mode == "RGBA" and im.getchannel("A").getextrema() == (255, 255)


def test_the_icon_uses_the_sites_own_ink_and_accent_and_is_not_blank():
    Image = pytest.importorskip("PIL.Image", reason="Pillow is installed with the web app, not in the daily job")
    css = {k: v for k, v in re.findall(r"--(ink|accent|paper):(#[0-9a-fA-F]{6});", APP)}
    assert set(css) == {"ink", "accent", "paper"}
    to_rgb = lambda hx: tuple(int(hx[i:i + 2], 16) for i in (1, 3, 5))                                                         # noqa: E731
    im = Image.open(ICON).convert("RGB")
    colours = im.getcolors(maxcolors=100_000)
    counts = {c: n for n, c in colours}
    total = im.size[0] * im.size[1]
    assert counts.get(to_rgb(css["ink"]), 0) > 0.40 * total                                                                    # a dark tile
    assert counts.get(to_rgb(css["accent"]), 0) > 0.05 * total                                                                 # with the red bar
    assert sum(n for c, n in counts.items() if all(abs(c[i] - to_rgb(css["paper"])[i]) < 12 for i in range(3))) > 0.08 * total  # and the light letters


def test_the_app_passes_that_file_as_its_page_icon_and_no_emoji_icon_remains_anywhere():
    call = APP[APP.index("st.set_page_config("):APP.index("\n", APP.index("st.set_page_config("))]
    assert 'page_icon=str(FAVICON) if FAVICON.is_file() else None' in call and 'page_title="UFC Fight Predictor"' in call
    assert 'FAVICON = Path(__file__).resolve().parent / "assets" / "favicon.png"' in APP
    assert APP.index("FAVICON = ") < APP.index("st.set_page_config(")                                                          # defined before it is used, and before any other Streamlit call
    for path in list((ROOT / "app").glob("*.py")) + list((ROOT / "demo").glob("*.py")):
        assert "🥊" not in path.read_text(encoding="utf-8"), path.name
    assert not re.search(r'page_icon\s*=\s*"[^\w\s"]', APP)                                                                    # no string that starts with a symbol, which is how an emoji is passed


def test_the_runbook_says_where_the_icon_lives_and_how_to_change_it():
    runbook = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    for needle in ("## The browser-tab icon", "app/assets/favicon.png", "Ctrl+F5", "tests/test_favicon.py"):
        assert needle in runbook, needle
