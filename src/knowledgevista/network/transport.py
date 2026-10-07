"""The one place that touches a socket: a tiny HTTP GET over the standard library.

It is behind a protocol (`Transport`) so every retry, back-off and failure rule in `policy.py` is tested against a
scripted transport AND against a real HTTP server on the loopback interface (tests/test_network.py), never only mocks.

Only GET. Redirects are followed by urllib (at most the default), the body is read up to a cap, and the failure kinds
a caller needs to tell apart (timeout, unreachable, TLS) are reported as `TransportError.kind`, not as a message to be
parsed.
"""

from __future__ import annotations

import http.client
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol


class TransportError(Exception):
    """The request did not produce an HTTP response. `kind` is one of: timeout, dns, connection, tls."""

    def __init__(self, kind: str, message: str = ""):
        super().__init__(message or kind)
        self.kind = kind


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)  # lower-cased names
    body: bytes = b""
    truncated: bool = False  # the body was longer than the cap


class Transport(Protocol):
    def get(self, url: str, headers: dict[str, str], timeout: float, max_bytes: int) -> HttpResponse: ...


class UrllibTransport:
    def get(self, url: str, headers: dict[str, str], timeout: float, max_bytes: int) -> HttpResponse:
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - scheme is checked by the caller
                return self._read(response.status, response.headers, response, max_bytes)
        except urllib.error.HTTPError as error:  # a response with an error status is still a response
            try:
                return self._read(error.code, error.headers, error, max_bytes)
            finally:
                error.close()
        except urllib.error.URLError as error:
            raise _classify(error.reason) from error
        except (TimeoutError, socket.timeout) as error:
            raise TransportError("timeout", "the request timed out") from error
        except ssl.SSLError as error:
            raise TransportError("tls", str(error)) from error
        except (http.client.HTTPException, ConnectionError, OSError) as error:
            raise TransportError("connection", f"{type(error).__name__}: {error}") from error

    @staticmethod
    def _read(status: int, headers, stream, max_bytes: int) -> HttpResponse:
        try:
            body = stream.read(max_bytes + 1)
        except (TimeoutError, socket.timeout) as error:
            raise TransportError("timeout", "the response timed out") from error
        except (http.client.HTTPException, OSError) as error:
            raise TransportError("connection", f"{type(error).__name__}: {error}") from error
        lowered = {name.lower(): value for name, value in headers.items()}
        return HttpResponse(status, lowered, body[:max_bytes], truncated=len(body) > max_bytes)


def _classify(reason: object) -> TransportError:
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return TransportError("timeout", "the request timed out")
    if isinstance(reason, socket.gaierror):
        return TransportError("dns", f"could not resolve the host: {reason}")
    if isinstance(reason, ssl.SSLError):
        return TransportError("tls", str(reason))
    return TransportError("connection", f"{type(reason).__name__}: {reason}")
