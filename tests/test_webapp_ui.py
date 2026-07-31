"""UI-template checks for the web app: structure, safety and no-emoji policy.

The API surface is covered by test_webapp.py; these tests pin down the
presentation layer — the key structural hooks the JS relies on, the safety
conventions (esc() everywhere, index-based event delegation, no inline
onclick with interpolated user data) and the hard rule that no emoji appear
anywhere in the UI chrome.
"""

import re

from saveyourshit.config import Config
from saveyourshit.store.archive import Archive
from saveyourshit.webapp import INDEX_HTML, handle

# Common emoji blocks: misc symbols/dingbats, emoticons, transport, supplemental
# symbols, flags, plus the emoji variation selector.
EMOJI_RE = re.compile(
    "["
    "\U00002600-\U000027bf"  # misc symbols + dingbats
    "\U0001f000-\U0001faff"  # emoticons, symbols, transport, supplemental
    "\U0001f1e6-\U0001f1ff"  # regional indicators (flags)
    "\U00002b00-\U00002bff"  # misc symbols and arrows
    "\U0000fe0f"  # variation selector-16
    "]"
)


def test_no_emoji_in_template():
    match = EMOJI_RE.search(INDEX_HTML)
    assert match is None, f"emoji found in UI template: {match.group()!r}"


def test_index_route_serves_template(layout):
    with Archive(layout) as arc:
        code, ctype, body = handle("/", {}, arc, Config())
    assert code == 200
    assert "text/html" in ctype
    assert body.decode("utf-8") == INDEX_HTML


def test_structural_hooks_present():
    hooks = [
        # layout skeleton the JS renders into
        'id="app"',
        'id="rail"',
        'id="list"',
        'id="pane"',
        # API endpoints wired in the client
        "/api/status",
        "/api/threads?connector=",
        "/api/thread?connector=",
        "/api/records?connector=",
        # views and components
        "showDashboard",
        "openConnector",
        "openThread",
        "openSpecial",
        "renderThreadRows",
        "filterThreads",
        "daysep",
        "msgstream",
        'class="search"',
        # brand tiles per platform (kept from the original design)
        "function tile(",
        "BRAND",
    ]
    for hook in hooks:
        assert hook in INDEX_HTML, f"missing structural hook: {hook}"


def test_polish_hooks_present():
    """The redesign's states: loading skeletons, empty states, motion, focus."""
    for hook in [
        "prefers-reduced-motion",
        ":focus-visible",
        "emptyState(",
        "skRows(",
        "skMsgs(",
        "spinner",
        "tabular-nums",
        "No results",
        "No data yet",
        "No conversations",
        "@media (max-width:1100px)",
    ]:
        assert hook in INDEX_HTML, f"missing polish hook: {hook}"


def test_blue_brand_system_is_used_in_the_ui():
    assert "--brand:#2563eb" in INDEX_HTML.lower()
    assert "--brand-strong:#1d4ed8" in INDEX_HTML.lower()
    assert "function brandMark(" in INDEX_HTML
    assert 'aria-label="Save Your Shit"' in INDEX_HTML
    assert "#d9a05b" not in INDEX_HTML.lower()
    assert "#ecb873" not in INDEX_HTML.lower()


def test_narrow_rail_and_empty_archive_copy_are_app_native():
    assert ".sec{ display:none; }" in INDEX_HTML
    assert "Archive → Add Export" in INDEX_HTML
    assert "run <code>syt ingest" not in INDEX_HTML


def test_favicon_request_is_quiet(layout):
    with Archive(layout) as arc:
        code, ctype, body = handle("/favicon.ico", {}, arc, Config())
    assert code == 204
    assert ctype == "image/x-icon"
    assert body == b""


def test_safety_conventions():
    # All user data goes through esc(); clicks use index-based delegation, so
    # thread names with quotes/unicode never end up inside inline handlers.
    assert "function esc(" in INDEX_HTML
    assert "data-i" in INDEX_HTML
    assert 'onclick="' not in INDEX_HTML
    assert "javascript:" not in INDEX_HTML
    # No external resources: must work fully offline.
    assert "http://" not in INDEX_HTML.replace("http://127.0.0.1", "")
    assert "https://" not in INDEX_HTML
    assert "@import" not in INDEX_HTML
    assert "<link" not in INDEX_HTML
