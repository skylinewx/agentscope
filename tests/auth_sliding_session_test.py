"""Rolling login sessions: real middleware, cookies and isolated Redis CAS."""

import asyncio
import time
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch, AsyncMock

from fastapi import HTTPException
from fastapi.testclient import TestClient

from agentscope.app.auth import AuthService, AuthSettings
from agentscope.app.auth import _service, _store
from agentscope.app.auth._http import NewUser
import user_auth_test as fixtures


class SlidingSessionHTTPTest(TestCase):
    def test_redis_timeout_does_not_log_out_the_user(self):
        from redis.exceptions import TimeoutError
        with patch.object(self.auth, 'authenticate', AsyncMock(side_effect=TimeoutError())):
            response = self.client.get('/auth/me')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['detail']['code'], 'STORAGE_TEMPORARILY_UNAVAILABLE')
        self.assertEqual(self.client.cookies.get(self.auth.cookie_name), self.token)

    def setUp(self):
        directory = Path(__file__).resolve().parents[1] / ".cache/auth-tests"
        directory.mkdir(parents=True, exist_ok=True)
        workdir = self.enterContext(tempfile.TemporaryDirectory(dir=directory))
        app, self.auth = fixtures.make_app(workdir)

        @app.get("/test/invalid-input")
        async def invalid_input():
            raise HTTPException(400, "Invalid business input")

        self.client = self.enterContext(TestClient(app))
        login = self.client.post(
            "/auth/login",
            json={"username": "admin", "password": fixtures.ADMIN_PASSWORD},
            headers=fixtures.ORIGIN,
        )
        self.assertEqual(login.status_code, 200)
        self.csrf = login.json()["csrf_token"]
        self.token = self.client.cookies.get(self.auth.cookie_name)
        self.ttl = self.auth.settings.session_ttl_seconds

    def session(self):
        return self.client.portal.call(self.auth.store.session, self.token)

    def shorten_session(self):
        session = self.session()
        session["expires_at"] = time.time() + 300
        self.client.portal.call(
            self.auth.store.save_session, self.token, session, 300
        )
        return session

    def assert_cookie_renewed(self, response):
        self.assertIn(f"Max-Age={self.ttl}", response.headers["set-cookie"])
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=lax", response.headers["set-cookie"])
        self.assertEqual(
            self.client.cookies.get(self.auth.cookie_name), self.token
        )

    def test_request_renews_redis_cookie_without_rotating_identity(self):
        old = self.shorten_session()
        response = self.client.get("/auth/me")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["csrf_token"], self.csrf)
        self.assert_cookie_renewed(response)
        saved = self.session()
        self.assertGreater(saved["expires_at"], old["expires_at"])
        self.assertAlmostEqual(
            saved["expires_at"], time.time() + self.ttl, delta=2
        )
        redis_ttl = self.client.portal.call(
            self.auth.store.connection.ttl,
            self.auth.store.session_key(self.token),
        )
        self.assertGreaterEqual(redis_ttl, self.ttl - 2)
        self.assertLessEqual(redis_ttl, self.ttl + 1)

    def test_activity_keeps_session_past_original_deadline_then_idle_expires(
        self,
    ):
        issued = self.session()["expires_at"] - self.ttl
        for hours in (10, 20, 31):
            instant = issued + hours * 3600
            clock = SimpleNamespace(time=lambda: instant)
            with (
                patch.object(_store, "time", clock),
                patch.object(_service, "time", clock),
            ):
                response = self.client.get("/auth/me")
            self.assertEqual(response.status_code, 200)
            self.assert_cookie_renewed(response)
            self.assertEqual(self.session()["expires_at"], instant + self.ttl)
        # Capture the deadline outside the portal's event loop.
        expired = self.session()["expires_at"] + 1
        clock = SimpleNamespace(time=lambda: expired)
        with patch.object(_service, "time", clock):
            response = self.client.get("/auth/me")
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("set-cookie", response.headers)

    def test_public_and_invalid_requests_do_not_renew(self):
        saved = self.shorten_session()
        for method, path, headers, expected in (
            ("GET", "/branding", {}, 200),
            ("POST", "/test/upload", {}, 403),
            ("POST", "/test/upload", fixtures.ORIGIN, 403),
        ):
            with self.subTest(path=path, headers=list(headers)):
                response = self.client.request(method, path, headers=headers)
                self.assertEqual(response.status_code, expected)
                self.assertNotIn("set-cookie", response.headers)
                self.assertEqual(self.session(), saved)

    def test_authenticated_business_error_still_counts_as_activity(self):
        saved = self.shorten_session()
        response = self.client.get("/test/invalid-input")
        self.assertEqual(response.status_code, 400)
        self.assert_cookie_renewed(response)
        self.assertGreater(self.session()["expires_at"], saved["expires_at"])

    def test_stream_and_upload_responses_renew_cookie(self):
        self.shorten_session()
        response = self.client.get("/test/events")
        self.assertEqual(response.status_code, 200)
        self.assert_cookie_renewed(response)
        self.shorten_session()
        response = self.client.post(
            "/test/upload",
            files={"file": ("test.txt", b"hello")},
            headers={**fixtures.ORIGIN, "X-CSRF-Token": self.csrf},
        )
        self.assertEqual(response.status_code, 200)
        self.assert_cookie_renewed(response)

    def test_logout_cookie_deletion_is_not_overwritten(self):
        response = self.client.post(
            "/auth/logout",
            headers={**fixtures.ORIGIN, "X-CSRF-Token": self.csrf},
        )
        self.assertEqual(response.status_code, 204)
        cookies = response.headers.get_list("set-cookie")
        self.assertEqual(len(cookies), 1)
        self.assertIn("Max-Age=0", cookies[0])
        self.assertIsNone(self.session())
        response = self.client.get(
            "/auth/me", headers={"Cookie": f"agentscope_session={self.token}"}
        )
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("set-cookie", response.headers)
        self.assertIsNone(self.session())


