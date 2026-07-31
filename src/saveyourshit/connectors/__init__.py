"""Connector registry. Importing this package registers every built-in connector."""

from . import (  # noqa: F401  (register on import)
    discord,
    facebook,
    instagram,
    reddit,
    telegram,
    twitter,
    whatsapp,
)
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
