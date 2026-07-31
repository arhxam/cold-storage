"""PyInstaller entry point for the `syt` CLI.

Kept as a tiny script (instead of pointing PyInstaller at the package) so the
frozen binary behaves exactly like the installed console script.
"""

from saveyourshit.cli import app

if __name__ == "__main__":
    app()