class SlidingSessionConcurrencyTest(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.auth = await AuthService(
            fixtures.FakeAuthStore(),
            AuthSettings(admin_password=fixtures.ADMIN_PASSWORD),
        ).__aenter__()
        self.addAsyncCleanup(self.auth.__aexit__, None, None, None)
        self.user = await self.auth.create_user(
            NewUser(username="alice", password=fixtures.USER_PASSWORD)
        )
        self.token, self.initial, _ = await self.auth.login(
            "alice", fixtures.USER_PASSWORD, "test"
        )

    async def test_concurrent_requests_never_shorten_deadline(self):
        # A response computed earlier must not overwrite a later renewal.
        self.initial["expires_at"] += 60
        await self.auth.store.save_session(self.token, self.initial, 43260)
        results = await asyncio.gather(
            *(self.auth.renew_session(self.token) for _ in range(8))
        )
        self.assertTrue(all(result is not None for result in results))
        saved = await self.auth.store.session(self.token)
        self.assertGreaterEqual(
            saved["expires_at"], self.initial["expires_at"]
        )
        self.assertEqual(saved["csrf_token"], self.initial["csrf_token"])

    async def race_revocation(self, revoke):
        client = self.auth.store.connection
        original_pipeline = client.pipeline
        reached, release = asyncio.Event(), asyncio.Event()
        first = True

        def pipeline(*args, **kwargs):
            nonlocal first
            pipe = original_pipeline(*args, **kwargs)
            if first:
                first = False
                execute = pipe.execute

                async def delayed_execute(*args, **kwargs):
                    reached.set()
                    await release.wait()
                    return await execute(*args, **kwargs)

                pipe.execute = delayed_execute
            return pipe

        with patch.object(client, "pipeline", side_effect=pipeline):
            renewal = asyncio.create_task(self.auth.renew_session(self.token))
            try:
                await asyncio.wait_for(reached.wait(), 3)
                await revoke()
                release.set()
                self.assertIsNone(await renewal)
            finally:
                release.set()
                await asyncio.gather(renewal, return_exceptions=True)

    async def test_logout_during_renewal_cannot_restore_session(self):
        await self.race_revocation(
            lambda: self.auth.store.delete_session(self.token)
        )
        self.assertIsNone(await self.auth.store.session(self.token))

    async def test_password_reset_during_renewal_cannot_restore_session(self):
        await self.race_revocation(
            lambda: self.auth.edit_user(
                self.user["id"], {"password_hash": "reset"}
            )
        )
        self.assertIsNone(await self.auth.authenticate(self.token))

    async def test_disable_during_renewal_cannot_restore_session(self):
        await self.race_revocation(
            lambda: self.auth.edit_user(
                self.user["id"], {"status": "disabled"}
            )
        )
        self.assertIsNone(await self.auth.authenticate(self.token))

    async def test_expired_session_is_not_renewed(self):
        self.initial["expires_at"] = time.time() - 1
        await self.auth.store.save_session(self.token, self.initial, 60)
        self.assertIsNone(await self.auth.renew_session(self.token))
        self.assertEqual(
            await self.auth.store.session(self.token), self.initial
        )

    async def test_internal_stream_checks_do_not_renew(self):
        await self.auth.authenticate(self.token)
        await self.auth.guard_request(self.token, asyncio.sleep(2.1))
        self.assertEqual(
            await self.auth.store.session(self.token), self.initial
        )

    async def test_stream_survives_renewal_but_stops_after_revocation(self):
        ended = asyncio.Event()

        async def stream():
            try:
                await asyncio.Event().wait()
            finally:
                ended.set()

        task = asyncio.create_task(
            self.auth.guard_request(self.token, stream())
        )
        try:
            await self.auth.renew_session(self.token)
            await asyncio.sleep(2.1)
            self.assertFalse(task.done())
            await self.auth.store.delete_session(self.token)
            with self.assertRaises(HTTPException):
                await asyncio.wait_for(task, 3)
            self.assertTrue(ended.is_set())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
