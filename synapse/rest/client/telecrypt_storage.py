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

import logging
import re
from http import HTTPStatus
from io import IOBase
from typing import Iterable

from synapse.api.errors import Codes, NotFoundError, SynapseError
from synapse.config._base import ConfigError
from synapse.http.servlet import RestServlet, parse_json_object_from_request
from synapse.http.site import SynapseRequest
from synapse.media._base import FileInfo, ThumbnailInfo
from synapse.media.filepath import _validate_path_component
from synapse.media.media_storage import MediaStorage
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.stringutils import parse_and_validate_mxc_uri

logger = logging.getLogger(__name__)

MAX_DELETE_MEDIA_IDS = 128
MAX_DELETE_REQUEST_BYTES = 32 * 1024


def enforce_delete_body_limit(request: SynapseRequest) -> None:
    """Reject oversized JSON before the general parser consumes it."""

    content_length = request.getHeader("Content-Length")
    if content_length is None:
        raise SynapseError(
            HTTPStatus.BAD_REQUEST,
            "Content-Length is required",
            Codes.BAD_JSON,
        )
    try:
        advertised_size = int(content_length)
    except (TypeError, ValueError):
        raise SynapseError(
            HTTPStatus.BAD_REQUEST,
            "Content-Length value is invalid",
            Codes.BAD_JSON,
        ) from None
    if advertised_size < 0 or advertised_size > MAX_DELETE_REQUEST_BYTES:
        raise SynapseError(
            HTTPStatus.BAD_REQUEST,
            "Request body exceeds the 32 KiB limit",
            Codes.TOO_LARGE,
        )

    content = request.content
    if isinstance(content, IOBase) and content.seekable():
        current_position = content.tell()
        content.seek(0, 2)
        actual_size = content.tell()
        content.seek(current_position)
        if actual_size > MAX_DELETE_REQUEST_BYTES:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "Request body exceeds the 32 KiB limit",
                Codes.TOO_LARGE,
            )


def parse_delete_media_ids(body: JsonDict, hs: HomeServer) -> list[str]:
    """Validate and canonicalize the endpoint's local MXC identifiers."""

    if set(body) != {"media_ids"}:
        raise SynapseError(
            HTTPStatus.BAD_REQUEST,
            "Request must contain only media_ids",
            Codes.BAD_JSON,
        )

    media_ids = body["media_ids"]
    if type(media_ids) is not list or not 1 <= len(media_ids) <= MAX_DELETE_MEDIA_IDS:
        raise SynapseError(
            HTTPStatus.BAD_REQUEST,
            "media_ids must contain between 1 and 128 identifiers",
            Codes.BAD_JSON,
        )

    canonical_ids: list[str] = []
    for media_uri in media_ids:
        if type(media_uri) is not str:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "media_ids must contain strings",
                Codes.BAD_JSON,
            )

        try:
            host, port, media_id = parse_and_validate_mxc_uri(media_uri)
        except ValueError:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "media_ids must contain valid MXC identifiers",
                Codes.BAD_JSON,
            ) from None

        try:
            _validate_path_component(media_id)
        except ValueError:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "media_ids contain an invalid local media identifier",
                Codes.BAD_JSON,
            ) from None

        origin = host if port is None else f"{host}:{port}"
        if not hs.is_mine_server_name(origin):
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "media_ids must refer to local media",
                Codes.INVALID_PARAM,
            )

        canonical_ids.append(media_id)

    if len(set(canonical_ids)) != len(canonical_ids):
        raise SynapseError(
            HTTPStatus.BAD_REQUEST,
            "media_ids must be unique",
            Codes.BAD_JSON,
        )

    return canonical_ids


def build_media_file_infos(
    media_id: str, thumbnails: Iterable[ThumbnailInfo]
) -> list[FileInfo]:
    """Return the canonical original followed by every stored thumbnail."""

    return [
        FileInfo(server_name=None, file_id=media_id),
        *[
            FileInfo(server_name=None, file_id=media_id, thumbnail=thumbnail)
            for thumbnail in thumbnails
        ],
    ]


class TelecryptDeleteMediaServlet(RestServlet):
    """Delete user-owned local media from all configured storage providers."""

    PATTERNS = [
        re.compile(r"^/_matrix/client/unstable/io\.telecrypt\.storage/delete_media$")
    ]

    def __init__(self, hs: HomeServer):
        super().__init__()
        self.auth = hs.get_auth()
        self.store = hs.get_datastores().main
        self.media_repo = hs.get_media_repository()
        self.media_storage: MediaStorage = self.media_repo.media_storage
        self.hs = hs
        if (
            not hs.config.media.enable_local_media_storage
            and self.media_storage.storage_providers
            and not self.media_storage.deletion_supported
        ):
            raise ConfigError(
                "TeleCrypt media deletion requires a configured provider with "
                "delete support"
            )

    async def on_POST(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request, allow_guest=False)
        enforce_delete_body_limit(request)
        body = parse_json_object_from_request(request)
        media_ids = parse_delete_media_ids(body, self.hs)
        user_id = requester.user.to_string()

        async with self.media_repo.local_media_upload_linearizer.queue(user_id):
            file_infos: list[FileInfo] = []
            existing_media_ids: list[str] = []
            for media_id in media_ids:
                media = await self.store.get_local_media(media_id)
                if media is None:
                    continue
                if media.url_cache:
                    raise SynapseError(
                        HTTPStatus.BAD_REQUEST,
                        "URL-cache media cannot be deleted by this endpoint",
                        Codes.INVALID_PARAM,
                    )
                if media.user_id != user_id:
                    raise NotFoundError("Media not found")

                thumbnails = await self.store.get_local_media_thumbnails(media_id)
                file_infos.extend(build_media_file_infos(media_id, thumbnails))
                existing_media_ids.append(media_id)

            try:
                await self.media_storage.delete_files(file_infos)
            except Exception:
                logger.exception("Failed to delete requested media from storage")
                raise SynapseError(
                    HTTPStatus.BAD_GATEWAY,
                    "Media storage deletion failed",
                    Codes.UNKNOWN,
                ) from None

            await self.store.delete_local_media(existing_media_ids)
        return HTTPStatus.NO_CONTENT, {}
