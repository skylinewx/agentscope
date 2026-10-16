# -*- coding: utf-8 -*-
"""The installed-skill record."""

from datetime import datetime, timezone

from pydantic import Field

from ._base import _RecordBase


class SkillRecord(_RecordBase):
    """One skill a user has installed, at the user level rather than in
    any one workspace — the skill counterpart of :class:`MCPRecord`.

    The complete immutable payload is identified by ``artifact_id`` in
    the service Skill store. Packages are saved by explicit installs and
    updates; metadata and switch state stay in the configured storage.
    """

    user_id: str

    artifact_id: str | None = None
    """Whole-directory content hash in the service Skill artifact store."""

    name: str
    """Unique library name per user; workspace bindings use the record ID."""

    display_name: str | None = Field(default=None)
    """The card's user-facing name, snapshotted at install time."""

    description: str = Field(default="")
    """The card's description, snapshotted at install time."""

    tags: list[str] = Field(default_factory=list)
    """The card's tags, snapshotted at install time."""

    author: str | None = Field(default=None)
    """Who published the skill, snapshotted at install time."""

    icon_url: str | None = Field(default=None)
    """The card's icon, snapshotted at install time. Kept so the library
    looks like the hub listing it came from rather than losing its
    identity the moment it is installed."""

    url: str | None = Field(default=None)
    """The skill's page on the hub's website, snapshotted at install
    time, so the library can still link back to where it came from."""

    markdown: str = Field(default="")
    """The ``SKILL.md`` body, snapshotted at install time. Enough to show
    the user what the skill does without going back to the hub."""

    hub_id: str | None = Field(default=None)
    """The hub this was installed from, or ``None`` when added by hand."""

    card_id: str | None = Field(default=None)
    """The card's id on that hub. Paired with ``hub_id`` it answers
    "where did this come from" and "is there a newer version"."""

    version: str | None = Field(default=None)
    """An explicit version label or the saved package's short hash."""

    version_updated_at: datetime | None = Field(default=None)
    """When this library revision was saved, in UTC; unknown for old data."""

    def update_version(self, artifact_id: str, version: str) -> None:
        """Date a changed revision independently of metadata and switches."""
        if (self.artifact_id, self.version) != (artifact_id, version):
            self.version_updated_at = datetime.now(timezone.utc)
        self.artifact_id = artifact_id
        self.version = version
