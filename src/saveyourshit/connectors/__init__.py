"""Connector registry. Importing this package registers every built-in connector."""

from . import discord, facebook, instagram, twitter  # noqa: F401  (register on import)
from .base import (
    Connector,
    all_connectors,
    detect_connector,
    get,
    register,
)

__all__ = [
    "Connector",
    "all_connectors",
    "detect_connector",
    "get",
    "register",
]
