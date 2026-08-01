"""Brand asset contract for the web UI and packaged macOS app."""

import json
import re
import struct
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
LOGO_DIR = ROOT / "assets" / "logo"


def _png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def test_logo_svg_is_self_contained_vector():
    """The mark must render anywhere, with nothing to fetch and no live text."""
    logo = LOGO_DIR / "cold-storage-mark.svg"
    root = ElementTree.parse(logo).getroot()
    assert root.attrib["viewBox"] == "0 0 1024 1024"
    source = logo.read_text()
    # No text element: the wordmark is set in the UI, not baked into the icon,
    # so it never renders with a missing font.
    assert "<text" not in source
    # Nothing external — an icon that needs the network is not an icon.
    assert "href=" not in source and "<image" not in source
    # Three faces make the container read as a solid object at any size.
    assert source.count("<polygon") >= 3


def test_flat_glyph_tints_with_the_surrounding_text():
    """The in-app and menu-bar marks are masks, not pictures."""
    glyph = (LOGO_DIR / "cold-storage-glyph.svg").read_text()
    assert "currentColor" in glyph
    assert "linearGradient" not in glyph, "a gradient cannot tint or act as a template"


def test_the_ui_and_tray_use_the_container_mark():
    """A stale shield in the title bar or menu bar is the kind of thing that
    survives a rebrand because nothing points at it."""
    ui = (ROOT / "src" / "coldstorage" / "webapp.py").read_text()
    tray = (ROOT / "app" / "main.js").read_text()
    shield = 'd="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6z"'
    assert shield not in tray, "the menu-bar icon is still the old shield"
    # The brand mark helper and the title bar must both draw the container.
    brand = ui[ui.index("function brandMark("):]
    assert "polygon" in brand[:600], "brandMark() no longer draws the container"


def test_logo_png_family_has_exact_square_dimensions():
    for size in (16, 32, 64, 128, 256, 512, 1024):
        path = LOGO_DIR / f"cold-storage-mark-{size}.png"
        assert _png_size(path) == (size, size)


def test_macos_icon_and_package_configuration():
    icon = ROOT / "app" / "assets" / "icon.icns"
    assert icon.read_bytes()[:4] == b"icns"

    package = json.loads((ROOT / "app" / "package.json").read_text())
    assert package["build"]["mac"]["icon"] == "assets/icon.icns"
    assert "assets/icon.icns" in package["build"]["files"]


def test_native_window_enforces_the_verified_layout_floor():
    main = (ROOT / "app" / "main.js").read_text()
    assert "minWidth: 900" in main
    assert "minHeight: 640" in main


def test_window_background_matches_the_ui_background():
    """A mismatch flashes the wrong colour for a frame on every launch.

    Asserted as a relationship rather than a literal, so the two cannot drift
    apart again the next time the palette changes.
    """
    from coldstorage.webapp import INDEX_HTML

    ui = re.search(r"--bg:\s*(#[0-9a-fA-F]{6})", INDEX_HTML)
    assert ui, "the UI must define a --bg token"
    native = re.search(r'backgroundColor:\s*"(#[0-9a-fA-F]{6})"', (ROOT / "app" / "main.js").read_text())
    assert native, "the native window must set a backgroundColor"
    assert native.group(1).lower() == ui.group(1).lower(), (
        f"native window is {native.group(1)} but the UI paints {ui.group(1)}"
    )
