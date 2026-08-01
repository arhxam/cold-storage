"""PyInstaller entry point for the `cold` CLI.

Kept as a tiny script (instead of pointing PyInstaller at the package) so the
frozen binary behaves exactly like the installed console script.
"""

from coldstorage.cli import app

if __name__ == "__main__":
    app()
