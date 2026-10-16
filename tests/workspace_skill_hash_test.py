# -*- coding: utf-8 -*-
"""Regression tests for skill directory content deduplication."""

import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.async_case import IsolatedAsyncioTestCase

from agentscope.workspace import LocalWorkspace

_SKILL_MD = b"---\nname: report\ndescription: A report skill\n---\nReport.\n"


class SkillDirectoryHashTest(IsolatedAsyncioTestCase):
    """Deduplication follows the complete installed skill directory."""

    async def asyncSetUp(self) -> None:
        """Create source directories and an initialized workspace."""
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.workspace = LocalWorkspace(workdir=str(self.root / "workspace"))
        await self.workspace.initialize()

    async def asyncTearDown(self) -> None:
        """Close the workspace before deleting its temporary files."""
        await self.workspace.close()

    def _make_skill(
        self,
        directory: str,
        files: dict[str, bytes] | None = None,
    ) -> Path:
        """Write a skill with fixed Markdown and arbitrary extra files."""
        source = self.root / "sources" / directory
        source.mkdir(parents=True)
        (source / "SKILL.md").write_bytes(_SKILL_MD)
        for name, content in (files or {}).items():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return source

    async def test_auxiliary_file_changes_are_not_deduplicated(self) -> None:
        """Script, reference and binary changes preserve distinct packages."""
        for index, name in enumerate(
            ["scripts/run.py", "references/guide.md", "assets/sample.bin"],
        ):
            with self.subTest(file=name):
                source = self._make_skill(str(index), {name: b"old\x00"})
                agent_id = f"agent-{index}"
                await self.workspace.add_skill(str(source), agent_id=agent_id)
                (source / name).write_bytes(b"new\x00")
                await self.workspace.add_skill(str(source), agent_id=agent_id)

                skills = await self.workspace.list_skills(agent_id=agent_id)
                self.assertEqual(len(skills), 2)
                self.assertEqual(
                    {
                        (Path(skill.dir) / name).read_bytes()
                        for skill in skills
                    },
                    {b"old\x00", b"new\x00"},
                )

    async def test_identical_trees_ignore_location_order_and_mtime(
        self,
    ) -> None:
        """The same files dedup across root paths and filesystem metadata."""
        files = {"scripts/run.py": b"run", "references/guide.md": b"guide"}
        first = self._make_skill("first", files)
        second = self._make_skill("second", dict(reversed(files.items())))
        (first / "empty").mkdir()
        (second / "empty").mkdir()
        for path in second.rglob("*"):
            os.utime(path, (1000000000, 1000000000))

        await self.workspace.add_skill(str(first))
        await self.workspace.add_skill(str(second))
        self.assertEqual(len(await self.workspace.list_skills()), 1)

    async def test_file_paths_and_empty_entries_affect_deduplication(
        self,
    ) -> None:
        """Renames, deletions and empty entries change package identity."""
        source = self._make_skill("paths", {"run.py": b"run"})
        await self.workspace.add_skill(str(source))
        (source / "run.py").rename(source / "renamed.py")
        await self.workspace.add_skill(str(source))
        (source / "renamed.py").unlink()
        await self.workspace.add_skill(str(source))
        (source / "empty.txt").touch()
        await self.workspace.add_skill(str(source))
        (source / "empty-dir").mkdir()
        await self.workspace.add_skill(str(source))
        self.assertEqual(len(await self.workspace.list_skills()), 5)

    async def test_reinitialization_recognizes_script_changes(self) -> None:
        """Changing only a seed script is recognized after a restart."""
        source = self._make_skill("seed", {"scripts/run.py": b"old"})
        workdir = str(self.root / "seed-workspace")
        seeded = LocalWorkspace(workdir=workdir, skill_paths=[str(source)])
        await seeded.initialize()
        await seeded.close()
        (source / "scripts/run.py").write_bytes(b"new")

        seeded = LocalWorkspace(workdir=workdir, skill_paths=[str(source)])
        await seeded.initialize()
        try:
            skills = await seeded.list_skills(agent_id="new-agent")
            self.assertEqual(len(skills), 2)
            self.assertEqual(
                {
                    (Path(skill.dir) / "scripts/run.py").read_bytes()
                    for skill in skills
                },
                {b"old", b"new"},
            )
        finally:
            await seeded.close()

    async def test_import_compares_current_installed_contents(self) -> None:
        """An edited installed script dedups against its matching package."""
        source = self._make_skill("edited", {"scripts/run.py": b"old"})
        await self.workspace.add_skill(str(source))
        installed = (await self.workspace.list_skills())[0]
        (Path(installed.dir) / "scripts/run.py").write_bytes(b"new")
        (source / "scripts/run.py").write_bytes(b"new")

        await self.workspace.add_skill(str(source))
        self.assertEqual(len(await self.workspace.list_skills()), 1)

    async def test_manual_addition_compares_current_installed_contents(
        self,
    ) -> None:
        """Directory reconciliation recognizes duplicate edited packages."""
        source = self._make_skill("manual", {"scripts/run.py": b"old"})
        await self.workspace.add_skill(str(source))
        installed = Path((await self.workspace.list_skills())[0].dir)
        (installed / "scripts/run.py").write_bytes(b"new")
        partition = installed.parent
        shutil.copytree(installed, partition / "manual-copy")
        previous = json.loads((partition / ".index").read_text("utf-8"))
        # Ensure reconciliation runs even on filesystems with coarse mtimes.
        os.utime(
            partition,
            (previous["skills_dir_mtime"] + 2,) * 2,
        )

        self.assertEqual(len(await self.workspace.list_skills()), 1)
