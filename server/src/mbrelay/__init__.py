"""mbrelay -- serve USB-attached micro:bit radio relays over TCP.

A client connecting to the pool port is bound to a free relay and from then on
the socket is a transparent byte pipe to that board's serial port. See
``docs/relay-server.md`` in the repository root for the full contract.
"""

__version__ = "0.20260826.8"
