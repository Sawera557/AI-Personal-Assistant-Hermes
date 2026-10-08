"""Keep the whole test suite offline.

hermes/config.py runs load_dotenv(override=True), so importing anything from
hermes loads the developer's real .env — real provider keys and the production
DBRDS_HOST. A test that forgets to stub one call would then spend money or touch
the production database. This file closes that hole for every test:

  * HERMES_DISABLE_DB=1 is set before any hermes import, so every store and
    settings flag takes its in-memory path.
  * Outbound sockets to anything other than loopback are refused. A test that
    reaches the network fails loudly instead of silently making a paid call.
    The TestClient talks to the app in-process and never opens a socket.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import sys
from pathlib import Path

os.environ["HERMES_DISABLE_DB"] = "1"

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class NetworkBlocked(RuntimeError):
    """Raised when a test tries to open a non-loopback connection."""


def _is_loopback(address) -> bool:
    host = address[0] if isinstance(address, tuple) else address
    if not isinstance(host, str):
        return False
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo


def _guarded_connect(self, address):
    if self.family == getattr(socket, "AF_UNIX", object()) or _is_loopback(address):
        return _real_connect(self, address)
    raise NetworkBlocked(f"test tried to connect to {address!r}; the suite is offline")


def _guarded_connect_ex(self, address):
    if self.family == getattr(socket, "AF_UNIX", object()) or _is_loopback(address):
        return _real_connect_ex(self, address)
    raise NetworkBlocked(f"test tried to connect to {address!r}; the suite is offline")


def _guarded_getaddrinfo(host, *args, **kwargs):
    if host is None or _is_loopback(host if isinstance(host, str) else str(host)):
        return _real_getaddrinfo(host, *args, **kwargs)
    raise NetworkBlocked(f"test tried to resolve {host!r}; the suite is offline")


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
socket.getaddrinfo = _guarded_getaddrinfo
