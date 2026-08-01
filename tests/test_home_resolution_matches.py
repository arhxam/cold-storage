"""The app and the engine must agree on where the archive is.

They are two implementations of the same rule — one in Python, one in the
Electron shell — and when they drifted apart the app looked in ~/ColdStorage,
found nothing, and ran `cold init`; the engine resolved the user's real
pre-rename archive and refused with "already initialized". The user saw
"Setup failed" on a perfectly good install.

These run the real resolver from each side against the same fake home.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAIN_JS = ROOT / "app" / "main.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _js_home(home: Path, env: dict[str, str] | None = None) -> str:
    """Run main.js's coldHome() in isolation, with os.homedir() stubbed."""
    src = MAIN_JS.read_text()
    body = src[src.index("function coldHome()") :]
    body = body[: body.index("\n}\n") + 3]
    script = (
        "const path=require('path'),fs=require('fs');\n"
        f"const os={{homedir:()=>{json.dumps(str(home))}}};\n"
        + body
        + "\nconsole.log(coldHome());"
    )
    out = subprocess.run(
        [shutil.which("node"), "-e", script],
        capture_output=True,
        text=True,
        env={**_base_env(), **(env or {})},
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _base_env() -> dict[str, str]:
    """A clean env, minus anything that would preempt the resolution we test."""
    e = {k: v for k, v in os.environ.items() if k not in ("COLD_HOME", "SYT_HOME")}
    return e


def _py_home(home: Path, env: dict[str, str] | None = None) -> str:
    code = (
        "import os\n"
        "from pathlib import Path\n"
        "from coldstorage import paths\n"
        f"paths.DEFAULT_HOME = Path({str(home)!r}) / 'ColdStorage'\n"
        f"paths.LEGACY_HOME = Path({str(home)!r}) / 'SaveYourShit'\n"
        "print(paths.get_home())\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=ROOT / "src",
        env={**_base_env(), **(env or {})},
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _both(tmp_path, env=None) -> tuple[str, str]:
    return _js_home(tmp_path, env), _py_home(tmp_path, env)


def test_fresh_install_agrees(tmp_path):
    js, py = _both(tmp_path)
    assert Path(js) == Path(py) == tmp_path / "ColdStorage"


def test_existing_pre_rename_archive_agrees(tmp_path):
    """The exact case that produced "Setup failed" on a working install."""
    legacy = tmp_path / "SaveYourShit"
    legacy.mkdir()
    (legacy / "config.toml").write_text("encrypt = true")
    js, py = _both(tmp_path)
    assert Path(js) == legacy, "the app must use the existing archive"
    assert Path(py) == legacy
    assert Path(js) == Path(py)


def test_new_archive_wins_when_both_exist(tmp_path):
    legacy = tmp_path / "SaveYourShit"
    legacy.mkdir()
    (legacy / "config.toml").write_text("encrypt = true")
    (tmp_path / "ColdStorage").mkdir()
    js, py = _both(tmp_path)
    assert Path(js) == Path(py) == tmp_path / "ColdStorage"


def test_a_bare_legacy_folder_is_not_mistaken_for_an_archive(tmp_path):
    """An empty ~/SaveYourShit left behind after a cleanup is not an archive."""
    (tmp_path / "SaveYourShit").mkdir()
    js, py = _both(tmp_path)
    assert Path(js) == Path(py) == tmp_path / "ColdStorage"


@pytest.mark.parametrize("var", ["COLD_HOME", "SYT_HOME"])
def test_env_override_agrees(tmp_path, var):
    target = tmp_path / "elsewhere"
    js, py = _both(tmp_path, {var: str(target)})
    assert Path(js) == Path(py) == target
