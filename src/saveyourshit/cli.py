"""``syt`` — the command-line interface. Designed to be dead simple:

    syt init            # one-time setup (folder, passphrase, recovery kit)
    syt ingest <path>   # back up a downloaded export (auto-detects platform)
    syt status          # where's my data, what's backed up, what's stale
    syt search <query>  # full-text search across everything
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
    home: Path | None = typer.Option(None, help="Backup folder (default ~/SaveYourShit)."),
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
            f"  [bold]syt ingest ~/Downloads/your-export.zip[/]",
            title="Save Your Shit",
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
    path: Path = typer.Argument(..., help="An export .zip or unpacked folder."),
    connector: str | None = typer.Option(
        None, "--connector", "-c", help="Force a connector instead of auto-detecting."
    ),
    passphrase: str | None = typer.Option(None, help="Passphrase (if not cached)."),
) -> None:
    """Back up a downloaded data export. Auto-detects the platform."""
    if not path.exists():
        _fail(f"no such file or folder: {path}")
    rt = _runtime(passphrase)
    with rt.open_archive() as archive, console.status(f"Ingesting {path.name}…"):
        result = Engine(archive).ingest(path, connector_id=connector)
    if result.status == "error":
        err.print(f"[yellow]partial ingest:[/] {result.error}")
    console.print(
        f"[green]✓[/] Backed up [bold]{result.added}[/] new items from "
        f"[bold]{result.connector}[/] ({result.batches} batches)."
    )
    if result.snapshot:
        console.print(f"  Raw export kept at: [dim]{result.snapshot}[/]")


@app.command()
def status(passphrase: str | None = typer.Option(None, help="Passphrase (if not cached).")) -> None:
    """Show what's backed up, where it lives, and what's gone stale."""
    rt = _runtime(passphrase)
    with rt.open_archive() as archive:
        st = compute_status(archive, rt.config)

    console.print(
        Panel.fit(
            f"[bold]{st.home}[/]\n"
            f"{'🔒 encrypted' if st.encrypted else '⚠ not encrypted'} · "
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
        console.print("[dim]Nothing backed up yet. Try `syt ingest <export>`.[/]")
    if st.any_stale:
        console.print(
            "\n[yellow]⚠ Some backups are stale (>7 days).[/] "
            "Download a fresh export and run `syt ingest`."
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
        _fail("not initialized — run `syt init` first")
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
    console.print("[green]✓[/] Recovered. New passphrase set.")


def _runtime(passphrase: str | None):
    try:
        return load_runtime(passphrase=passphrase)
    except LockedError as e:
        _fail(str(e))
    except KeyError_ as e:
        _fail(str(e))


if __name__ == "__main__":
    app()
