"""Run ``python -m googlemcp <args>`` with all networking disabled and logged.

Usage:
    python stdio_launcher.py <attempt-log> <googlemcp args...>
    python stdio_launcher.py <attempt-log> --probe

Calls to the guarded functions below are appended to ``<attempt-log>`` as
``<kind> <target>`` and then refused with ``OSError``. Guarded:
``socket.socket`` methods ``connect``, ``connect_ex``, ``bind``, ``listen``,
``sendto``, and ``sendmsg``; and the ``socket`` module functions
``create_connection``, ``getaddrinfo``, ``gethostbyname``,
``gethostbyname_ex``, ``gethostbyaddr``, and ``getnameinfo``. Each guarded
method is denied for every address family: a stdio server has no reason to
use them (asyncio's self-pipe uses ``socketpair``, which needs none of these
calls). Other socket APIs are not intercepted.

``--probe`` deliberately exercises every guarded call and prints
``denied <kind>`` for each one that was refused. Tests use it as a positive
control: if a call is not refused, the guard is broken.

One call is known and expected: when urllib3 is imported it probes IPv6
support with ``bind(("::1", 0))`` in ``urllib3.util.connection._has_ipv6``
and closes the socket without listening. It is still refused (urllib3 then
assumes no IPv6), but it is logged as ``expected-bind`` so tests can keep
asserting that every other attempt count is zero. Only that exact caller with
a loopback, port 0 address gets the ``expected-`` prefix.

Out of scope: code that calls the C ``_socket`` module directly, or socket
APIs not listed above, bypasses these Python-level hooks.
"""

from __future__ import annotations

import runpy
import socket
import sys
from pathlib import Path
from typing import Any

_LOG = Path(sys.argv[1])


def _deny(kind: str, target: object) -> OSError:
    with _LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{kind} {target!r}\n")
    return OSError(f"{kind} disabled by the e2e stdio launcher")


_LOOPBACK_PORT0 = {("::1", 0), ("127.0.0.1", 0)}


def _is_urllib3_ipv6_probe(kind: str, target: object) -> bool:
    if kind != "bind" or not isinstance(target, tuple) or tuple(target[:2]) not in _LOOPBACK_PORT0:
        return False
    caller = sys._getframe(2)
    return (
        caller.f_code.co_name == "_has_ipv6"
        and caller.f_globals.get("__name__") == "urllib3.util.connection"
    )


def _socket_method(kind: str, target_index: int | None) -> Any:
    def guarded(self: socket.socket, *args: Any, **kwargs: Any) -> Any:
        target = args[target_index] if target_index is not None and args else None
        if _is_urllib3_ipv6_probe(kind, target):
            raise _deny(f"expected-{kind}", target)
        raise _deny(kind, target)

    return guarded


_SOCKET_METHODS = {
    "connect": 0,
    "connect_ex": 0,
    "bind": 0,
    "listen": None,
    "sendto": -1,
    "sendmsg": None,
}
for _name, _index in _SOCKET_METHODS.items():
    setattr(socket.socket, _name, _socket_method(_name, _index))


def _module_function(kind: str) -> Any:
    def guarded(*args: Any, **kwargs: Any) -> Any:
        raise _deny(kind, args[0] if args else None)

    return guarded


_MODULE_FUNCTIONS = (
    "create_connection",
    "getaddrinfo",
    "gethostbyname",
    "gethostbyname_ex",
    "gethostbyaddr",
    "getnameinfo",
)
for _name in _MODULE_FUNCTIONS:
    setattr(socket, _name, _module_function(_name))


def _probe() -> int:
    """Try every guarded call; print ``denied <kind>`` for each refusal."""
    attempts: list[tuple[str, Any]] = [
        ("getaddrinfo", lambda: socket.getaddrinfo("example.com", 443)),
        ("gethostbyname", lambda: socket.gethostbyname("example.com")),
        ("gethostbyname_ex", lambda: socket.gethostbyname_ex("example.com")),
        ("gethostbyaddr", lambda: socket.gethostbyaddr("192.0.2.1")),
        ("getnameinfo", lambda: socket.getnameinfo(("192.0.2.1", 443), 0)),
        ("create_connection", lambda: socket.create_connection(("192.0.2.1", 443), 1)),
    ]
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    attempts += [
        ("bind", lambda: tcp.bind(("127.0.0.1", 0))),
        ("listen", lambda: tcp.listen(1)),
        ("connect", lambda: tcp.connect(("192.0.2.1", 443))),
        ("connect_ex", lambda: tcp.connect_ex(("192.0.2.1", 443))),
        ("sendto", lambda: udp.sendto(b"x", ("192.0.2.1", 53))),
        ("sendmsg", lambda: udp.sendmsg([b"x"], [], 0, ("192.0.2.1", 53))),
    ]
    failures = 0
    for kind, attempt in attempts:
        try:
            attempt()
        except OSError:
            print(f"denied {kind}")
        else:
            print(f"ALLOWED {kind}")
            failures += 1
    tcp.close()
    udp.close()
    return 1 if failures else 0


if sys.argv[2:] == ["--probe"]:
    sys.exit(_probe())

sys.argv = ["googlemcp", *sys.argv[2:]]
runpy.run_module("googlemcp", run_name="__main__", alter_sys=True)
