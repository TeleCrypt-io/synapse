# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.

import logging
from typing import Any
from unittest.mock import patch

from twisted.internet.testing import MemoryReactor

from synapse.logging.context import current_context
from synapse.rest import admin
from synapse.rest.client import login, versions
from synapse.server import HomeServer
from synapse.synapse_rust.http_client import HttpClient
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests import unittest

logger = logging.getLogger(__name__)


class VersionsTestCase(unittest.HomeserverTestCase):
    """
    Test `VersionsRestServlet`
    """

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        versions.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        hs = self.setup_test_homeserver()

        # XXX: We must create the Rust HTTP client before we call `reactor.run()` below.
        # Twisted's `MemoryReactor` doesn't invoke `callWhenRunning` callbacks if it's
        # already running and we rely on that to start the Tokio thread pool in Rust. In
        # the future, this may not matter, see https://github.com/twisted/twisted/pull/12514
        self._http_client = hs.get_proxied_http_client()
        _ = HttpClient(
            reactor=hs.get_reactor(),
            user_agent=self._http_client.user_agent.decode("utf8"),
        )

        # This triggers the server startup hooks, which starts the Tokio thread pool
        reactor.run()

        return hs

    def tearDown(self) -> None:
        # MemoryReactor doesn't trigger the shutdown phases, and we want the
        # Tokio thread pool to be stopped
        # XXX: This logic should probably get moved somewhere else
        shutdown_triggers = self.reactor.triggers.get("shutdown", {})
        for phase in ["before", "during", "after"]:
            triggers = shutdown_triggers.get(phase, [])
            for callbable, args, kwargs in triggers:
                callbable(*args, **kwargs)

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.admin_user = self.register_user("admin", "pass", admin=True)
        self.admin_user_tok = self.login("admin", "pass")

    @unittest.override_config(
        {
            "experimental_features": {
                "msc3881_enabled": False,
                "msc3575_enabled": False,
            }
        }
    )
    def test_anonymous_global_flags_false(self) -> None:
        self._assert_feature_flags(self._request_versions(), False)

    @unittest.override_config(
        {
            "experimental_features": {
                "msc3881_enabled": True,
                "msc3575_enabled": True,
            }
        }
    )
    def test_anonymous_global_flags_true(self) -> None:
        self._assert_feature_flags(self._request_versions(), True)

    @unittest.override_config(
        {
            "experimental_features": {
                "msc3575_enabled": False,
                "msc3881_enabled": False,
            }
        }
    )
    def test_per_user_feature_lookup_uses_request_context(self) -> None:
        """Per-user flag lookups use the request context and Python store."""
        db_pool = self.hs.get_datastores().main.db_pool
        seen_interactions: list[tuple[str, object]] = []
        original_run_interaction = db_pool.runInteraction

        async def run_interaction(desc: str, *args: Any, **kwargs: Any) -> Any:
            seen_interactions.append((desc, current_context()))
            return await original_run_interaction(desc, *args, **kwargs)

        with patch.object(db_pool, "runInteraction", new=run_interaction):
            channel = self.make_request(
                "GET",
                "/_matrix/client/versions",
                access_token=self.admin_user_tok,
            )

        self.assertEqual(channel.code, 200, channel.result)
        self._sanity_check_versions_response(channel.json_body)
        self._assert_feature_flags(channel.json_body, False)
        self.assertEqual(
            [desc for desc, _ in seen_interactions],
            ["get_feature_enabled", "get_feature_enabled"],
        )
        self.assertTrue(
            all(
                context is channel.request.logcontext
                for _, context in seen_interactions
            )
        )

    @unittest.override_config(
        {
            "experimental_features": {
                "msc3881_enabled": False,
                "msc3575_enabled": False,
            }
        }
    )
    def test_authenticated_with_per_user_features(self) -> None:
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        self._assert_feature_flags(self._request_versions(user1_tok), False)

        self._enable_experimental_feature_for_user(
            target_user_id=user1_id,
            features={"msc3881": True, "msc3575": True},
        )
        self._assert_feature_flags(self._request_versions(user1_tok), True)

        self._enable_experimental_feature_for_user(
            target_user_id=user1_id,
            features={"msc3881": False, "msc3575": False},
        )
        self._assert_feature_flags(self._request_versions(user1_tok), False)

    @unittest.override_config(
        {
            "experimental_features": {
                "msc3881_enabled": True,
                "msc3575_enabled": True,
            }
        }
    )
    def test_global_flags_override_per_user_false(self) -> None:
        user_id = self.register_user("user1", "pass")
        user_tok = self.login(user_id, "pass")
        self._enable_experimental_feature_for_user(
            target_user_id=user_id,
            features={"msc3881": False, "msc3575": False},
        )
        self._assert_feature_flags(self._request_versions(user_tok), True)

    def test_msc4446_false_by_default(self) -> None:
        channel = self.make_request("GET", "/_matrix/client/versions")
        self.assertEqual(channel.code, 200, channel.result)
        self.assertFalse(channel.json_body["unstable_features"]["com.beeper.msc4446"])

    @unittest.override_config({"experimental_features": {"msc4446_enabled": True}})
    def test_msc4446_true_if_enabled(self) -> None:
        channel = self.make_request("GET", "/_matrix/client/versions")
        self.assertEqual(channel.code, 200, channel.result)
        self.assertTrue(channel.json_body["unstable_features"]["com.beeper.msc4446"])

    def _sanity_check_versions_response(self, versions_response: JsonDict) -> None:
        """
        Make sure this looks like a `/_matrix/client/versions` response
        """
        self.assertIsInstance(
            versions_response["versions"],
            list,
            f"Expected `versions` to be a list of strings but saw {versions_response}",
        )
        self.assertIsInstance(
            versions_response["unstable_features"],
            dict,
            f"Expected `unstable_features` to be a dict mapping feature name to a bool but saw {versions_response}",
        )

    def _request_versions(self, access_token: str | None = None) -> JsonDict:
        channel = self.make_request(
            "GET", "/_matrix/client/versions", access_token=access_token
        )
        self.assertEqual(channel.code, 200, channel.result)
        self._sanity_check_versions_response(channel.json_body)
        return channel.json_body

    def _assert_feature_flags(self, response: JsonDict, expected: bool) -> None:
        unstable_features = response["unstable_features"]
        self.assertEqual(unstable_features["org.matrix.msc3881"], expected, response)
        self.assertEqual(
            unstable_features["org.matrix.simplified_msc3575"], expected, response
        )
        self.assertNotIn("org.matrix.msc3575", unstable_features)

    def _enable_experimental_feature_for_user(
        self, *, target_user_id: str, features: dict[str, bool]
    ) -> None:
        """
        Use the admin API to enable an experimental feature for a specific user
        """
        channel = self.make_request(
            "PUT",
            f"/_synapse/admin/v1/experimental_features/{target_user_id}",
            content={
                "features": features,
            },
            access_token=self.admin_user_tok,
        )
        self.assertEqual(channel.code, 200)
