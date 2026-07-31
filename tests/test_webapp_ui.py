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
        "Nothing backed up yet",
        "No conversations",
        "@media (max-width:1100px)",
    ]:
        assert hook in INDEX_HTML, f"missing polish hook: {hook}"


def test_brand_mark_and_no_legacy_palettes():
    assert "function brandMark(" in INDEX_HTML
    assert 'aria-label="Save Your Shit"' in INDEX_HTML
    lowered = INDEX_HTML.lower()
    for legacy in ("#d9a05b", "#ecb873", "#2563eb", "#1d4ed8"):
        assert legacy not in lowered, f"legacy brand colour still present: {legacy}"


def test_narrow_rail_and_empty_archive_copy_are_app_native():
    assert ".sec{ display:none; }" in INDEX_HTML
    # The empty state routes people to the in-app Connect flow, not a native
    # menu path — the app is the center of everything now.
    assert "Connect a platform" in INDEX_HTML
    assert "Archive → Add Export" not in INDEX_HTML


def test_in_app_connect_flow_is_wired():
    """The desktop app connects accounts and backs up without the CLI."""
    # The native bridge + in-app flows.
    assert "window.sytBridge" in INDEX_HTML
    assert "function showConnect(" in INDEX_HTML
    assert "BRIDGE.addExport" in INDEX_HTML
    assert "handleIngest" in INDEX_HTML
    # Integrated, draggable title bar that hosts the window controls.
    assert "-webkit-app-region:drag" in INDEX_HTML
    assert 'class="titlebar"' in INDEX_HTML


def test_automation_controls_are_wired():
    """Connect once, pick a frequency, and it runs itself."""
    for hook in [
        "BRIDGE.connect",
        "BRIDGE.disconnect",
        "BRIDGE.setSchedule",
        "BRIDGE.syncNow",
        "BRIDGE.syncAll",
        "onAccounts",
        "function acctState(",
        "SCHEDULES",
        "launchAtLogin",
    ]:
        assert hook in INDEX_HTML, f"missing automation hook: {hook}"
    # Every schedule the scheduler understands is offered in the UI.
    for freq in ("daily", "weekly", "monthly", "manual"):
        assert f"'{freq}'" in INDEX_HTML
    # Account state is rendered from data, never hardcoded per platform.
    assert "ACCOUNTS" in INDEX_HTML


def test_platform_marks_are_real_logos_not_letters():
    """Each platform shows its own mark, drawn inline (works offline)."""
    assert "PLATFORM_ICONS" in INDEX_HTML
    for platform in (
        "instagram",
        "facebook",
        "twitter",
        "whatsapp",
        "telegram",
        "discord",
        "reddit",
        "google",
        "snapchat",
        "linkedin",
        "slack",
    ):
        assert f"{platform}:{{" in INDEX_HTML.replace(" ", ""), f"no logo for {platform}"
    # Drawn inline as SVG — never fetched from a CDN. The only <img> in the UI
    # is the user's own archived media, served from the loopback /media/ route.
    assert 'viewBox="0 0 24 24"' in INDEX_HTML
    assert "src='http" not in INDEX_HTML
    assert 'src="http' not in INDEX_HTML
    assert "mediaUrl(" in INDEX_HTML, "images must go through the /media/ helper"


def test_shadcn_zinc_tokens():
    """The UI uses the flat shadcn zinc-dark system, not the old blue gradients."""
    lowered = INDEX_HTML.lower()
    assert "--bg:#09090b" in lowered  # zinc-950
    assert "--panel:#18181b" in lowered  # zinc-900
    assert "--line:#27272a" in lowered  # zinc-800
    # No leftover gradient-heavy brand chrome.
    assert "linear-gradient(180deg,#3b82f6" not in lowered
    assert "linear-gradient(160deg,#3b82f6" not in lowered


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
    # No external resources are loaded — the UI works fully offline. The only
    # remaining "http" strings are the SVG XML namespace (a constant identifier
    # in inline data: URLs, never fetched) and the loopback API base.
    offline = INDEX_HTML.replace("http://127.0.0.1", "").replace(
        "http://www.w3.org/2000/svg", ""
    )
    assert "http://" not in offline
    assert 'src="http' not in INDEX_HTML  # no external <script>/<img>
    assert "url(http" not in INDEX_HTML  # no external CSS url()
    assert "@import" not in INDEX_HTML
    assert "<link" not in INDEX_HTML
