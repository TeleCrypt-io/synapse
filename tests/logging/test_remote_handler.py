#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 The Matrix.org Foundation C.I.C.
# Copyright (C) 2023 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
# Originally licensed under the Apache License, Version 2.0:
# <http://www.apache.org/licenses/LICENSE-2.0>.
#
# [This file includes modifications made by New Vector Limited]
#
#

from twisted.internet.protocol import Protocol
from twisted.internet.testing import AccumulatingProtocol, MemoryReactorClock

from synapse.logging import RemoteHandler

from tests.logging import LoggerCleanupMixin
from tests.server import FakeTransport, get_clock
from tests.unittest import TestCase
from tests.utils import checked_cast


def connect_logging_client(
    reactor: MemoryReactorClock, client_id: int
) -> tuple[Protocol, AccumulatingProtocol]:
    # This is essentially tests.server.connect_client, but disabling autoflush on
    # the client transport. This is necessary to avoid an infinite loop due to
    # sending of data via the logging transport causing additional logs to be
    # written.
    factory = reactor.tcpClients.pop(client_id)[2]
    client = factory.buildProtocol(None)
    server = AccumulatingProtocol()
    server.makeConnection(FakeTransport(client, reactor))
    client.makeConnection(FakeTransport(server, reactor, autoflush=False))

    return client, server


class RemoteHandlerTestCase(LoggerCleanupMixin, TestCase):
    def setUp(self) -> None:
        self.reactor, _ = get_clock()

    def test_log_output(self) -> None:
        """
        The remote handler delivers logs over TCP.
        """
        handler = RemoteHandler("127.0.0.1", 9000, _reactor=self.reactor)
        logger = self.get_logger(handler)

        logger.info("Hello there, %s!", "wally")

        # Trigger the connection
        client, server = connect_logging_client(self.reactor, 0)

        # Trigger data being sent
        client_transport = checked_cast(FakeTransport, client.transport)
        client_transport.flush()

        # One log message, with a single trailing newline
        logs = server.data.decode("utf8").splitlines()
        self.assertEqual(len(logs), 1)
        self.assertEqual(server.data.count(b"\n"), 1)

        # Ensure the data passed through properly.
        self.assertEqual(logs[0], "Hello there, wally!")

    def test_log_output_is_not_shed_on_backpressure(self) -> None:
        """The remote handler preserves every record while delivery is delayed."""
        handler = RemoteHandler("127.0.0.1", 9000, _reactor=self.reactor)
        logger = self.get_logger(handler)

        for i in range(20):
            logger.warning("warn %s", i)

        # Allow the reconnection
        client, server = connect_logging_client(self.reactor, 0)
        client_transport = checked_cast(FakeTransport, client.transport)
        client_transport.flush()

        logs = server.data.decode("utf8").splitlines()
        self.assertEqual(["warn %s" % (i,) for i in range(20)], logs)

    def test_cancel_connection(self) -> None:
        """
        Gracefully handle the connection being cancelled.
        """
        handler = RemoteHandler("127.0.0.1", 9000, _reactor=self.reactor)
        logger = self.get_logger(handler)

        # Send a message.
        logger.info("Hello there, %s!", "wally")

        # Do not accept the connection and shutdown. This causes the pending
        # connection to be cancelled (and should not raise any exceptions).
        handler.close()
