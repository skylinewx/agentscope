# -*- coding: utf-8 -*-
# unittest.enterContext owns temporary resources.
# pylint: disable=protected-access, consider-using-with
"""HTTP resource lifecycle through the app and Redis serializers."""

import io
import json
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import TestCase
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
from fastapi.testclient import TestClient

from agentscope.app import create_app
from agentscope.app.message_bus import RedisMessageBus
from agentscope.app.resources import LocalSkillStore
from agentscope.app.storage import RedisStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.mcp import HttpMCPConfig, MCPClient, StdioMCPConfig

HEADERS = {"X-User-ID": "alice"}


def _markdown(name: str) -> str:
    """Markdown fixture."""
    return (
        f"---\nname: {name}\ndescription: Resource test.\n---\n"
        "Use scripts/run.py.\n"
    )


def _archive(name: str, script: str) -> bytes:
    """Archive fixture."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("SKILL.md", _markdown(name))
        package.writestr("scripts/run.py", script)
    return buffer.getvalue()


class ResourceRouterTest(TestCase):
    """Persistence, enable/disable, owner isolation and explicit upgrades."""

    def setUp(self) -> None:
        """Create user upload fixtures and persistent test backends."""
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.skill_path = self.root / "bundle"
        (self.skill_path / "scripts").mkdir(parents=True)
        (self.skill_path / "SKILL.md").write_text(
            _markdown("analysis"), encoding="utf-8"
        )
        (self.skill_path / "scripts/run.py").write_text(
            "version one", encoding="utf-8"
        )
        self.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        self.seed_resources = True
        self.default_mcp = MCPClient(
            name="browser",
            is_stateful=True,
            mcp_config=HttpMCPConfig(
                url="https://offline.invalid/mcp",
                headers={"Authorization": "Bearer private-key"},
            ),
        )

    def start(
        self, workspace_manager: LocalWorkspaceManager | None = None
    ) -> TestClient:
        """Build an app sharing the test storage across restarts."""
        redis = self.redis

        class Storage(RedisStorage):
            """Redis storage backed by the shared in-memory client."""

            async def __aenter__(self) -> Any:
                """Aenter fixture."""
                self._client = redis
                return self

            async def aclose(self) -> None:
                """Aclose fixture."""
                self._client = None

        class Bus(RedisMessageBus):
            """Message bus backed by the shared in-memory client."""

            async def __aenter__(self) -> Any:
                """Aenter fixture."""
                self._client = redis
                return self

            async def aclose(self) -> None:
                """Aclose fixture."""
                self._client = None

        app = create_app(
            storage=Storage(),
            message_bus=Bus(),
            workspace_manager=workspace_manager
            or LocalWorkspaceManager(str(self.root / "workspaces")),
            skill_store=LocalSkillStore(str(self.root / "artifacts")),
            enable_index_worker=False,
        )
        fixture = self

        class UserClient(TestClient):
            """Create fixture data through user-management endpoints."""

            def __enter__(self) -> Any:
                """Start the app and perform explicit fixture setup."""
                super().__enter__()
                if fixture.seed_resources:
                    fixture.seed_library(self, HEADERS)
                return self

        return UserClient(app)

    def seed_library(self, client: TestClient, headers: dict) -> None:
        """Explicitly create fixture resources without replacing saved data."""
        if not client.get("/mcp", headers=headers).json():
            response = client.post(
                "/mcp",
                json={
                    "client": self.default_mcp.model_dump(mode="json"),
                    "version": "1",
                },
                headers=headers,
            )
            self.assertEqual(response.status_code, 201, response.text)
        if not client.get("/skill", headers=headers).json():
            response = client.post(
                "/skill",
                json={
                    "files": {
                        "SKILL.md": (self.skill_path / "SKILL.md").read_text(
                            "utf-8"
                        ),
                        "scripts/run.py": (
                            self.skill_path / "scripts/run.py"
                        ).read_text("utf-8"),
                    },
                    "version": "1",
                },
                headers=headers,
            )
            self.assertEqual(response.status_code, 201, response.text)

    def scope(self, client: TestClient) -> dict[str, str]:
        """Create a persisted agent and session through the HTTP API."""
        agent = client.post(
            "/agent/",
            json={"name": "tester", "system_prompt": "hi"},
            headers=HEADERS,
        )
        self.assertEqual(agent.status_code, 201, agent.text)
        agent_id = agent.json()["agent_id"]
        session = client.post(
            "/sessions/", json={"agent_id": agent_id}, headers=HEADERS
        )
        self.assertEqual(session.status_code, 201, session.text)
        scope = {
            "agent_id": agent_id,
            "session_id": session.json()["session_id"],
        }
        if self.seed_resources:
            for kind in ("mcp", "skill"):
                records = client.get(f"/{kind}", headers=HEADERS).json()
                response = client.post(
                    f"/workspace/{kind}/from-library",
                    params=scope,
                    json={f"{kind}_ids": [record["id"] for record in records]},
                    headers=HEADERS,
                )
                self.assertEqual(response.status_code, 201, response.text)
                self.assertFalse(response.json()["failed"])
        return scope

    def listing(
        self, client: TestClient, kind: str, scope: dict
    ) -> list[dict]:
        """List one resource kind in a selected workspace scope."""
        response = client.get(
            f"/workspace/{kind}", params=scope, headers=HEADERS
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_user_stdio_configuration_survives_restart_without_connecting(
        self,
    ) -> None:
        """User snapshots stay independent and list without transport I/O."""
        self.seed_resources = False
        source = MCPClient(
            name="browser",
            is_stateful=True,
            mcp_config=StdioMCPConfig(
                command="offline-test-command", args=["--original"]
            ),
        )
        with self.start() as client:
            created = client.post(
                "/mcp",
                json={"client": source.model_dump(mode="json")},
                headers=HEADERS,
            )
            self.assertEqual(created.status_code, 201, created.text)
            record_id = created.json()["id"]
            source.mcp_config.args.append("--changed")
        with self.start() as client:
            detail = client.get(f"/mcp/{record_id}", headers=HEADERS)
            self.assertEqual(detail.status_code, 200, detail.text)
            self.assertEqual(
                detail.json()["client"]["mcp_config"]["args"], ["--original"]
            )
            self.assertEqual(detail.json()["source"], "custom")
            self.assertNotIn("builtin_id", detail.json())
            self.assertNotIn("available_version", detail.json())
            self.assertEqual(
                client.get("/health", headers=HEADERS).status_code, 200
            )

    def test_direct_mcp_management_masks_and_retains_credentials(self) -> None:
        """Direct edits preserve credentials, IDs and disabled switches."""
        self.seed_resources = False
        body = {
            "client": {
                "name": "manual",
                "is_stateful": False,
                "mcp_config": {
                    "type": "http_mcp",
                    "url": "https://offline.invalid/mcp?token=url-secret",
                    "headers": {"Authorization": "Bearer header-secret"},
                },
            },
            "description": "manual config",
        }
        with patch.object(
            MCPClient, "connect", new_callable=AsyncMock
        ) as connect:
            with self.start() as client:
                created = client.post("/mcp", json=body, headers=HEADERS)
                self.assertEqual(created.status_code, 201, created.text)
                record_id = created.json()["id"]
                self.assertEqual(created.json()["source"], "custom")
                self.assertNotIn("header-secret", created.text)
                duplicate = client.post("/mcp", json=body, headers=HEADERS)
                self.assertEqual(duplicate.status_code, 409, duplicate.text)
                detail = client.get(f"/mcp/{record_id}", headers=HEADERS)
                self.assertEqual(detail.status_code, 200, detail.text)
                self.assertNotIn("header-secret", detail.text)
                self.assertNotIn("url-secret", detail.text)
                edited = detail.json()["client"]
                edited["name"] = "renamed-manual"
                edited["mcp_config"]["timeout"] = 42
                changed = client.patch(
                    f"/mcp/{record_id}",
                    json={"client": edited, "description": "edited"},
                    headers=HEADERS,
                )
                self.assertEqual(changed.status_code, 200, changed.text)
                self.assertEqual(changed.json()["id"], record_id)
                self.assertEqual(changed.json()["name"], "renamed-manual")
                self.assertNotIn("enabled", changed.json())
                saved = client.portal.call(
                    client.app.state.storage.get_mcp, "alice", record_id
                )
                self.assertEqual(
                    saved.client.mcp_config.url,
                    body["client"]["mcp_config"]["url"],
                )
                self.assertEqual(
                    saved.client.mcp_config.headers,
                    {"Authorization": "Bearer header-secret"},
                )
                self.assertEqual(saved.client.mcp_config.timeout, 42)
                self.assertEqual(
                    client.get(
                        f"/mcp/{record_id}", headers={"X-User-ID": "bob"}
                    ).status_code,
                    404,
                )
                removed = client.delete(f"/mcp/{record_id}", headers=HEADERS)
                self.assertEqual(removed.status_code, 204, removed.text)
                self.assertEqual(
                    client.get("/mcp", headers=HEADERS).json(), []
                )
            connect.assert_not_awaited()

    def test_stdio_edit_masks_auth_args_and_rejects_unknown_placeholders(
        self,
    ) -> None:
        """Read/edit keeps write-only CLI arguments and environment values."""
        self.seed_resources = False
        with self.start() as client:
            created = client.post(
                "/mcp",
                json={
                    "client": {
                        "name": "stdio",
                        "is_stateful": True,
                        "mcp_config": {
                            "type": "stdio_mcp",
                            "command": "offline-command",
                            "args": [
                                "--api-key",
                                "argument-secret",
                                "--port",
                                "123",
                            ],
                            "env": {"TOKEN": "environment-secret"},
                        },
                    }
                },
                headers=HEADERS,
            )
            self.assertEqual(created.status_code, 201, created.text)
            record_id = created.json()["id"]
            detail = client.get(f"/mcp/{record_id}", headers=HEADERS)
            self.assertNotIn("argument-secret", detail.text)
            self.assertNotIn("environment-secret", detail.text)
            config = detail.json()["client"]
            config["mcp_config"]["args"][3] = "456"
            changed = client.patch(
                f"/mcp/{record_id}", json={"client": config}, headers=HEADERS
            )
            self.assertEqual(changed.status_code, 200, changed.text)
            saved = client.portal.call(
                client.app.state.storage.get_mcp, "alice", record_id
            )
            self.assertEqual(
                saved.client.mcp_config.args,
                ["--api-key", "argument-secret", "--port", "456"],
            )
            self.assertEqual(
                saved.client.mcp_config.env, {"TOKEN": "environment-secret"}
            )
            config["mcp_config"]["env"]["UNKNOWN"] = "[KEEP_EXISTING_SECRET]"
            rejected = client.patch(
                f"/mcp/{record_id}", json={"client": config}, headers=HEADERS
            )
            self.assertEqual(rejected.status_code, 422, rejected.text)
            self.assertNotIn("argument-secret", rejected.text)

    def test_skill_revision_date_survives_noops_switches_and_restart(
        self,
    ) -> None:
        """Date content/label revisions, not switches or identical saves."""
        self.seed_resources = False
        first = datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc)
        second = datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc)
        third = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)
        with (
            patch("agentscope.app.storage._model._skill.datetime") as clock,
            self.start() as client,
        ):
            clock.now.return_value = first
            created = client.post(
                "/skill",
                json={
                    "files": {
                        "SKILL.md": _markdown("dated"),
                        "scripts/run.py": "original",
                    },
                    "version": "1",
                },
                headers=HEADERS,
            )
            self.assertEqual(created.status_code, 201, created.text)
            record_id = created.json()["id"]
            date = created.json()["version_updated_at"]
            self.assertEqual(datetime.fromisoformat(date), first)
            clock.now.assert_called_once_with(timezone.utc)
            clock.now.return_value = second
            scope = self.scope(client)
            client.post(
                "/workspace/skill/from-library",
                params=scope,
                headers=HEADERS,
                json={"skill_ids": [record_id]},
            )
            for enabled in (False, True):
                switched = client.patch(
                    f"/workspace/skill/{record_id}",
                    params=scope,
                    json={"enabled": enabled},
                    headers=HEADERS,
                )
                self.assertEqual(switched.status_code, 204, switched.text)
                self.assertEqual(
                    client.get(f"/skill/{record_id}", headers=HEADERS).json()[
                        "version_updated_at"
                    ],
                    date,
                )
            same = client.patch(
                f"/skill/{record_id}/files",
                json={"files": {"scripts/run.py": "original"}},
                headers=HEADERS,
            )
            self.assertEqual(same.status_code, 200, same.text)
            self.assertEqual(same.json()["version_updated_at"], date)
            relabeled = client.patch(
                f"/skill/{record_id}/files",
                json={
                    "files": {"scripts/run.py": "original"},
                    "version": "2",
                },
                headers=HEADERS,
            )
            self.assertEqual(relabeled.status_code, 200, relabeled.text)
            self.assertEqual(
                datetime.fromisoformat(relabeled.json()["version_updated_at"]),
                second,
            )
            clock.now.return_value = third
            edited = client.patch(
                f"/skill/{record_id}/files",
                json={"files": {"scripts/run.py": "only script changed"}},
                headers=HEADERS,
            )
            self.assertEqual(edited.status_code, 200, edited.text)
            saved = edited.json()
            self.assertEqual(
                datetime.fromisoformat(saved["version_updated_at"]), third
            )
            self.assertNotEqual(saved["version"], relabeled.json()["version"])
            for view in (
                client.get("/skill", headers=HEADERS).json()[0],
                client.get(f"/skill/{record_id}", headers=HEADERS).json(),
            ):
                self.assertEqual(
                    view["version_updated_at"], saved["version_updated_at"]
                )
        with self.start() as client:
            self.assertEqual(
                client.get("/skill", headers=HEADERS).json()[0], saved
            )

    def test_legacy_skill_revision_date_is_not_inferred_or_migrated(
        self,
    ) -> None:
        """Reads, switches and no-op saves cannot invent an old date."""
        with self.start() as client:
            record = client.get("/skill", headers=HEADERS).json()[0]
            record_id = record["id"]

            async def undate() -> tuple[str, str]:
                """Drop the new field to simulate an untouched old record."""
                storage = client.app.state.storage
                key = storage._key(
                    storage.key_config.skill,
                    user_id="alice",
                    skill_id=record_id,
                )
                payload = json.loads(await self.redis.get(key))
                payload.pop("version_updated_at")
                raw = json.dumps(payload)
                await self.redis.set(key, raw)
                return key, raw

            key, raw = client.portal.call(undate)
            for view in (
                client.get("/skill", headers=HEADERS).json()[0],
                client.get(f"/skill/{record_id}", headers=HEADERS).json(),
            ):
                self.assertIsNone(view["version_updated_at"])
            self.assertEqual(client.portal.call(self.redis.get, key), raw)
            for script in ("version one", "new content"):
                response = client.patch(
                    f"/skill/{record_id}/files",
                    json={"files": {"scripts/run.py": script}},
                    headers=HEADERS,
                )
                self.assertEqual(response.status_code, 200, response.text)
                if script == "version one":
                    self.assertIsNone(response.json()["version_updated_at"])
                else:
                    self.assertIsNotNone(response.json()["version_updated_at"])

    def test_direct_skill_authoring_and_owned_file_access(self) -> None:
        """A Skill can be authored and edited without an agent or session."""
        self.seed_resources = False
        files = {
            "SKILL.md": _markdown("authored"),
            "scripts/run.py": "print('one')",
        }
        with self.start() as client:
            created = client.post(
                "/skill", json={"files": files}, headers=HEADERS
            )
            self.assertEqual(created.status_code, 201, created.text)
            record_id = created.json()["id"]
            listed = client.get(f"/skill/{record_id}/files", headers=HEADERS)
            self.assertEqual(listed.status_code, 200, listed.text)
            self.assertEqual(
                {entry["path"] for entry in listed.json()}, set(files)
            )
            text = client.get(
                f"/skill/{record_id}/file",
                params={"path": "scripts/run.py"},
                headers=HEADERS,
            )
            self.assertEqual(text.json()["content"], files["scripts/run.py"])
            duplicate = client.post(
                "/skill", json={"files": files}, headers=HEADERS
            )
            self.assertEqual(duplicate.status_code, 409, duplicate.text)
            for route in (
                f"/skill/{record_id}/files",
                f"/skill/{record_id}/file?path=SKILL.md",
            ):
                self.assertEqual(
                    client.get(
                        route, headers={"X-User-ID": "bob"}
                    ).status_code,
                    404,
                )
            denied = client.patch(
                f"/skill/{record_id}/files",
                json={"files": {"scripts/run.py": "wrong owner"}},
                headers={"X-User-ID": "bob"},
            )
            self.assertEqual(denied.status_code, 404, denied.text)
            for path in (
                "../outside",
                "C:/outside",
                "folder\\outside",
                "/outside",
            ):
                response = client.get(
                    f"/skill/{record_id}/file",
                    params={"path": path},
                    headers=HEADERS,
                )
                self.assertEqual(response.status_code, 422, response.text)
            changed = client.patch(
                f"/skill/{record_id}/files",
                json={"files": {"scripts/run.py": "print('two')"}},
                headers=HEADERS,
            )
            self.assertEqual(changed.status_code, 200, changed.text)
            self.assertEqual(changed.json()["id"], record_id)
            self.assertNotEqual(
                changed.json()["version"], created.json()["version"]
            )
            removed = client.delete(f"/skill/{record_id}", headers=HEADERS)
            self.assertEqual(removed.status_code, 204, removed.text)
            self.assertEqual(client.get("/skill", headers=HEADERS).json(), [])

    def test_archive_edits_retain_binary_files_and_old_package(self) -> None:
        """Text edits preserve the rest of the complete package."""
        self.seed_resources = False
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("SKILL.md", _markdown("binary"))
            archive.writestr("scripts/run.py", "old script")
            archive.writestr("assets/icon.bin", b"\x00\xff\x01")
            archive.writestr("references/old.txt", "old reference")
        with self.start() as client:
            created = client.post(
                "/skill/archive",
                params={"archive_format": "zip", "version": "1"},
                files={"archive": ("skill.zip", buffer.getvalue())},
                headers=HEADERS,
            )
            self.assertEqual(created.status_code, 201, created.text)
            record_id = created.json()["id"]
            before = client.portal.call(
                client.app.state.storage.get_skill, "alice", record_id
            )
            store = client.app.state.resource_service.skill_store
            old_dir = Path(store.path(before.artifact_id))
            file_list = client.get(
                f"/skill/{record_id}/files", headers=HEADERS
            ).json()
            binary = next(
                item for item in file_list if item["path"] == "assets/icon.bin"
            )
            self.assertFalse(binary["editable"])
            self.assertEqual(
                client.get(
                    f"/skill/{record_id}/file",
                    params={"path": binary["path"]},
                    headers=HEADERS,
                ).status_code,
                422,
            )
            response = client.patch(
                f"/skill/{record_id}/files",
                json={
                    "files": {
                        "scripts/run.py": "new script",
                        "references/old.txt": None,
                        "references/new.txt": "new reference",
                    },
                    "version": "2",
                },
                headers=HEADERS,
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("enabled", response.json())
            self.assertIsNotNone(before.version_updated_at)
            after = client.portal.call(
                client.app.state.storage.get_skill, "alice", record_id
            )
            self.assertGreater(
                after.version_updated_at, before.version_updated_at
            )
            new_dir = Path(store.path(after.artifact_id))
            self.assertNotEqual(old_dir, new_dir)
            self.assertEqual(
                (old_dir / "scripts/run.py").read_text(), "old script"
            )
            self.assertEqual(
                (new_dir / "scripts/run.py").read_text(), "new script"
            )
            self.assertEqual(
                (new_dir / "assets/icon.bin").read_bytes(), b"\x00\xff\x01"
            )
            self.assertFalse((new_dir / "references/old.txt").exists())
            self.assertEqual(
                (new_dir / "references/new.txt").read_text(), "new reference"
            )

    def test_invalid_skill_edits_leave_saved_package_unchanged(self) -> None:
        """Unsafe paths, bad metadata and duplicate versions do not commit."""
        self.seed_resources = False
        with self.start() as client:
            created = client.post(
                "/skill",
                json={
                    "files": {
                        "SKILL.md": _markdown("protected"),
                        "scripts/run.py": "original",
                    },
                    "version": "1",
                },
                headers=HEADERS,
            )
            self.assertEqual(created.status_code, 201, created.text)
            record_id = created.json()["id"]
            cases = (
                ({"files": {"../escaped": "bad"}}, 422),
                ({"files": {"scripts": "conflicts with a directory"}}, 422),
                ({"files": {"SKILL.md/child.txt": "bad"}}, 422),
                ({"files": {"SKILL.md": None}}, 422),
                ({"files": {"SKILL.md": "invalid metadata"}}, 422),
                (
                    {"files": {"scripts/run.py": "changed"}, "version": "1"},
                    409,
                ),
            )
            for body, expected in cases:
                with self.subTest(body=body):
                    response = client.patch(
                        f"/skill/{record_id}/files", json=body, headers=HEADERS
                    )
                    self.assertEqual(
                        response.status_code, expected, response.text
                    )
                    self.assertNotIn(str(self.root), response.text)
                    self.assertNotIn(
                        str(self.root).replace("\\", "\\\\"), response.text
                    )
                    self.assertEqual(
                        client.get(
                            f"/skill/{record_id}", headers=HEADERS
                        ).json()["version"],
                        "1",
                    )
                    self.assertEqual(
                        client.get(
                            f"/skill/{record_id}", headers=HEADERS
                        ).json()["version_updated_at"],
                        created.json()["version_updated_at"],
                    )
                    self.assertEqual(
                        client.get(
                            f"/skill/{record_id}/file",
                            params={"path": "scripts/run.py"},
                            headers=HEADERS,
                        ).json()["content"],
                        "original",
                    )

    def test_direct_folder_upload_and_complete_replacement(self) -> None:
        """Folder imports preserve IDs and need no workspace."""
        self.seed_resources = False

        def upload(client: TestClient, url: str, script: bytes) -> Any:
            payload = {
                "folder/SKILL.md": _markdown("folder").encode(),
                "folder/scripts/run.py": script,
            }
            manifest = json.dumps(
                {
                    "entries": [
                        {"path": path, "size": len(data)}
                        for path, data in payload.items()
                    ]
                }
            )
            return client.post(
                url,
                data={"manifest": manifest},
                files=[
                    ("files", (Path(path).name, data))
                    for path, data in payload.items()
                ],
                headers=HEADERS,
            )

        with self.start() as client:
            created = upload(client, "/skill/upload", b"first")
            self.assertEqual(created.status_code, 201, created.text)
            record_id = created.json()["id"]
            changed = upload(client, f"/skill/{record_id}/upload", b"second")
            self.assertEqual(changed.status_code, 200, changed.text)
            self.assertEqual(changed.json()["id"], record_id)
            self.assertNotEqual(
                changed.json()["version"], created.json()["version"]
            )
            self.assertIsNotNone(created.json()["version_updated_at"])
            self.assertNotEqual(
                changed.json()["version_updated_at"],
                created.json()["version_updated_at"],
            )
            self.assertEqual(
                client.get(
                    f"/skill/{record_id}/file",
                    params={"path": "scripts/run.py"},
                    headers=HEADERS,
                ).json()["content"],
                "second",
            )

    def test_library_edit_requires_explicit_workspace_apply(self) -> None:
        """Library edits preserve workspace copies and upgrade protection."""
        self.seed_resources = False
        with self.start() as client:
            created = client.post(
                "/skill",
                json={
                    "files": {
                        "SKILL.md": _markdown("selected"),
                        "scripts/run.py": "first",
                    }
                },
                headers=HEADERS,
            )
            self.assertEqual(created.status_code, 201, created.text)
            record_id = created.json()["id"]
            scope = self.scope(client)
            equipped = client.post(
                "/workspace/skill/from-library",
                params=scope,
                json={"skill_ids": [record_id]},
                headers=HEADERS,
            )
            self.assertEqual(equipped.status_code, 201, equipped.text)
            installed = self.listing(client, "skill", scope)[0]
            initial_date = created.json()["version_updated_at"]
            self.assertEqual(
                installed["installed_version_updated_at"], initial_date
            )
            self.assertEqual(installed["version_updated_at"], initial_date)
            old_script = Path(installed["dir"], "scripts/run.py")
            edited = client.patch(
                f"/skill/{record_id}/files",
                json={"files": {"scripts/run.py": "second"}},
                headers=HEADERS,
            )
            self.assertEqual(edited.status_code, 200, edited.text)
            pending = self.listing(client, "skill", scope)[0]
            new_date = edited.json()["version_updated_at"]
            self.assertNotEqual(initial_date, new_date)
            self.assertEqual(
                pending["installed_version_updated_at"], initial_date
            )
            self.assertEqual(pending["version_updated_at"], new_date)
            self.assertEqual(old_script.read_text(), "first")
            old_script.write_text("workspace edit")
            rejected = client.post(
                f"/workspace/skill/{record_id}/upgrade",
                params=scope,
                headers=HEADERS,
            )
            self.assertEqual(rejected.status_code, 409, rejected.text)
            self.assertEqual(
                self.listing(client, "skill", scope)[0][
                    "installed_version_updated_at"
                ],
                initial_date,
            )
            self.assertEqual(old_script.read_text(), "workspace edit")
            old_script.write_text("first")
            applied = client.post(
                f"/workspace/skill/{record_id}/upgrade",
                params=scope,
                headers=HEADERS,
            )
            self.assertEqual(applied.status_code, 200, applied.text)
            current = self.listing(client, "skill", scope)[0]
            self.assertEqual(current["installed_version_updated_at"], new_date)
            self.assertEqual(
                Path(current["dir"], "scripts/run.py").read_text(), "second"
            )
            self.assertEqual(old_script.read_text(), "first")

    def test_old_builtin_markers_are_ignored_and_records_can_be_managed(
        self,
    ) -> None:
        """Old markers neither rewrite stored data nor restrict CRUD."""
        with self.start() as client:
            storage = client.app.state.storage

            async def mark_legacy(
                kind: str, record_id: str
            ) -> tuple[str, str]:
                """Insert the old JSON shape to exercise read compatibility."""
                key = storage._key(
                    getattr(storage.key_config, kind),
                    user_id="alice",
                    **{f"{kind}_id": record_id},
                )
                payload = json.loads(await self.redis.get(key))
                payload["builtin_id"] = "old-custom-definition"
                raw = json.dumps(payload)
                await self.redis.set(key, raw)
                return key, raw

            for kind in ("mcp", "skill"):
                record = client.get(f"/{kind}", headers=HEADERS).json()[0]
                record_id = record["id"]
                key, raw = client.portal.call(mark_legacy, kind, record_id)
                view = client.get(
                    f"/{kind}/{record_id}", headers=HEADERS
                ).json()
                self.assertEqual(view["source"], "custom")
                self.assertNotIn("builtin_id", view)
                self.assertNotIn("available_version", view)
                self.assertEqual(client.portal.call(self.redis.get, key), raw)
                deleted = client.delete(
                    f"/{kind}/{record_id}", headers=HEADERS
                )
                self.assertEqual(deleted.status_code, 204, deleted.text)
                self.assertEqual(
                    client.get(f"/{kind}", headers=HEADERS).json(), []
                )
        self.seed_resources = False
        with self.start() as client:
            for kind in ("mcp", "skill"):
                self.assertEqual(
                    client.get(f"/{kind}", headers=HEADERS).json(), []
                )
                self.assertEqual(
                    client.get(
                        f"/{kind}", headers={"X-User-ID": "new-user"}
                    ).json(),
                    [],
                )

    def test_workspace_defaults_are_not_converted_to_user_records(
        self,
    ) -> None:
        """Workspace defaults do not populate user-managed resources."""
        manager = LocalWorkspaceManager(
            str(self.root / "workspaces"),
            default_mcps=[self.default_mcp],
            skill_paths=[str(self.skill_path)],
        )
        self.seed_resources = False
        with self.start(manager) as client:
            self.assertEqual(client.get("/mcp", headers=HEADERS).json(), [])
            self.assertEqual(client.get("/skill", headers=HEADERS).json(), [])
            scope = self.scope(client)
            self.assertEqual(self.listing(client, "mcp", scope), [])
            self.assertEqual(self.listing(client, "skill", scope), [])
            self.assertEqual(client.get("/mcp", headers=HEADERS).json(), [])
            self.assertEqual(client.get("/skill", headers=HEADERS).json(), [])

    def test_mcp_rename_frees_the_old_name_in_the_workspace(self) -> None:
        """Renamed bindings and a new resource may coexist and be removed."""
        with self.start() as client:
            scope = self.scope(client)
            original_id = self.listing(client, "mcp", scope)[0]["resource_id"]
            renamed = client.patch(
                f"/mcp/{original_id}",
                json={"name": "renamed-browser"},
                headers=HEADERS,
            )
            self.assertEqual(renamed.status_code, 200, renamed.text)
            added = client.post(
                "/workspace/mcp",
                params=scope,
                json={
                    "name": "browser",
                    "is_stateful": False,
                    "mcp_config": {
                        "type": "http_mcp",
                        "url": "https://new.invalid/mcp",
                    },
                },
                headers=HEADERS,
            )
            self.assertEqual(added.status_code, 201, added.text)
            entries = self.listing(client, "mcp", scope)
            ids = {r["name"]: r["resource_id"] for r in entries}
            self.assertEqual(set(ids), {"browser", "renamed-browser"})
            self.assertEqual(ids["renamed-browser"], original_id)
            self.assertNotEqual(ids["browser"], original_id)
        with self.start() as client:
            self.assertEqual(
                {
                    r["name"]: r["resource_id"]
                    for r in self.listing(client, "mcp", scope)
                },
                ids,
            )
            uninstalled = client.delete(
                f"/mcp/{ids['browser']}", headers=HEADERS
            )
            self.assertEqual(uninstalled.status_code, 204, uninstalled.text)
            retained = self.listing(client, "mcp", scope)
            deleted = next(
                r for r in retained if r["resource_id"] == ids["browser"]
            )
            self.assertEqual(deleted["status"], "unavailable")
            self.assertEqual(deleted["resource_id"], ids["browser"])
            removed = client.delete(
                f"/workspace/mcp/{ids['browser']}",
                params=scope,
                headers=HEADERS,
            )
            self.assertEqual(removed.status_code, 204, removed.text)
            remaining = self.listing(client, "mcp", scope)
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["resource_id"], original_id)
            self.assertEqual(remaining[0]["name"], "renamed-browser")

    def test_switches_names_and_selection_survive_app_restart(self):
        with self.start() as client:
            scope = self.scope(client)
            selected = {
                kind: self.listing(client, kind, scope)[0]
                for kind in ("mcp", "skill")
            }
            script = Path(selected["skill"]["dir"], "scripts/run.py")
            script.write_text("session edit", encoding="utf-8")
            mcp_id = selected["mcp"]["resource_id"]
            client.patch(
                f"/mcp/{mcp_id}",
                headers=HEADERS,
                json={"name": "renamed-browser"},
            )
            for kind, row in selected.items():
                response = client.patch(
                    f"/workspace/{kind}/{row['resource_id']}",
                    params=scope,
                    headers=HEADERS,
                    json={"enabled": False},
                )
                self.assertEqual(response.status_code, 204, response.text)
        with self.start() as client:
            for kind, row in selected.items():
                self.assertFalse(
                    self.listing(client, kind, scope)[0]["enabled"]
                )
                response = client.patch(
                    f"/workspace/{kind}/{row['resource_id']}",
                    params=scope,
                    headers=HEADERS,
                    json={"enabled": True},
                )
                self.assertEqual(response.status_code, 204, response.text)
            self.assertEqual(
                self.listing(client, "mcp", scope)[0]["name"],
                "renamed-browser",
            )
            self.assertEqual(script.read_text("utf-8"), "session edit")
            for kind, row in selected.items():
                self.assertEqual(
                    client.delete(
                        f"/workspace/{kind}/{row['resource_id']}",
                        params=scope,
                        headers=HEADERS,
                    ).status_code,
                    204,
                )
        with self.start() as client:
            self.assertEqual(self.listing(client, "mcp", scope), [])
            self.assertEqual(self.listing(client, "skill", scope), [])

    def test_resources_and_changes_are_owner_scoped(self):
        with self.start() as client:
            scope = self.scope(client)
            self.seed_library(client, {"X-User-ID": "bob"})
            for kind in ("mcp", "skill"):
                alice = client.get(f"/{kind}", headers=HEADERS).json()[0]
                bob_headers = {"X-User-ID": "bob"}
                bob = client.get(f"/{kind}", headers=bob_headers).json()[0]
                self.assertNotEqual(alice["id"], bob["id"])
                self.assertEqual(
                    client.patch(
                        f"/workspace/{kind}/{alice['id']}",
                        params=scope,
                        json={"enabled": False},
                        headers=bob_headers,
                    ).status_code,
                    404,
                )
                self.assertEqual(
                    client.delete(
                        f"/{kind}/{alice['id']}", headers=bob_headers
                    ).status_code,
                    404,
                )
                self.assertEqual(
                    client.get(f"/{kind}", headers=bob_headers).json()[0], bob
                )
                self.assertEqual(
                    client.post(
                        f"/{kind}/{alice['id']}/upgrade", headers=bob_headers
                    ).status_code,
                    404,
                )

    def test_user_update_requires_library_then_workspace_apply(
        self,
    ) -> None:
        """User updates require a separate workspace apply."""
        with self.start() as client:
            scope = self.scope(client)
            old = self.listing(client, "skill", scope)[0]
            skill_id = old["resource_id"]
        (self.skill_path / "scripts/run.py").write_text(
            "version two", encoding="utf-8"
        )
        with self.start() as client:
            library = client.get("/skill", headers=HEADERS).json()[0]
            self.assertEqual(library["version"], "1")
            self.assertEqual(
                client.patch(
                    f"/skill/{skill_id}/files",
                    json={
                        "files": {"scripts/run.py": "version two"},
                        "version": "2",
                    },
                    headers=HEADERS,
                ).status_code,
                200,
            )
            installed = self.listing(client, "skill", scope)[0]
            self.assertEqual(
                (installed["version"], installed["installed_version"]),
                ("2", "1"),
            )
            self.assertEqual(
                Path(installed["dir"], "scripts/run.py").read_text(
                    encoding="utf-8"
                ),
                "version one",
            )
            response = client.post(
                f"/workspace/skill/{skill_id}/upgrade",
                params=scope,
                headers=HEADERS,
            )
            self.assertEqual(response.status_code, 200, response.text)
            installed = self.listing(client, "skill", scope)
            self.assertEqual(len(installed), 1)
            self.assertEqual(installed[0]["installed_version"], "2")
            self.assertEqual(
                Path(installed[0]["dir"], "scripts/run.py").read_text(
                    encoding="utf-8"
                ),
                "version two",
            )
            self.assertTrue(Path(old["dir"]).is_dir())
            mcp = client.get("/mcp", headers=HEADERS).json()[0]
            self.assertEqual(mcp["version"], "1")
            self.assertEqual(
                client.patch(
                    f"/mcp/{mcp['id']}",
                    json={
                        "client": self.default_mcp.model_dump(mode="json"),
                        "version": "2",
                    },
                    headers=HEADERS,
                ).json()["version"],
                "2",
            )

    def test_workspace_upgrade_refuses_to_replace_edits(self) -> None:
        """Workspace upgrade refuses to replace edits."""
        with self.start() as client:
            scope = self.scope(client)
            old = self.listing(client, "skill", scope)[0]
            script = Path(old["dir"], "scripts/run.py")
            script.write_text("my edits", encoding="utf-8")
        (self.skill_path / "scripts/run.py").write_text(
            "version two", encoding="utf-8"
        )
        with self.start() as client:
            response = client.patch(
                f"/skill/{old['resource_id']}/files",
                json={
                    "files": {"scripts/run.py": "version two"},
                    "version": "2",
                },
                headers=HEADERS,
            )
            self.assertEqual(response.status_code, 200, response.text)
            response = client.post(
                f"/workspace/skill/{old['resource_id']}/upgrade",
                params=scope,
                headers=HEADERS,
            )
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(script.read_text(encoding="utf-8"), "my edits")
            self.assertEqual(
                self.listing(client, "skill", scope)[0]["installed_version"],
                "1",
            )

    def test_manual_skill_package_update_validates_before_publishing(
        self,
    ) -> None:
        """Manual skill package update validates before publishing."""
        custom = self.root / "custom"
        (custom / "scripts").mkdir(parents=True)
        (custom / "SKILL.md").write_text(_markdown("custom"), encoding="utf-8")
        (custom / "scripts/run.py").write_text("original", encoding="utf-8")
        with self.start() as client:
            scope = self.scope(client)
            upload = client.post(
                "/workspace/skill",
                params=scope,
                json={"skill_path": str(custom)},
                headers=HEADERS,
            )
            self.assertEqual(upload.status_code, 201, upload.text)
            record = next(
                r
                for r in client.get("/skill", headers=HEADERS).json()
                if r["name"] == "custom"
            )
            skill_id = record["id"]
            self.assertIsNotNone(record["version_updated_at"])
            invalid = client.post(
                f"/skill/{skill_id}/package",
                params={"archive_format": "zip", "version": "2"},
                files={"archive": ("invalid.zip", b"invalid")},
                headers=HEADERS,
            )
            self.assertEqual(invalid.status_code, 422, invalid.text)
            self.assertEqual(
                client.get(f"/skill/{skill_id}", headers=HEADERS).json()[
                    "version"
                ],
                record["version"],
            )
            same_version = client.post(
                f"/skill/{skill_id}/package",
                params={"archive_format": "zip", "version": record["version"]},
                files={
                    "archive": ("skill.zip", _archive("custom", "updated"))
                },
                headers=HEADERS,
            )
            self.assertEqual(same_version.status_code, 409, same_version.text)
            update = client.post(
                f"/skill/{skill_id}/package",
                params={"archive_format": "zip", "version": "2"},
                files={
                    "archive": ("skill.zip", _archive("custom", "updated"))
                },
                headers=HEADERS,
            )
            self.assertEqual(update.status_code, 200, update.text)
            self.assertNotEqual(
                update.json()["version_updated_at"],
                record["version_updated_at"],
            )
            apply = client.post(
                f"/workspace/skill/{skill_id}/upgrade",
                params=scope,
                headers=HEADERS,
            )
            self.assertEqual(apply.status_code, 200, apply.text)
            installed = next(
                r
                for r in self.listing(client, "skill", scope)
                if r["resource_id"] == skill_id
            )
            self.assertEqual(
                Path(installed["dir"], "scripts/run.py").read_text(
                    encoding="utf-8"
                ),
                "updated",
            )
            self.assertEqual(
                client.delete(
                    f"/skill/{skill_id}", headers=HEADERS
                ).status_code,
                204,
            )
            installed = next(
                r
                for r in self.listing(client, "skill", scope)
                if r["resource_id"] == skill_id
            )
            self.assertEqual(installed["status"], "unavailable")
            self.assertTrue(Path(installed["dir"]).is_dir())

    def test_invalid_mcp_config_edit_is_rejected_without_exposing_secrets(
        self,
    ) -> None:
        """Invalid mcp config edit is rejected without exposing secrets."""
        with self.start() as client:
            before = client.get("/mcp", headers=HEADERS).json()[0]
            response = client.patch(
                f"/mcp/{before['id']}",
                json={
                    "name": "illegal name",
                    "client": self.default_mcp.model_dump(mode="json"),
                },
                headers=HEADERS,
            )
            self.assertEqual(response.status_code, 422, response.text)
            self.assertNotIn("private-key", response.text)
            self.assertEqual(
                client.get("/mcp", headers=HEADERS).json()[0], before
            )

    def test_folder_upload_saves_all_files_and_retains_session_switch(
        self,
    ) -> None:
        """Folder upload saves all files and rejects disabled resource."""
        payload = {
            "upload/SKILL.md": _markdown("upload").encode(),
            "upload/scripts/run.py": b"uploaded script",
            "upload/references/info.txt": b"reference",
        }
        manifest = json.dumps(
            {
                "entries": [
                    {"path": path, "size": len(data)}
                    for path, data in payload.items()
                ]
            }
        )
        files = [
            ("files", (Path(path).name, data, "application/octet-stream"))
            for path, data in payload.items()
        ]
        with self.start() as client:
            scope = self.scope(client)
            response = client.post(
                "/workspace/skill/upload",
                params=scope,
                data={"manifest": manifest},
                files=files,
                headers=HEADERS,
            )
            self.assertEqual(response.status_code, 201, response.text)
            installed = next(
                r
                for r in self.listing(client, "skill", scope)
                if r["name"] == "upload"
            )
            directory = Path(installed["dir"])
            self.assertIsNotNone(installed["installed_version_updated_at"])
            self.assertEqual(
                (directory / "scripts/run.py").read_bytes(), b"uploaded script"
            )
            self.assertEqual(
                (directory / "references/info.txt").read_bytes(), b"reference"
            )
            client.patch(
                f"/workspace/skill/{installed['resource_id']}",
                params=scope,
                json={"enabled": False},
                headers=HEADERS,
            )
            retry = client.post(
                "/workspace/skill/upload",
                params=scope,
                data={"manifest": manifest},
                files=files,
                headers=HEADERS,
            )
            self.assertEqual(retry.status_code, 201, retry.text)
            self.assertFalse(
                next(
                    r
                    for r in self.listing(client, "skill", scope)
                    if r["resource_id"] == installed["resource_id"]
                )["enabled"]
            )
            self.assertEqual(
                (directory / "scripts/run.py").read_bytes(), b"uploaded script"
            )
