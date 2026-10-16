# -*- coding: utf-8 -*-
"""A partition exists only after a successful, complete copy publication."""

import os
import shutil
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from agentscope.workspace._base import _EQUIP_PARTITION_SHIM


class SkillPartitionPublishTest(TestCase):
    """Exercise the exact script run by local and sandbox backends."""

    def setUp(self) -> None:
        """Create a seed with an auxiliary file that must survive copying."""
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.seed = self.root / "seed"
        self.seed.mkdir()
        (self.seed / "script.py").write_text("payload", "utf-8")
        self.partition = self.root / "session"
        self.enterContext(
            patch("sys.argv", ["-c", str(self.seed), str(self.partition)])
        )

    def assert_complete(self) -> None:
        """The final directory contains all files with no abandoned staging."""
        self.assertEqual(
            (self.partition / "script.py").read_text("utf-8"), "payload"
        )
        self.assertFalse(list(self.root.glob("session.equipping-*")))

    def test_retries_temporary_windows_lock(self) -> None:
        """A temporary rename lock must not look like an empty partition."""
        rename = os.rename
        attempts = 0

        def locked(source, destination):
            nonlocal attempts
            attempts += 1
            if attempts <= 2:
                error = PermissionError("Directory temporarily in use")
                error.winerror = 5
                raise error
            rename(source, destination)

        with patch("os.rename", new=locked):
            exec(_EQUIP_PARTITION_SHIM, {})
        self.assertGreaterEqual(attempts, 3)
        self.assert_complete()

    def test_persistent_denial_is_not_reported_as_equipped(self) -> None:
        """Real failure propagates, removes staging, and remains retryable."""
        for winerror, expected_attempts in ((5, 6), (None, 1)):
            with self.subTest(winerror=winerror):
                error = PermissionError("Persistent denial")
                error.winerror = winerror
                with (
                    patch("os.rename", side_effect=error) as rename,
                    patch("time.sleep"),
                    self.assertRaises(PermissionError),
                ):
                    exec(_EQUIP_PARTITION_SHIM, {})
                self.assertEqual(rename.call_count, expected_attempts)
                self.assertFalse(self.partition.exists())
                self.assertFalse(list(self.root.glob("session.equipping-*")))
        exec(_EQUIP_PARTITION_SHIM, {})
        self.assert_complete()

    def test_concurrent_publisher_is_accepted(self) -> None:
        """The losing copier may reuse an actually published directory."""

        def concurrent(source, destination):
            shutil.copytree(source, destination)
            raise FileExistsError(destination)

        with patch("os.rename", new=concurrent):
            exec(_EQUIP_PARTITION_SHIM, {})
        self.assert_complete()
