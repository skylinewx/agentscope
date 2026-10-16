# -*- coding: utf-8 -*-
"""Local immutable Skill artifacts independent of hubs and knowledge bases."""

import asyncio
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Literal

import frontmatter
from yaml import YAMLError

from ...workspace import LocalWorkspace
from ...workspace._local_workspace import _hash_skill_directory

MAX_TEXT_FILE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class StoredSkill:
    """Metadata identifying a complete directory in the artifact store."""

    artifact_id: str
    name: str
    description: str
    markdown: str


class LocalSkillStore:
    """Content-addressed directories under a persistent service directory.

    Original packages are immutable. Agent copies may be edited freely.
    Staging validates packages before publishing their content-addressed
    directory; failed imports leave the previous version available.
    """

    def __init__(self, root_dir: str) -> None:
        self.root_dir = str(Path(root_dir).resolve())

    def path(self, artifact_id: str) -> str:
        """Resolve a stored content hash, rejecting arbitrary paths."""
        if not re.fullmatch(r"[0-9a-f]{64}", artifact_id):
            raise ValueError("Invalid Skill artifact ID.")
        path = os.path.join(self.root_dir, artifact_id)
        if not os.path.isfile(os.path.join(path, "SKILL.md")):
            raise FileNotFoundError("The saved Skill package is unavailable.")
        return path

    @staticmethod
    def _relative_file(root: Path, path: str) -> Path:
        """Resolve portable file paths strictly inside one saved package."""
        if (
            not path
            or any(char in path for char in ("\\", ":", "\0"))
            or any(part in ("", ".", "..") for part in path.split("/"))
        ):
            raise ValueError("Use a relative file path inside the Skill.")
        target = root.joinpath(*path.split("/"))
        if not target.resolve().is_relative_to(root.resolve()):
            raise ValueError("Skill file path is outside its package.")
        return target

    async def list_files(self, artifact_id: str) -> list[dict]:
        """List package files, marking bounded UTF-8 files as editable."""

        def listing() -> list[dict]:
            root = Path(self.path(artifact_id))
            result = []
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                editable = False
                size = path.stat().st_size
                if size <= MAX_TEXT_FILE_BYTES:
                    data = path.read_bytes()
                    try:
                        data.decode("utf-8")
                        editable = b"\0" not in data
                    except UnicodeDecodeError:
                        pass
                result.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "size": size,
                        "editable": editable,
                    }
                )
            return result

        return await asyncio.to_thread(listing)

    async def read_file(self, artifact_id: str, path: str) -> str:
        """Read only a bounded text file from the requested saved package."""

        def read() -> str:
            root = Path(self.path(artifact_id))
            target = self._relative_file(root, path)
            if not target.is_file():
                raise FileNotFoundError("Skill file is unavailable.")
            if target.stat().st_size > MAX_TEXT_FILE_BYTES:
                raise ValueError("This file exceeds the text editor limit.")
            data = target.read_bytes()
            if b"\0" in data:
                raise ValueError("This file is not UTF-8 text.")
            try:
                return data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("This file is not UTF-8 text.") from exc

        return await asyncio.to_thread(read)

    async def save_files(
        self, files: dict[str, str | None], artifact_id: str | None = None
    ) -> StoredSkill:
        """Publish a new complete package; retain unchanged binary files."""

        def save() -> StoredSkill:
            os.makedirs(self.root_dir, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=self.root_dir) as staging:
                root = Path(staging, "package")
                if artifact_id:
                    shutil.copytree(self.path(artifact_id), root)
                else:
                    root.mkdir()
                seen: set[str] = set()
                for path, content in files.items():
                    if path.casefold() in seen:
                        raise ValueError("Skill file paths must be unique.")
                    seen.add(path.casefold())
                    target = self._relative_file(root, path)
                    if content is None:
                        if target.is_file():
                            target.unlink()
                        continue
                    data = content.encode("utf-8")
                    if len(data) > MAX_TEXT_FILE_BYTES or b"\0" in data:
                        raise ValueError(
                            "Skill text files must be at most 1 MiB."
                        )
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                paths = [path for path in root.rglob("*") if path.is_file()]
                if (
                    len(paths) > 100
                    or sum(path.stat().st_size for path in paths)
                    > 500 * 1024 * 1024
                ):
                    raise ValueError(
                        "The Skill package exceeds upload limits."
                    )
                if not (root / "SKILL.md").is_file():
                    raise ValueError("A Skill package requires SKILL.md.")
                return self._capture_directory(str(root))

        return await asyncio.to_thread(save)

    async def capture_directory(self, path: str) -> StoredSkill:
        """Validate and copy all files, including scripts and references."""
        return await asyncio.to_thread(self._capture_directory, path)

    def _capture_directory(self, path: str) -> StoredSkill:
        os.makedirs(self.root_dir, exist_ok=True)
        # Check symlink cycles before copytree follows them recursively.
        _hash_skill_directory(path)
        # Copy first: metadata and the hash must describe the bytes actually
        # saved, even if the input directory is being edited.
        with tempfile.TemporaryDirectory(dir=self.root_dir) as staging:
            copied = os.path.join(staging, "package")
            shutil.copytree(path, copied)
            markdown = Path(copied, "SKILL.md").read_text(encoding="utf-8")
            try:
                content = frontmatter.loads(markdown)
            except YAMLError as exc:
                raise ValueError(
                    "Skill SKILL.md has invalid YAML metadata."
                ) from exc
            name, description = content.get("name"), content.get("description")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("Skill SKILL.md requires a nonempty name.")
            if not isinstance(description, str) or not description.strip():
                raise ValueError("Skill SKILL.md requires a description.")
            digest = _hash_skill_directory(copied)
            target = os.path.join(self.root_dir, digest)
            if not os.path.exists(target):
                for attempt in range(6):
                    try:
                        os.replace(copied, target)
                        break
                    except OSError as exc:
                        # A concurrent import may publish this digest first.
                        # Validate its complete payload below before reuse.
                        if os.path.isdir(target):
                            break
                        # Windows scanners may briefly hold new directories.
                        # Retry only sharing/access errors, with a bound.
                        if (
                            getattr(exc, "winerror", None) not in (5, 32, 33)
                            or attempt == 5
                        ):
                            raise
                        time.sleep(0.025 * 2**attempt)
            if _hash_skill_directory(target) != digest:
                raise ValueError(
                    "The saved Skill package failed integrity validation."
                )
            return StoredSkill(digest, name, description, markdown)

    async def capture_archive(
        self,
        stream: AsyncIterator[bytes],
        archive_format: Literal["zip", "tar", "tar.gz"],
        name: str,
    ) -> StoredSkill:
        """Use the workspace's safe archive importer, then save its payload."""
        os.makedirs(self.root_dir, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.root_dir) as staging:
            workspace = LocalWorkspace(workdir=staging)
            await workspace.initialize()
            try:
                try:
                    await workspace.add_skill_archive(
                        stream, archive_format, name
                    )
                except RuntimeError as exc:
                    raise ValueError(f"Invalid Skill archive: {exc}") from exc
                skills = await workspace.list_skills()
                if len(skills) != 1:
                    raise ValueError(
                        "A Skill package must contain exactly one Skill."
                    )
                return await self.capture_directory(skills[0].dir)
            finally:
                await workspace.close()
