#
# This file is licensed under the Affero General Public License version 3.
#
# Copyright (C) 2026 TeleCrypt
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#

from contextlib import asynccontextmanager, contextmanager
from io import BytesIO
from typing import Any, Iterator
from unittest.mock import patch

from twisted.internet import defer
from twisted.internet.defer import Deferred

from synapse.rest.media.upload_resource import AsyncUploadServlet, UploadServlet
from synapse.util.async_helpers import Linearizer

from tests import unittest
from tests.server import get_clock


class _FakeUser:
    def __init__(self, user_id: str):
        self._user_id = user_id

    def to_string(self) -> str:
        return self._user_id


class _FakeRequester:
    def __init__(self, user_id: str):
        self.user = _FakeUser(user_id)


class _FakeAuth:
    async def get_user_by_req(self, request: Any) -> _FakeRequester:
        return _FakeRequester(request.user_id)


class _FakeHeaders:
    def hasHeader(self, name: bytes) -> bool:
        return name == b"Content-Type"

    def getRawHeaders(self, name: bytes) -> list[bytes]:
        return [b"application/octet-stream"]


class _FakeRequest:
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.args: dict[bytes, list[bytes]] = {}
        self.requestHeaders = _FakeHeaders()
        self.content = BytesIO(b"content")

    def getHeader(self, name: str) -> str | None:
        if name == "Content-Length":
            return "7"
        return None


class _FakeStore:
    @asynccontextmanager
    async def _lock(self) -> Any:
        yield

    async def try_acquire_lock(self, lock_name: str, lock_key: str) -> Any:
        return self._lock()


class _FakeCallbacks:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.gates: dict[str, Deferred[None]] = {}

    async def is_user_allowed_to_upload_media_of_size(
        self, user_id: str, content_length: int
    ) -> bool:
        self.started.append(user_id)
        gate = self.gates.get(user_id)
        if gate is not None:
            await gate
        return True


class _FakeMediaRepository:
    def __init__(self, clock: Any, callbacks: _FakeCallbacks):
        self.local_media_upload_linearizer = Linearizer(
            name="test_media_upload", clock=clock
        )
        self.callbacks = callbacks
        self.stages: list[tuple[str, str]] = []
        self.failure_phase: str | None = None
        self.fail_once = False
        self.put_gate: Deferred[None] | None = None
        self.put_started: list[str] = []

    async def verify_can_upload(self, media_id: str, user: _FakeUser) -> None:
        return None

    async def create_or_update_content(self, *args: Any, **kwargs: Any) -> None:
        requester = args[4]
        user_id = requester.to_string()
        if kwargs.get("media_id") is not None:
            self.put_started.append(user_id)
        put_gate = self.put_gate
        self.put_gate = None
        if put_gate is not None:
            await put_gate

        self.stages.append(("provider", user_id))
        if self.failure_phase == "provider" and self.fail_once:
            self.fail_once = False
            raise RuntimeError("provider failure")

        self.stages.append(("metadata", user_id))
        if self.failure_phase == "metadata" and self.fail_once:
            self.fail_once = False
            raise RuntimeError("metadata failure")


class UploadResourceLinearizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reactor, self.clock = get_clock()
        self.callbacks = _FakeCallbacks()
        self.media_repo = _FakeMediaRepository(self.clock, self.callbacks)
        servlet: Any = UploadServlet.__new__(UploadServlet)
        servlet.auth = _FakeAuth()
        servlet.media_repo = self.media_repo
        servlet.max_upload_size = 1024
        servlet._media_repository_callbacks = self.callbacks
        self.servlet = servlet

    def _async_servlet(self) -> Any:
        servlet: Any = AsyncUploadServlet.__new__(AsyncUploadServlet)
        servlet.auth = _FakeAuth()
        servlet.media_repo = self.media_repo
        servlet.store = _FakeStore()
        servlet.server_name = "example.com"
        servlet.max_upload_size = 1024
        servlet._media_repository_callbacks = self.callbacks
        return servlet

    @contextmanager
    def _responses(self) -> Iterator[list[tuple[Any, ...]]]:
        responses: list[tuple[Any, ...]] = []

        def respond(*args: Any, **kwargs: Any) -> None:
            responses.append(args)

        patcher = patch("synapse.rest.media.upload_resource.respond_with_json", respond)
        with patcher:
            yield responses

    def _pump(self) -> None:
        self.reactor.pump([0] * 100)

    def _start(self, user_id: str) -> "Deferred[None]":
        return defer.ensureDeferred(self.servlet.on_POST(_FakeRequest(user_id)))

    def _start_put(self, servlet: Any, user_id: str) -> "Deferred[None]":
        return defer.ensureDeferred(
            servlet.on_PUT(_FakeRequest(user_id), "example.com", "media-id")
        )

    def test_same_user_uploads_serialize_before_callback(self) -> None:
        first_gate: Deferred[None] = Deferred()
        self.callbacks.gates["@alice:example.com"] = first_gate

        with self._responses():
            first = self._start("@alice:example.com")
            self.assertFalse(first.called)
            self.assertEqual(self.callbacks.started, ["@alice:example.com"])

            second = self._start("@alice:example.com")
            self.assertFalse(second.called)
            self.assertEqual(self.callbacks.started, ["@alice:example.com"])

            first_gate.callback(None)
            self._pump()

            self.assertTrue(first.called)
            self.assertTrue(second.called)
            self.assertEqual(
                self.callbacks.started,
                ["@alice:example.com", "@alice:example.com"],
            )
            self.successResultOf(first)
            self.successResultOf(second)

    def test_different_user_uploads_are_independent(self) -> None:
        first_gate: Deferred[None] = Deferred()
        self.callbacks.gates["@alice:example.com"] = first_gate

        with self._responses():
            first = self._start("@alice:example.com")
            self.assertFalse(first.called)

            second = self._start("@bob:example.com")
            self.assertTrue(second.called)
            self.successResultOf(second)
            self.assertEqual(
                self.callbacks.started,
                ["@alice:example.com", "@bob:example.com"],
            )

            first_gate.callback(None)
            self._pump()
            self.successResultOf(first)

    def test_async_put_uses_the_shared_upload_lock(self) -> None:
        first_gate: Deferred[None] = Deferred()
        self.callbacks.gates["@alice:example.com"] = first_gate
        put_servlet = self._async_servlet()

        with self._responses():
            first = self._start("@alice:example.com")
            self.assertFalse(first.called)

            put = self._start_put(put_servlet, "@alice:example.com")
            self.assertFalse(put.called)
            self.assertEqual(self.callbacks.started, ["@alice:example.com"])

            first_gate.callback(None)
            self._pump()

            self.assertTrue(first.called)
            self.assertTrue(put.called)
            self.successResultOf(first)
            self.successResultOf(put)
            self.assertEqual(self.media_repo.put_started, ["@alice:example.com"])

    def test_async_put_cancellation_releases_shared_upload_lock(self) -> None:
        self.media_repo.put_gate = Deferred()
        put_servlet = self._async_servlet()

        with self._responses():
            cancelled = self._start_put(put_servlet, "@alice:example.com")
            self.assertFalse(cancelled.called)
            self.assertEqual(self.media_repo.put_started, ["@alice:example.com"])

            cancelled.cancel()
            self.failureResultOf(cancelled, defer.CancelledError)

            retried = self._start_put(put_servlet, "@alice:example.com")
            self.successResultOf(retried)
            self.assertEqual(
                self.media_repo.put_started,
                ["@alice:example.com", "@alice:example.com"],
            )

    def _assert_lock_released_after_failure(self, phase: str) -> None:
        self.media_repo.failure_phase = phase
        self.media_repo.fail_once = True

        with self._responses():
            failed = self._start("@alice:example.com")
            self.failureResultOf(failed, RuntimeError)

            retried = self._start("@alice:example.com")
            self.successResultOf(retried)

        self.assertEqual(
            self.callbacks.started,
            ["@alice:example.com", "@alice:example.com"],
        )
        self.assertEqual(
            self.media_repo.stages,
            [
                ("provider", "@alice:example.com"),
                *([("metadata", "@alice:example.com")] if phase == "metadata" else []),
                ("provider", "@alice:example.com"),
                ("metadata", "@alice:example.com"),
            ],
        )

    def test_lock_released_after_provider_failure(self) -> None:
        self._assert_lock_released_after_failure("provider")

    def test_lock_released_after_metadata_failure(self) -> None:
        self._assert_lock_released_after_failure("metadata")
