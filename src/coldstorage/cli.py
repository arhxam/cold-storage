"""``cold`` — the command-line interface. Designed to be dead simple:

    cold init            # one-time setup (folder, passphrase, recovery kit)
    cold ingest <path>   # back up a downloaded export (auto-detects platform)
    cold status          # where's my data, what's backed up, what's stale
    cold search <query>  # full-text search across everything
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import DISPLAY_NAME, __version__, connectors
from .config import Config
from .crypto import KeyManager
from .crypto.keys import KeyError_, RecoveryKit
from .engine import Engine
from .paths import Layout
from .runtime import LockedError, load_runtime
from .status import compute_status, human_bytes

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help=f"{DISPLAY_NAME} — local-first backup for your own social-media data. No server.",
)
console = Console()
err = Console(stderr=True)


#: Machine-readable marker on the "I can't read this export" failure. The app
#: keys off it to stop retrying — re-reading the same unreadable file cannot
#: start working, so it is a permanent failure, not a transient one.
UNRECOGNIZED_MARKER = "COLD_UNRECOGNIZED_EXPORT"


def _fail_unrecognized(path: Path, exc: Exception) -> None:
    """Explain an unrecognized export the way `cold check` does, then exit."""
    from .preflight import unrecognized_reasons

    err.print(f"[red]✗[/] Could not read this export: [bold]{path.name}[/]")
    for reason in unrecognized_reasons(path):
        err.print(f"  • {reason}")
    err.print(f"\n[dim]Run `cold check {path}` for a full report.[/]")
    err.print(f"[dim]{UNRECOGNIZED_MARKER}: {exc}[/]")
    raise typer.Exit(2)


def _fail(msg: str, code: int = 1) -> None:
    err.print(f"[bold red]error:[/] {msg}")
    raise typer.Exit(code)


@app.command()
def version() -> None:
    """Print the version."""
    console.print(f"{DISPLAY_NAME} {__version__}")


@app.command()
def init(
    passphrase: str | None = typer.Option(
        None, help="Encryption passphrase (prompted if omitted)."
    ),
    no_encrypt: bool = typer.Option(False, "--no-encrypt", help="Store data unencrypted."),
    home: Path | None = typer.Option(None, help="Backup folder (default ~/ColdStorage)."),
) -> None:
    """One-time setup: choose a folder, set a passphrase, save your Recovery Kit."""
    layout = Layout(home)
    if layout.exists():
        _fail(f"already initialized at {layout.home} (delete it to start over)")
    layout.ensure()

    config = Config(encrypt=not no_encrypt)

    if config.encrypt:
        if not passphrase:
            if not sys.stdin.isatty():
                _fail("no passphrase given and not a TTY; pass --passphrase or --no-encrypt")
            import getpass

            p1 = getpass.getpass("Choose a passphrase: ")
            p2 = getpass.getpass("Repeat passphrase: ")
            if p1 != p2:
                _fail("passphrases did not match")
            passphrase = p1
        km = KeyManager(layout.keys_dir)
        cipher, kit = km.create(passphrase)
        cached = km.cache_in_keychain(cipher)
        _print_recovery_kit(kit, cached)

    config.save(layout)

    console.print()
    console.print(
        Panel.fit(
            f"[bold green]You're set up.[/]\n\n"
            f"Your data lives in:\n  [bold]{layout.home}[/]\n\n"
            f"Next: download an export from any platform and run\n"
            f"  [bold]cold ingest ~/Downloads/your-export.zip[/]",
            title="Cold Storage",
        )
    )


def _print_recovery_kit(kit: RecoveryKit, cached: bool) -> None:
    console.print()
    console.print(
        Panel(
            f"[bold]{kit.code}[/]\n\n"
            "[yellow]Write this down and store it somewhere safe (not on this computer).[/]\n"
            "It is the ONLY way to recover your backups if you forget your passphrase\n"
            "and lose this machine. There is no reset link — that is the point.",
            title="🔑  RECOVERY KIT — SAVE THIS NOW",
            border_style="yellow",
        )
    )
    if cached:
        console.print(
            "[dim]Your key is cached in the OS keychain, so scheduled backups won't prompt.[/]"
        )
    if sys.stdin.isatty():
        typer.confirm("Have you saved your Recovery Kit?", abort=True)


@app.command()
def ingest(
    path: Path = typer.Argument(..., help="An export .zip/folder, or a folder to scan with --all."),
    connector: str | None = typer.Option(
        None, "--connector", "-c", help="Force a connector instead of auto-detecting."
    ),
    scan_all: bool = typer.Option(
        False, "--all", help="Scan the folder and ingest every export inside (e.g. ~/Downloads)."
    ),
    no_snapshot: bool = typer.Option(
        False, "--no-snapshot", help="Don't keep a second raw copy of the export (saves disk)."
    ),
    passphrase: str | None = typer.Option(None, help="Passphrase (if not cached)."),
) -> None:
    """Back up a downloaded data export. Auto-detects the platform.

    With --all, point it at a folder (like ~/Downloads) and it ingests every
    recognizable export it finds — the simplest way to keep everything current.
    """
    if not path.exists():
        _fail(f"no such file or folder: {path}")
    rt = _runtime(passphrase)
    _warn_low_disk(rt.layout.home, path, keep_snapshot=not no_snapshot)
    keep_snapshot = not no_snapshot

    if scan_all:
        with rt.open_archive() as archive, console.status(f"Scanning {path}…"):
            results = Engine(archive).ingest_folder(path, keep_snapshot=keep_snapshot)
        if not results:
            console.print(f"[yellow]No recognizable exports found in[/] {path}")
            return
        total = sum(r.added for r in results)
        for r in results:
            flag = " [yellow](partial)[/]" if r.status == "error" else ""
            console.print(f"[green]✓[/] {r.connector}: [bold]{r.added}[/] new items{flag}")
        console.print(f"\n[bold]{total}[/] new items across {len(results)} export(s).")
        return

    with rt.open_archive() as archive, console.status(f"Ingesting {path.name}…"):
        try:
            result = Engine(archive).ingest(
                path, connector_id=connector, keep_snapshot=keep_snapshot
            )
        except KeyError:
            # A typo in --connector is a user error, not a crash.
            known = ", ".join(sorted(c.id for c in connectors.all_connectors()))
            _fail(f"unknown platform {connector!r}. Choose one of: {known}")
        except ValueError as exc:
            # "I don't recognize this" is a normal thing for a user to hit — the
            # usual cause is an HTML export instead of JSON. Say what to do
            # about it instead of printing a traceback.
            _fail_unrecognized(path, exc)
    if result.status == "error":
        # Say it plainly and exit non-zero: a green tick on a partial (or empty)
        # import is how someone ends up believing they have a backup they do not.
        if result.added == 0:
            err.print(
                f"[yellow]⚠[/] Nothing could be read from this "
                f"[bold]{result.connector}[/] export — it was not backed up."
            )
        else:
            err.print(
                f"[yellow]⚠[/] Backed up [bold]{result.added}[/] items from "
                f"[bold]{result.connector}[/], but part of this export could not be read."
            )
        err.print(f"  {result.error}")
        err.print("[dim]  Re-download the export (choose JSON, not HTML) and retry.[/]")
        if result.snapshot:
            err.print(f"[dim]  Raw export kept at: {result.snapshot}[/]")
        raise typer.Exit(3)
    console.print(
        f"[green]✓[/] Backed up [bold]{result.added}[/] new items from "
        f"[bold]{result.connector}[/] ({result.batches} batches)."
    )
    if result.snapshot:
        console.print(f"  Raw export kept at: [dim]{result.snapshot}[/]")


@app.command()
def check(
    path: Path = typer.Argument(..., help="An export .zip or folder to inspect."),
    connector: str | None = typer.Option(None, "--connector", "-c", help="Force a connector."),
) -> None:
    """Dry-run: inspect an export and report what would be backed up. Writes nothing.

    Run this on your real export BEFORE `ingest` to confirm the parser matches your
    data — it prints how many messages, followers, posts, and media it recognized.
    """
    from .preflight import check_export

    if not path.exists():
        _fail(f"no such file or folder: {path}")
    with console.status(f"Inspecting {path.name}…"):
        result = check_export(path, connector_id=connector)

    if result.connector is None:
        err.print("[red]Not recognized as any supported export.[/]")
        for warn in result.warnings:
            err.print(f"  [yellow]•[/] {warn}")
        raise typer.Exit(1)

    console.print(f"[green]✓[/] Detected: [bold]{result.connector}[/]")
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(justify="right", style="bold")
    table.add_column()
    for type_, n in sorted(result.counts.items(), key=lambda kv: -kv[1]):
        table.add_row(f"{n:,}", type_)
    if result.threads:
        table.add_row(f"{result.threads:,}", "conversations")
    if result.media_refs:
        table.add_row(f"{result.media_refs:,}", "media referenced")
    console.print(table)
    for warn in result.warnings:
        err.print(f"  [yellow]⚠ {warn}[/]")
    if not result.warnings:
        console.print("[dim]Looks good. Run `cold ingest` to back it up.[/]")


@app.command()
def status(
    passphrase: str | None = typer.Option(None, help="Passphrase (if not cached)."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable JSON output."),
) -> None:
    """Show what's backed up, where it lives, and what's gone stale."""
    rt = _runtime(passphrase)
    with rt.open_archive() as archive:
        st = compute_status(archive, rt.config)

    if json_out:
        import json
        from dataclasses import asdict

        typer.echo(json.dumps(asdict(st), indent=2))
        return

    console.print(
        Panel.fit(
            f"[bold]{st.home}[/]\n"
            f"{'🔒 media encrypted' if st.encrypted else '⚠ not encrypted'} · "
            f"{st.total_records:,} items · {st.blob_count:,} media files · "
            f"{human_bytes(st.total_bytes)}",
            title="Your archive",
        )
    )
    table = Table(show_header=True, header_style="bold")
    table.add_column("Platform")
    table.add_column("Items", justify="right")
    table.add_column("Last backup")
    table.add_column("State")
    for c in st.connectors:
        if c.last_status is None:
            state = "[dim]never run[/]"
        elif c.last_status == "error":
            state = "[red]last run errored[/]"
        elif c.stale:
            state = "[yellow]stale[/]"
        else:
            state = "[green]ok[/]"
        table.add_row(
            c.connector,
            f"{c.records:,}",
            (c.last_run_at or "—")[:19].replace("T", " "),
            state,
        )
    if st.connectors:
        console.print(table)
    else:
        console.print("[dim]Nothing backed up yet. Try `cold ingest <export>`.[/]")
    if st.any_stale:
        console.print(
            "\n[yellow]⚠ Some backups are stale (>7 days).[/] "
            "Download a fresh export and run `cold ingest`."
        )


@app.command()
def search(
    query: str = typer.Argument(..., help="Full-text query."),
    connector: str | None = typer.Option(None, "--connector", "-c"),
    limit: int = typer.Option(20, help="Max results."),
    passphrase: str | None = typer.Option(None, help="Passphrase (if not cached)."),
) -> None:
    """Search your backed-up chats, posts, and captions."""
    rt = _runtime(passphrase)
    with rt.open_archive() as archive:
        hits = archive.index.search(query, connector=connector, limit=limit)
    if not hits:
        console.print("[dim]No matches.[/]")
        return
    for h in hits:
        when = (h.get("created_at") or "")[:19].replace("T", " ")
        who = h.get("author") or ""
        console.print(
            f"[cyan]{h['connector']}[/] [dim]{when}[/] "
            f"[bold]{who}[/] {(h.get('text') or '').strip()[:200]}"
        )


@app.command()
def view(
    output: Path | None = typer.Option(None, "--output", "-o", help="Where to write the HTML."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open it when done."),
    passphrase: str | None = typer.Option(None, help="Passphrase (if not cached)."),
) -> None:
    """Build a self-contained offline HTML viewer of your archive."""
    from .viewer import build_viewer

    rt = _runtime(passphrase)
    out = output or (rt.layout.home / "viewer.html")
    with rt.open_archive() as archive:
        path = build_viewer(archive, out)
    console.print(f"[green]✓[/] Viewer written to [bold]{path}[/]")
    if open_browser:
        import webbrowser

        webbrowser.open(path.as_uri())


@app.command()
def serve(
    port: int = typer.Option(8787, help="Local port."),
    host: str = typer.Option("127.0.0.1", help="Bind address (loopback by default)."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open your browser."),
    passphrase: str | None = typer.Option(None, help="Passphrase (if not cached)."),
) -> None:
    """Launch the local web app (dashboard + data viewer) in your browser."""
    from .webapp import serve as serve_app

    rt = _runtime(passphrase)
    url = f"http://{host}:{port}/"
    console.print(f"[green]✓[/] Cold Storage is running at [bold]{url}[/]")
    console.print("[dim]Loopback only — nothing leaves your machine. Ctrl-C to stop.[/]")
    with rt.open_archive() as archive:
        serve_app(archive, rt.config, host=host, port=port, open_browser=open_browser)


@app.command(name="connectors")
def connectors_cmd() -> None:
    """List the platforms this can back up."""
    table = Table(show_header=True, header_style="bold")
    table.add_column("id")
    table.add_column("Platform")
    table.add_column("Rail")
    table.add_column("Provides")
    table.add_column("Risk")
    for c in sorted(connectors.all_connectors(), key=lambda c: c.id):
        table.add_row(
            c.id,
            c.display_name,
            c.rail,
            ", ".join(c.provides),
            "[red]ban risk[/]" if c.risky else "[green]safe[/]",
        )
    console.print(table)


@app.command()
def where(connector: str | None = typer.Argument(None)) -> None:
    """Print exactly where your data is stored on disk."""
    layout = Layout()
    if not layout.exists():
        _fail("not initialized — run `cold init` first")
    # plain output (no Rich wrapping) so paths stay copy-pasteable
    if connector:
        typer.echo(str(layout.connector_dir(connector)))
    else:
        typer.echo(f"Home:   {layout.home}")
        typer.echo(f"Index:  {layout.index_db}")
        typer.echo(f"Media:  {layout.blobs_dir}")
        typer.echo(f"Config: {layout.config_file}")
        typer.echo(f"Keys:   {layout.keys_dir}")


@app.command()
def passphrase(
    old: str | None = typer.Option(None, help="Current passphrase."),
    new: str | None = typer.Option(None, help="New passphrase."),
) -> None:
    """Change your encryption passphrase."""
    layout = Layout()
    km = KeyManager(layout.keys_dir)
    if not km.exists():
        _fail("not initialized or not encrypted")
    import getpass

    old = old or getpass.getpass("Current passphrase: ")
    new = new or getpass.getpass("New passphrase: ")
    try:
        km.change_passphrase(old, new)
    except KeyError_ as e:
        _fail(str(e))
    console.print("[green]✓[/] Passphrase changed.")


@app.command()
def recover(
    code: str = typer.Argument(..., help="Your Recovery Kit code."),
    new_passphrase: str | None = typer.Option(None, help="New passphrase to set."),
) -> None:
    """Reset your passphrase using your Recovery Kit."""
    layout = Layout()
    km = KeyManager(layout.keys_dir)
    if not km.exists():
        _fail("not initialized")
    import getpass

    new_passphrase = new_passphrase or getpass.getpass("New passphrase: ")
    try:
        cipher = km.reset_passphrase_with_recovery(RecoveryKit(code), new_passphrase)
        km.cache_in_keychain(cipher)
    except KeyError_ as e:
        _fail(str(e))
    except Exception:
        # This is the most stressful moment this tool has: the user has lost
        # their passphrase and is typing a code off paper. A Python traceback
        # here is unforgivable — and base32 has no 0, 1 or 8, which is exactly
        # what people substitute for O, I and B.
        _fail(
            "that recovery code could not be read.\n"
            "  Check for typos — the code never contains the digits 0, 1 or 8,\n"
            "  so those are usually the letters O, I and B."
        )
    console.print("[green]✓[/] Recovered. New passphrase set.")


@app.command()
def sync(
    remote: str | None = typer.Option(
        None, help="rclone remote to mirror to, e.g. b2:my-bucket/cold (default: config)."
    ),
    repo: Path | None = typer.Option(None, help="restic repo location (default: alongside home)."),
    init_repo: bool = typer.Option(False, "--init", help="Initialize the restic repo first."),
    passphrase: str | None = typer.Option(None, help="Passphrase (if not cached)."),
) -> None:
    """Back up your archive to an encrypted restic repo and optionally your own cloud.

    Requires the optional `restic` (and `rclone` for cloud) binaries. Everything
    stays encrypted end to end; your cloud provider only ever sees ciphertext.
    """
    from .sync import RcloneRemote, ResticRepo, SyncToolMissing

    rt = _runtime(passphrase)
    repo_loc = repo or (rt.layout.home.parent / f"{rt.layout.home.name}-restic")
    restic = ResticRepo(repo_loc)
    if not restic.is_available():
        _fail(
            "restic isn't installed (it's optional). Install it from https://restic.net "
            "to enable versioned encrypted backups."
        )
    if rt.cipher is not None:
        restic_pw = rt.cipher.derived_secret(b"restic")
    elif passphrase:
        restic_pw = passphrase
    else:
        _fail("need a passphrase to derive the restic key (pass --passphrase or enable encryption)")

    try:
        if init_repo:
            with console.status("Initializing restic repo…"):
                restic.init(restic_pw)
        with console.status("Snapshotting your archive…"):
            res = restic.snapshot([rt.layout.home], restic_pw, tags=["cold"])
        if not res.ok:
            _fail(f"restic backup failed: {res.stderr.strip()[:400]}")
        console.print("[green]✓[/] Local encrypted snapshot done.")

        remote = remote or rt.config.cloud_remote
        if remote:
            rclone = RcloneRemote()
            if not rclone.is_available():
                _fail(
                    "rclone isn't installed (optional). "
                    "Install from https://rclone.org for cloud sync."
                )
            with console.status(f"Syncing to {remote}…"):
                rres = rclone.copy(repo_loc, remote)
            if not rres.ok:
                _fail(f"cloud sync failed: {rres.stderr.strip()[:400]}")
            console.print(f"[green]✓[/] Mirrored to [bold]{remote}[/] (ciphertext only).")
        else:
            console.print(
                "[dim]No cloud remote set. Configure one and re-run to mirror offsite.[/]"
            )
    except SyncToolMissing as e:
        _fail(str(e))


@app.command()
def schedule(
    every: str = typer.Option("daily", help="daily | weekly."),
    remove: bool = typer.Option(False, "--remove", help="Remove the scheduled reminder."),
) -> None:
    """Set up a periodic reminder that checks whether your backups have gone stale.

    (Phase 1 exports are user-triggered, so this schedules a staleness check — not
    an automatic fetch. Automatic fetching arrives with the live connectors.)
    """
    import os
    import shutil
    import sys

    from . import scheduler

    label = "com.coldstorage.reminder"
    syt_path = shutil.which("cold") or f"{sys.executable} -m coldstorage"
    plist_dir = Path(os.environ["COLD_LAUNCHAGENTS_DIR"]) if os.environ.get(
        "COLD_LAUNCHAGENTS_DIR"
    ) else None

    if remove:
        if sys.platform == "darwin":
            removed = scheduler.uninstall_macos(label, plist_dir=plist_dir)
            console.print("[green]✓[/] Removed." if removed else "[dim]Nothing to remove.[/]")
        else:
            console.print("Remove the cron/Task Scheduler entry you added for `cold status`.")
        return

    if every not in ("daily", "weekly"):
        _fail("--every must be 'daily' or 'weekly'")
    interval = 86400 if every == "daily" else 604800

    if sys.platform == "darwin":
        path = scheduler.install_macos(
            label, [*syt_path.split(), "status"], interval, plist_dir=plist_dir
        )
        console.print(f"[green]✓[/] Installed launchd reminder at [dim]{path}[/]")
        console.print(f"  Activate it now with:  [bold]launchctl load {path}[/]")
    elif sys.platform.startswith("linux"):
        console.print("Add this to your crontab (`crontab -e`):")
        console.print(f"  [bold]{scheduler.cron_line(every, f'{syt_path} status')}[/]")
    else:
        args = scheduler.schtasks_command(label, f"{syt_path} status", every)
        console.print("Run this to schedule (Windows):")
        console.print(f"  [bold]{' '.join(args)}[/]")


@app.command()
def doctor() -> None:
    """Check your setup and optional tools."""
    import shutil

    layout = Layout()
    checks: list[tuple[str, bool, str]] = []
    checks.append(("initialized", layout.exists(), str(layout.home)))
    if layout.exists():
        cfg = Config.load(layout)
        checks.append(
            (
                "media encryption",
                cfg.encrypt,
                # Precise on purpose: the index and manifests are not encrypted,
                # and a tool people pick for privacy must not imply otherwise.
                "AES-256 (photos/video; index is not encrypted)" if cfg.encrypt else "OFF",
            )
        )
        checks.append(
            ("keyfile present", KeyManager(layout.keys_dir).exists(), str(layout.keys_dir))
        )
    checks.append(
        ("restic (versioned backup)", shutil.which("restic") is not None, "optional")
    )
    checks.append(("rclone (cloud sync)", shutil.which("rclone") is not None, "optional"))
    try:
        import keyring  # noqa: F401

        checks.append(("OS keychain library", True, "installed"))
    except ImportError:
        checks.append(("OS keychain library", False, "pip install coldstorage[keyring]"))

    for name, ok, note in checks:
        mark = "[green]✓[/]" if ok else "[yellow]○[/]"
        console.print(f"{mark} {name} [dim]{note}[/]")


def _dir_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def _warn_low_disk(home: Path, source: Path, *, keep_snapshot: bool) -> None:
    """Warn (and, on a TTY, confirm) before an ingest that might exhaust the disk."""
    import shutil

    try:
        free = shutil.disk_usage(home if home.exists() else home.parent).free
    except OSError:
        return
    src = _dir_size(source)
    # blob store ≈ source size; snapshot adds ~another copy. Add a small buffer.
    need = src * (2 if keep_snapshot else 1) + 50 * 1024 * 1024
    if free >= need:
        return
    gb = 1024**3
    err.print(
        f"[yellow]⚠ Low disk:[/] this export is ~{src/gb:.1f} GB and needs about "
        f"{need/gb:.1f} GB free, but only {free/gb:.1f} GB is available."
    )
    if keep_snapshot:
        err.print("  Tip: re-run with [bold]--no-snapshot[/] to skip the second raw copy,")
        err.print("  or free up disk space first. Your original export is never modified.")
    if sys.stdin.isatty():
        typer.confirm("Continue anyway?", abort=True)


def _runtime(passphrase: str | None):
    try:
        return load_runtime(passphrase=passphrase)
    except LockedError as e:
        _fail(str(e))
    except KeyError_ as e:
        _fail(str(e))


if __name__ == "__main__":
    app()
