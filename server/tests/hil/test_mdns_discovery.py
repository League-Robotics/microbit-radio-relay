"""Discovery against the responder actually running on this machine.

Marked hil for two reasons: it needs a live daemon, and it is the only test in
the tree that puts a real query on the wire. Everything the fakes can tell us is
already covered offline; what they cannot tell us is whether *this host's*
publisher is installed, whether its responder honours RFC 6762 s6.7 the way the
design assumes, and whether the record appears and disappears with the daemon.

Skipped, rather than failed, when no publisher is installed: avahi-utils is not
on a stock Ubuntu Server, and a host without it is a supported configuration --
the daemon logs one warning and serves boards exactly as before.
"""

from __future__ import annotations

import shutil

import pytest

from mbrelay.adminclient import AdminClient
from mbrelay.mdns import PUBLISH_TOOLS, browse_detailed

from hil_support import wait_until


@pytest.fixture(scope="module")
def publisher():
    for tool in PUBLISH_TOOLS:
        if found := shutil.which(tool):
            return found
    pytest.skip(f"none of {', '.join(PUBLISH_TOOLS)} is installed")


def test_the_daemon_reports_what_it_is_advertising(daemon, publisher):
    client = AdminClient(daemon["socket"])
    try:
        listener = client.call("status")["listeners"][0]
    finally:
        client.close()
    assert str(daemon["port"]) in listener["advertised"]


def test_browsing_finds_the_daemon_running_on_this_host(daemon, publisher):
    """The loopback multicast path: IP_MULTICAST_LOOP = 1 is what makes a daemon
    on THIS machine discoverable, and it is easy to lose in a refactor because
    nothing else notices."""
    found = {}

    def visible() -> bool:
        result = browse_detailed(timeout=1.5)
        found["result"] = result
        found["mine"] = [s for s in result.services if s.port == daemon["port"]]
        return bool(found["mine"])

    # The responder needs a moment to register the record after the daemon spawns
    # its publisher, so this polls rather than asserting on one browse.
    assert wait_until(visible, timeout=20.0, interval=1.0), (
        f"never saw port {daemon['port']} advertised "
        f"({found.get('result') and found['result'].problem})")

    service = found["mine"][0]
    assert service.txt.get("version")
    assert service.addresses or service.hostname.endswith(".local")
