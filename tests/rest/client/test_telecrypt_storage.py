#
# This file is licensed under the Affero General Public License version 3.
#
# Copyright (C) 2026 TeleCrypt
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#

import json
from contextlib import asynccontextmanager
from io import BytesIO
from typing import Any, AsyncIterator, Generator, cast

from twisted.internet import defer
from twisted.trial import unittest

from synapse.api.errors import Codes, NotFoundError, SynapseError
from synapse.media._base import FileInfo, ThumbnailInfo
from synapse.rest.client.telecrypt_storage import (
    TelecryptDeleteMediaServlet,
    build_media_file_infos,
    enforce_delete_body_limit,
    parse_delete_media_ids,
)
from synapse.types import JsonDict


def _frozen_thumbnails() -> list[ThumbnailInfo]:
    return [
        ThumbnailInfo(
            width=32,
            height=32,
            method="crop",
            type="image/png",
            length=10,
        ),
        ThumbnailInfo(
            width=96,
            height=96,
            method="crop",
            type="image/png",
            length=20,
        ),
        ThumbnailInfo(
            width=320,
            height=240,
            method="scale",
            type="image/png",
            length=30,
        ),
        ThumbnailInfo(
            width=640,
            height=480,
            method="scale",
            type="image/png",
            length=40,
        ),
        ThumbnailInfo(
            width=800,
            height=600,
            method="scale",
            type="image/png",
            length=50,
        ),
    ]


class _FakeHomeServer:
    def is_mine_server_name(self, server_name: str) -> bool:
        return server_name == "example.com"


class _FakeRequest:
    def __init__(self, body: bytes, content_length: str | None = None):
        self.content = BytesIO(body)
        self._content_length = content_length

    def getHeader(self, name: str) -> str | None:
        if name == "Content-Length":
            return self._content_length
        return None


class _FakeUser:
    def to_string(self) -> str:
        return "@owner:example.com"


class _FakeRequester:
    user = _FakeUser()


class _FakeAuth:
    async def get_user_by_req(
        self, request: Any, allow_guest: bool = False
    ) -> _FakeRequester:
        return _FakeRequester()


class _FakeLock:
    def __init__(self, events: list[str]):
        self.events = events
        self.active = False

    @asynccontextmanager
    async def queue(self, user_id: str) -> AsyncIterator[None]:
        self.events.append("lock-enter")
        self.active = True
        try:
            yield
        finally:
            self.active = False
            self.events.append("lock-exit")


class _FakeStore:
    def __init__(
        self,
        lock: _FakeLock,
        events: list[str],
        media: Any = None,
        thumbnails: list[ThumbnailInfo] | None = None,
        media_by_id: dict[str, Any] | None = None,
        metadata_failure: Exception | None = None,
    ):
        self.lock = lock
        self.events = events
        self.thumbnails = thumbnails or []
        self.media_by_id = media_by_id
        self.metadata_failure = metadata_failure
        self.metadata_attempts: list[list[str]] = []
        self.deleted_media_ids: list[list[str]] = []
        self.media = (
            media
            or type(
                "Media",
                (),
                {"user_id": "@owner:example.com", "url_cache": None},
            )()
        )

    async def get_local_media(self, media_id: str) -> Any:
        assert self.lock.active
        self.events.append("lookup")
        if self.media_by_id is not None:
            return self.media_by_id.get(media_id)
        return self.media

    async def get_local_media_thumbnails(self, media_id: str) -> list[ThumbnailInfo]:
        assert self.lock.active
        return self.thumbnails

    async def delete_local_media(self, media_ids: list[str]) -> None:
        assert self.lock.active
        self.metadata_attempts.append(list(media_ids))
        if self.metadata_failure is not None:
            raise self.metadata_failure
        self.deleted_media_ids.append(list(media_ids))
        self.events.append("metadata-delete")


