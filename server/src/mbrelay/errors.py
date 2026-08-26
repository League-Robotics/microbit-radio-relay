"""Exception types shared across mbrelay.

Kept in their own module so ``cli``/``server``/``session`` can all catch them
without importing each other.
"""

from __future__ import annotations


class MbrelayError(Exception):
    """Base for every error this package raises deliberately."""


class ConfigError(MbrelayError):
    """Bad or unparseable configuration. Always fatal at startup."""


class RelayError(MbrelayError):
    """The board did not behave the way the protocol says it should."""


class NoFreeDevice(MbrelayError):
    """No relay was available to bind to an incoming connection."""

    def __init__(self, total: int = 0, busy: int = 0, releasing: int = 0) -> None:
        self.total = total
        self.busy = busy
        self.releasing = releasing
        super().__init__(f"no relay available ({total} devices, {busy} in use, "
                         f"{releasing} being handed back)")


class AcquireFailed(MbrelayError):
    """A specific device could not be brought to a known-good state."""


class DeviceGone(MbrelayError):
    """The device disappeared from USB mid-operation."""


class AdminError(MbrelayError):
    """The admin socket refused a request, or could not be reached."""

    def __init__(self, message: str, code: str = "error") -> None:
        self.code = code
        super().__init__(message)


class DaemonNotRunning(AdminError):
    """No daemon is listening on the admin socket."""

    def __init__(self, path: str) -> None:
        super().__init__(f"no mbrelay daemon listening at {path}", code="not_running")


# Stable process exit codes. HIL tests and Ansible branch on these, so they are
# part of the public interface -- do not renumber.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NO_DAEMON = 3
EXIT_NO_DEVICE = 4
EXIT_NO_FREE_DEVICE = 5
EXIT_HARDWARE = 6
