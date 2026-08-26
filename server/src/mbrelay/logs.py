"""Logging setup.

Two audiences, one handler. Under systemd, stderr is the journal, so there is no
syslog handler and no journald library here -- instead we detect ``$JOURNAL_STREAM``
and emit a syslog priority prefix, which is what makes ``journalctl -p warning``
actually filter. In a terminal we print timestamps like any other CLI.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time

# syslog severities, keyed by logging level. journald strips the "<N>" prefix and
# uses it as the record priority.
_PRIORITY = {
    logging.CRITICAL: "<2>",
    logging.ERROR: "<3>",
    logging.WARNING: "<4>",
    logging.INFO: "<6>",
    logging.DEBUG: "<7>",
}

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message", "asctime", "taskName",
}


class PriorityFormatter(logging.Formatter):
    """Prefix each line with a syslog priority; omit the timestamp.

    Used only when running under systemd, where journald supplies its own
    timestamp and reads the priority prefix.
    """

    def format(self, record: logging.LogRecord) -> str:
        return _PRIORITY.get(record.levelno, "<6>") + super().format(record)


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for log shippers."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": round(record.created, 3),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Anything a LoggerAdapter injected (session=, uid=, peer=, ...) rides along
        # as a real field rather than being flattened into the message.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def under_systemd() -> bool:
    return bool(os.environ.get("JOURNAL_STREAM"))


def setup(level: str = "info", fmt: str = "text", stream=None) -> None:
    """Install the root handler. Idempotent -- safe to call again on SIGHUP."""
    stream = stream or sys.stderr
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(stream)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    elif under_systemd():
        handler.setFormatter(PriorityFormatter("%(name)s: %(message)s"))
    else:
        f = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        f.converter = time.localtime
        handler.setFormatter(f)

    root.addHandler(handler)
    set_level(level)


def set_level(level: str) -> None:
    logging.getLogger().setLevel(getattr(logging, level.upper(), logging.INFO))


class SessionLogger(logging.LoggerAdapter):
    """Injects session/device identity into every record.

    Makes ``journalctl -u mbrelay | grep uid=9906...`` show one board's whole
    life, which is the question you actually ask when a relay misbehaves.
    """

    def process(self, msg, kwargs):
        kwargs.setdefault("extra", {}).update(self.extra)
        tags = " ".join(f"{k}={v}" for k, v in self.extra.items() if v is not None)
        return (f"{msg} {tags}" if tags else msg), kwargs


def session_logger(logger: logging.Logger, **fields) -> SessionLogger:
    return SessionLogger(logger, fields)