class _FakeStorage:
    def __init__(
        self,
        lock: _FakeLock,
        events: list[str],
        failure: Exception | None = None,
    ):
        self.lock = lock
        self.events = events
        self.failure = failure
        self.file_infos: list[FileInfo] = []
        self.delete_calls: list[list[FileInfo]] = []

    async def delete_files(self, file_infos: list[FileInfo]) -> None:
        assert self.lock.active
        self.file_infos = file_infos
        self.delete_calls.append(file_infos)
        assert len(file_infos) == 6
        self.events.append("physical-delete")
        if self.failure is not None:
            raise self.failure


class _FakeMediaRepository:
    def __init__(self, lock: _FakeLock, storage: _FakeStorage):
        self.local_media_upload_linearizer = lock
        self.media_storage = storage


class TelecryptStorageValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hs: Any = _FakeHomeServer()

    def test_accepts_unique_local_mxc_identifiers(self) -> None:
        self.assertEqual(
            parse_delete_media_ids(
                {"media_ids": ["mxc://example.com/first", "mxc://example.com/second"]},
                self.hs,
            ),
            ["first", "second"],
        )

    def test_rejects_remote_identifiers(self) -> None:
        with self.assertRaises(SynapseError):
            parse_delete_media_ids(
                {"media_ids": ["mxc://remote.example/file"]}, self.hs
            )

    def test_rejects_duplicate_identifiers(self) -> None:
        with self.assertRaises(SynapseError):
            parse_delete_media_ids(
                {"media_ids": ["mxc://example.com/file", "mxc://example.com/file"]},
                self.hs,
            )

    def test_rejects_extra_request_fields(self) -> None:
        with self.assertRaises(SynapseError):
            parse_delete_media_ids(
                {"media_ids": ["mxc://example.com/file"], "s3_key": "wrong"},
                self.hs,
            )

    def test_resolves_original_and_all_thumbnail_keys(self) -> None:
        infos = build_media_file_infos("file", _frozen_thumbnails())

        self.assertEqual(len(infos), 6)
        self.assertEqual(infos[0].file_id, "file")
        self.assertIsNone(infos[0].thumbnail)
        self.assertEqual(infos[1].file_id, "file")
        thumbnail_dimensions: list[tuple[int, int, str]] = []
        for info in infos[1:]:
            assert info.thumbnail is not None
            thumbnail_dimensions.append(
                (info.thumbnail.width, info.thumbnail.height, info.thumbnail.method)
            )
        self.assertEqual(
            thumbnail_dimensions,
            [
                (32, 32, "crop"),
                (96, 96, "crop"),
                (320, 240, "scale"),
                (640, 480, "scale"),
                (800, 600, "scale"),
            ],
        )

    def test_rejects_oversized_delete_request_body(self) -> None:
        with self.assertRaises(SynapseError):
            enforce_delete_body_limit(
                cast(
                    Any,
                    _FakeRequest(
                        b"x" * (32 * 1024 + 1),
                        content_length=str(32 * 1024 + 1),
                    ),
                )
            )

    def test_rejects_delete_request_without_content_length(self) -> None:
        with self.assertRaises(SynapseError):
            enforce_delete_body_limit(cast(Any, _FakeRequest(b"{}")))

    def test_rejects_body_larger_than_advertised_limit(self) -> None:
        with self.assertRaises(SynapseError):
            enforce_delete_body_limit(
                cast(Any, _FakeRequest(b"x" * (32 * 1024 + 1), content_length="1"))
            )

    @defer.inlineCallbacks
    def test_delete_serializes_lookup_storage_and_metadata(
        self,
    ) -> Generator["defer.Deferred[Any]", object, None]:
        events: list[str] = []
        lock = _FakeLock(events)
        storage = _FakeStorage(lock, events)
        servlet: Any = TelecryptDeleteMediaServlet.__new__(TelecryptDeleteMediaServlet)
        servlet.auth = _FakeAuth()
        servlet.store = _FakeStore(
            lock,
            events,
            thumbnails=_frozen_thumbnails(),
        )
        servlet.media_repo = _FakeMediaRepository(lock, storage)
        servlet.media_storage = storage
        servlet.hs = _FakeHomeServer()

        body = json.dumps({"media_ids": ["mxc://example.com/file"]}).encode()
        request = _FakeRequest(body, content_length=str(len(body)))
        result = cast(
            tuple[int, JsonDict],
            (yield defer.ensureDeferred(servlet.on_POST(request))),
        )

        self.assertEqual(result[0], 204)
        self.assertEqual(
            events,
            [
                "lock-enter",
                "lookup",
                "physical-delete",
                "metadata-delete",
                "lock-exit",
            ],
        )
        self.assertEqual(
            [
                None
                if info.thumbnail is None
                else (
                    info.thumbnail.width,
                    info.thumbnail.height,
                    info.thumbnail.method,
                )
                for info in storage.file_infos
            ],
            [
                None,
                (32, 32, "crop"),
                (96, 96, "crop"),
                (320, 240, "scale"),
                (640, 480, "scale"),
                (800, 600, "scale"),
            ],
        )

    @defer.inlineCallbacks
    def test_delete_rejects_foreign_media_without_mutation(
        self,
    ) -> Generator["defer.Deferred[Any]", object, None]:
        events: list[str] = []
        lock = _FakeLock(events)
        storage = _FakeStorage(lock, events)
        owned_media = type(
            "Media",
            (),
            {"user_id": "@owner:example.com", "url_cache": None},
        )()
        foreign_media = type(
            "Media",
            (),
            {"user_id": "@other:example.com", "url_cache": None},
        )()
        store = _FakeStore(
            lock,
            events,
            thumbnails=_frozen_thumbnails(),
            media_by_id={"owned": owned_media, "foreign": foreign_media},
        )
        servlet: Any = TelecryptDeleteMediaServlet.__new__(TelecryptDeleteMediaServlet)
        servlet.auth = _FakeAuth()
        servlet.store = store
        servlet.media_repo = _FakeMediaRepository(lock, storage)
        servlet.media_storage = storage
        servlet.hs = _FakeHomeServer()

        body = json.dumps(
            {
                "media_ids": [
                    "mxc://example.com/owned",
                    "mxc://example.com/foreign",
                ]
            }
        ).encode()
        request = _FakeRequest(body, content_length=str(len(body)))
        error = cast(
            NotFoundError,
            (
                yield self.assertFailure(
                    defer.ensureDeferred(servlet.on_POST(request)), NotFoundError
                )
            ),
        )

        self.assertEqual(error.code, 404)
        self.assertEqual(
            events,
            ["lock-enter", "lookup", "lookup", "lock-exit"],
        )
        self.assertEqual(storage.delete_calls, [])
        self.assertEqual(store.metadata_attempts, [])
        self.assertEqual(store.deleted_media_ids, [])

    @defer.inlineCallbacks
    def test_provider_failure_returns_502_without_metadata_mutation(
        self,
    ) -> Generator["defer.Deferred[Any]", object, None]:
        events: list[str] = []
        lock = _FakeLock(events)
        storage = _FakeStorage(
            lock,
            events,
            failure=RuntimeError("provider failure"),
        )
        store = _FakeStore(
            lock,
            events,
            thumbnails=_frozen_thumbnails(),
        )
        servlet: Any = TelecryptDeleteMediaServlet.__new__(TelecryptDeleteMediaServlet)
        servlet.auth = _FakeAuth()
        servlet.store = store
        servlet.media_repo = _FakeMediaRepository(lock, storage)
        servlet.media_storage = storage
        servlet.hs = _FakeHomeServer()

        body = json.dumps({"media_ids": ["mxc://example.com/file"]}).encode()
        request = _FakeRequest(body, content_length=str(len(body)))
        error = cast(
            SynapseError,
            (
                yield self.assertFailure(
                    defer.ensureDeferred(servlet.on_POST(request)), SynapseError
                )
            ),
        )

        self.assertEqual(error.code, 502)
        self.assertEqual(error.errcode, Codes.UNKNOWN)
        self.assertEqual(
            events,
            ["lock-enter", "lookup", "physical-delete", "lock-exit"],
        )
        self.assertEqual(len(storage.delete_calls), 1)
        self.assertEqual(store.metadata_attempts, [])
        self.assertEqual(store.deleted_media_ids, [])

    @defer.inlineCallbacks
    def test_database_failure_after_provider_delete_is_retryable(
        self,
    ) -> Generator["defer.Deferred[Any]", object, None]:
        events: list[str] = []
        lock = _FakeLock(events)
        storage = _FakeStorage(lock, events)
        store = _FakeStore(
            lock,
            events,
            thumbnails=_frozen_thumbnails(),
            metadata_failure=RuntimeError("database failure"),
        )
        servlet: Any = TelecryptDeleteMediaServlet.__new__(TelecryptDeleteMediaServlet)
        servlet.auth = _FakeAuth()
        servlet.store = store
        servlet.media_repo = _FakeMediaRepository(lock, storage)
        servlet.media_storage = storage
        servlet.hs = _FakeHomeServer()

        body = json.dumps({"media_ids": ["mxc://example.com/file"]}).encode()
        request = _FakeRequest(body, content_length=str(len(body)))
        yield self.assertFailure(
            defer.ensureDeferred(servlet.on_POST(request)), RuntimeError
        )

        self.assertEqual(len(storage.delete_calls), 1)
        self.assertEqual(store.metadata_attempts, [["file"]])
        self.assertEqual(store.deleted_media_ids, [])

        store.metadata_failure = None
        retry_request = _FakeRequest(body, content_length=str(len(body)))
        result = cast(
            tuple[int, JsonDict],
            (yield defer.ensureDeferred(servlet.on_POST(retry_request))),
        )

        self.assertEqual(result[0], 204)
        self.assertEqual(len(storage.delete_calls), 2)
        self.assertEqual(store.metadata_attempts, [["file"], ["file"]])
        self.assertEqual(store.deleted_media_ids, [["file"]])
        self.assertEqual(
            events,
            [
                "lock-enter",
                "lookup",
                "physical-delete",
                "lock-exit",
                "lock-enter",
                "lookup",
                "physical-delete",
                "metadata-delete",
                "lock-exit",
            ],
        )

    @defer.inlineCallbacks
    def test_delete_rejects_url_cache_before_owner_lookup(
        self,
    ) -> Generator["defer.Deferred[Any]", object, None]:
        events: list[str] = []
        lock = _FakeLock(events)
        storage = _FakeStorage(lock, events)
        url_cache_media = type(
            "Media",
            (),
            {"user_id": None, "url_cache": "https://example.invalid/image"},
        )()
        servlet: Any = TelecryptDeleteMediaServlet.__new__(TelecryptDeleteMediaServlet)
        servlet.auth = _FakeAuth()
        servlet.store = _FakeStore(lock, events, url_cache_media)
        servlet.media_repo = _FakeMediaRepository(lock, storage)
        servlet.media_storage = storage
        servlet.hs = _FakeHomeServer()

        body = json.dumps({"media_ids": ["mxc://example.com/cache"]}).encode()
        request = _FakeRequest(body, content_length=str(len(body)))
        error = cast(
            SynapseError,
            (
                yield self.assertFailure(
                    defer.ensureDeferred(servlet.on_POST(request)), SynapseError
                )
            ),
        )

        self.assertEqual(error.code, 400)
        self.assertEqual(error.errcode, Codes.INVALID_PARAM)
        self.assertEqual(events, ["lock-enter", "lookup", "lock-exit"])
