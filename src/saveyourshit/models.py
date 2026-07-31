"""Canonical, platform-agnostic data model.

Every connector normalizes whatever it finds into :class:`NormalizedRecord`s and
:class:`MediaRef`s. The engine owns storage, hashing, and dedup, so connectors
stay dumb: they parse and yield, nothing more.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class RecordType(StrEnum):
    """The kinds of thing we back up, across all platforms."""

    MESSAGE = "message"
    POST = "post"
    COMMENT = "comment"
    LIKE = "like"
    FOLLOWER = "follower"
    FOLLOWING = "following"
    PROFILE = "profile"
    MEDIA = "media"
    SAVED = "saved"
    STORY = "story"
    OTHER = "other"


@dataclass(slots=True)
class MediaRef:
    """A pointer to a media file a connector wants stored.

    Exactly one of ``source_path`` (a file inside an unpacked export) or
    ``source_url`` (a remote link, possibly time-limited) should be set. The
    engine resolves it into a content-addressed blob and fills ``sha256``.
    """

    owner_uid: str
    kind: str = "file"  # image | video | audio | file
    source_path: str | None = None
    source_url: str | None = None
    filename: str | None = None
    sha256: str | None = None


@dataclass(slots=True)
class NormalizedRecord:
    """One canonical item: a message, a post, a follower, etc.

    ``uid`` must be stable and unique *within a connector* — it is the dedup key,
    so re-ingesting the same export twice never creates duplicates.
    """

    connector: str
    type: RecordType
    uid: str
    created_at: str | None = None  # ISO-8601, UTC where known
    text: str | None = None
    author: str | None = None
    thread: str | None = None  # conversation / thread id, for grouping DMs
    media: list[str] = field(default_factory=list)  # sha256 blob refs
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.type, str):
            self.type = RecordType(self.type)
        if not self.uid:
            raise ValueError("NormalizedRecord.uid must be non-empty")
        if not self.connector:
            raise ValueError("NormalizedRecord.connector must be non-empty")

    @property
    def global_uid(self) -> str:
        """Globally-unique id across connectors: ``connector:uid``."""
        return f"{self.connector}:{self.uid}"


@dataclass(slots=True)
class Batch:
    """A unit of work yielded by a connector: records + media to store."""

    records: list[NormalizedRecord] = field(default_factory=list)
    media: list[MediaRef] = field(default_factory=list)
    cursor: dict | None = None
