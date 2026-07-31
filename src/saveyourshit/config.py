"""Configuration model, persisted as TOML in the home directory.

Reading uses stdlib ``tomllib``. Writing uses a tiny hand-rolled serializer so we
carry no extra dependency for a format this small and this flat.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .paths import Layout


@dataclass
class ConnectorConfig:
    """Per-connector settings persisted in config."""

    enabled: bool = True
    # Rail C (session-scrape) connectors are risky; they must be explicitly
    # opted into, never on by default.
    allow_risky: bool = False
    schedule: str = "daily"  # daily | weekly | manual
    options: dict = field(default_factory=dict)


@dataclass
class Config:
    version: int = 1
    encrypt: bool = True
    cloud_remote: str | None = None  # rclone remote name, e.g. "b2:my-bucket"
    connectors: dict[str, ConnectorConfig] = field(default_factory=dict)

    # -- persistence ---------------------------------------------------------
    @classmethod
    def load(cls, layout: Layout) -> Config:
        if not layout.config_file.exists():
            return cls()
        data = tomllib.loads(layout.config_file.read_text(encoding="utf-8"))
        connectors = {
            name: ConnectorConfig(
                enabled=bool(c.get("enabled", True)),
                allow_risky=bool(c.get("allow_risky", False)),
                schedule=str(c.get("schedule", "daily")),
                options=dict(c.get("options", {})),
            )
            for name, c in data.get("connectors", {}).items()
        }
        return cls(
            version=int(data.get("version", 1)),
            encrypt=bool(data.get("encrypt", True)),
            cloud_remote=data.get("cloud_remote"),
            connectors=connectors,
        )

    def save(self, layout: Layout) -> None:
        layout.ensure()
        layout.config_file.write_text(self.to_toml(), encoding="utf-8")

    def to_toml(self) -> str:
        lines: list[str] = [
            "# Save Your Shit configuration",
            "# Everything is local. This file never leaves your machine.",
            f"version = {self.version}",
            f"encrypt = {_toml_bool(self.encrypt)}",
        ]
        if self.cloud_remote:
            lines.append(f'cloud_remote = "{_toml_escape(self.cloud_remote)}"')
        for name, c in sorted(self.connectors.items()):
            lines += [
                "",
                f"[connectors.{name}]",
                f"enabled = {_toml_bool(c.enabled)}",
                f"allow_risky = {_toml_bool(c.allow_risky)}",
                f'schedule = "{_toml_escape(c.schedule)}"',
            ]
            for k, v in sorted(c.options.items()):
                lines.append(f'options.{k} = "{_toml_escape(str(v))}"')
        return "\n".join(lines) + "\n"

    # -- convenience ---------------------------------------------------------
    def connector(self, name: str) -> ConnectorConfig:
        return self.connectors.setdefault(name, ConnectorConfig())


def _toml_bool(b: bool) -> str:
    return "true" if b else "false"


def _toml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def load_config(home: Path | None = None) -> tuple[Config, Layout]:
    layout = Layout(home)
    return Config.load(layout), layout
