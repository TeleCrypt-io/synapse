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

from twisted.internet import defer
from twisted.trial import unittest

from synapse.api.errors import SynapseError
from synapse.media._base import FileInfo, ThumbnailInfo
from synapse.rest.client.telecrypt_storage import (
    TelecryptDeleteMediaServlet,
    build_media_file_infos,
    enforce_delete_body_limit,
    parse_delete_media_ids,
)


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
    async def get_user_by_req(self, request, allow_guest=False):
        return _FakeRequester()


class _FakeLock:
    def __init__(self, events: list[str]):
        self.events = events
        self.active = False

    @asynccontextmanager
    async def queue(self, user_id: str):
        self.events.append("lock-enter")
        self.active = True
        try:
            yield
        finally:
            self.active = False
            self.events.append("lock-exit")


class _FakeStore:
    def __init__(self, lock: _FakeLock, events: list[str]):
        self.lock = lock
        self.events = events

    async def get_local_media(self, media_id: str):
        assert self.lock.active
        self.events.append("lookup")
        return type(
            "Media",
            (),
            {"user_id": "@owner:example.com", "url_cache": None},
        )()

    async def get_local_media_thumbnails(self, media_id: str):
        assert self.lock.active
        return []

    async def delete_local_media(self, media_ids):
        assert self.lock.active
        self.events.append("metadata-delete")


class _FakeStorage:
    def __init__(self, lock: _FakeLock, events: list[str]):
        self.lock = lock
        self.events = events

    async def delete_files(self, file_infos: list[FileInfo]) -> None:
        assert self.lock.active
        assert len(file_infos) == 1
        self.events.append("physical-delete")


class _FakeMediaRepository:
    def __init__(self, lock: _FakeLock, storage: _FakeStorage):
        self.local_media_upload_linearizer = lock
        self.media_storage = storage


class TelecryptStorageValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hs = _FakeHomeServer()

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
        infos = build_media_file_infos(
            "file",
            [
                ThumbnailInfo(
                    width=64,
                    height=64,
                    method="scale",
                    type="image/png",
                    length=10,
                )
            ],
        )

        self.assertEqual(len(infos), 2)
        self.assertEqual(infos[0].file_id, "file")
        self.assertIsNone(infos[0].thumbnail)
        self.assertEqual(infos[1].file_id, "file")
        self.assertEqual(infos[1].thumbnail.width, 64)

    def test_rejects_oversized_delete_request_body(self) -> None:
        with self.assertRaises(SynapseError):
            enforce_delete_body_limit(
                _FakeRequest(
                    b"x" * (32 * 1024 + 1),
                    content_length=str(32 * 1024 + 1),
                )
            )

    def test_rejects_delete_request_without_content_length(self) -> None:
        with self.assertRaises(SynapseError):
            enforce_delete_body_limit(_FakeRequest(b"{}"))

    def test_rejects_body_larger_than_advertised_limit(self) -> None:
        with self.assertRaises(SynapseError):
            enforce_delete_body_limit(
                _FakeRequest(b"x" * (32 * 1024 + 1), content_length="1")
            )

    @defer.inlineCallbacks
    def test_delete_serializes_lookup_storage_and_metadata(self) -> None:
        events: list[str] = []
        lock = _FakeLock(events)
        storage = _FakeStorage(lock, events)
        servlet = TelecryptDeleteMediaServlet.__new__(TelecryptDeleteMediaServlet)
        servlet.auth = _FakeAuth()
        servlet.store = _FakeStore(lock, events)
        servlet.media_repo = _FakeMediaRepository(lock, storage)
        servlet.media_storage = storage
        servlet.hs = _FakeHomeServer()

        body = json.dumps({"media_ids": ["mxc://example.com/file"]}).encode()
        request = _FakeRequest(body, content_length=str(len(body)))
        result = yield defer.ensureDeferred(servlet.on_POST(request))

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
