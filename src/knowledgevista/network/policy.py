"""The network policy: the single owner of the User-Agent, timeouts, retries, pacing, the request budget and the offline
switch (docs/METADATA.md, "Privacy and the network").

Rules this module enforces for every provider, so no provider has to remember them:

  * Nothing is sent unless `policy.online` is true. A disabled policy returns `not_attempted` without touching a socket.
  * Requests are paced one at a time. The starting interval is conservative and is then taken from the provider's own
    rate-limit headers (`x-rate-limit-limit` / `x-rate-limit-interval`, which Crossref sends), never from a number in
    this code: limits change, and a hard-coded one is wrong the day they do.
  * HTTP 429 and 503 are answered with the server's `Retry-After`; if it is longer than `max_wait` the run gives up on
    that provider instead of sleeping for minutes. Other server errors and timeouts back off exponentially with jitter.
  * A fixed request budget bounds one run, retries included.
  * "The provider could not answer" is a STATE (`rate_limited`, `offline`, ...), never "no match": callers must not
    treat a failure as evidence that a work does not exist.
"""

from __future__ import annotations

import email.utils
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlsplit

from knowledgevista import __version__
from knowledgevista.network.transport import HttpResponse, Transport, TransportError, UrllibTransport

PROJECT_URL = "https://github.com/xaerogonzo/knowledgevista"
_LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


class LookupState(StrEnum):
    SUCCESS = "success"
    NO_MATCH = "no_match"
    TRANSIENT_ERROR = "transient_error"
    RATE_LIMITED = "rate_limited"
    UNAUTHORIZED = "unauthorized"
    OFFLINE = "offline"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    NOT_ATTEMPTED = "not_attempted"

    @property
    def could_not_answer(self) -> bool:
        """True when the provider gave no answer at all, which must never be recorded as 'unresolved' or 'no match'."""
        return self not in (LookupState.SUCCESS, LookupState.NO_MATCH)


@dataclass
class NetworkPolicy:
    online: bool = False
    mailto: str | None = None
    timeout: float = 20.0
    max_retries: int = 2
    request_budget: int = 1000
    min_interval: float = 1.0  # the starting pace, in seconds between requests; adapted from rate-limit headers
    floor_interval: float = 0.2  # never faster than this, whatever a server says it allows
    max_wait: float = 60.0  # the longest the program sleeps on a Retry-After before giving up for the run
    max_bytes: int = 4_000_000
    backoff_base: float = 1.0

    def user_agent(self) -> str:
        contact = f"; mailto:{self.mailto}" if self.mailto else ""
        return f"KnowledgeVista/{__version__} ({PROJECT_URL}{contact})"


@dataclass
class FetchOutcome:
    state: LookupState
    status: int | None = None
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    attempts: int = 0
    detail: str = ""
    retry_after: float | None = None


_INTERVAL = re.compile(r"^\s*([\d.]+)\s*(ms|s|m)?\s*$", re.IGNORECASE)


def _seconds(text: str | None) -> float | None:
    match = _INTERVAL.match(text or "")
    if not match:
        return None
    scale = {"ms": 0.001, "s": 1.0, "m": 60.0, None: 1.0}[(match.group(2) or "").lower() or None]
    return float(match.group(1)) * scale


def _retry_after(value: str | None, now: float) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
        return max(0.0, when.timestamp() - now)
    except (TypeError, ValueError):
        return None


class Fetcher:
    """One provider's connection to the world: paced, bounded and honest about failure. Not thread-safe by design:
    one request at a time is the pace every provider asks of an anonymous client."""

    def __init__(
        self,
        policy: NetworkPolicy,
        transport: Transport | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ):
        self.policy = policy
        self.transport = transport or UrllibTransport()
        self._clock, self._wall, self._sleep, self._rng = clock, wall, sleep, rng
        self.interval = policy.min_interval
        self.requests_made = 0
        self.waited = 0.0
        self._last_start: float | None = None

    @property
    def budget_left(self) -> int:
        return max(0, self.policy.request_budget - self.requests_made)

    def _pace(self) -> None:
        if self._last_start is not None:
            wait = self._last_start + self.interval - self._clock()
            if wait > 0:
                self._wait(wait)
        self._last_start = self._clock()

    def _wait(self, seconds: float) -> None:
        self.waited += seconds
        self._sleep(seconds)

    def _learn_pace(self, headers: dict[str, str]) -> None:
        limit, interval = headers.get("x-rate-limit-limit"), _seconds(headers.get("x-rate-limit-interval"))
        try:
            allowed = float(limit) if limit else 0.0
        except ValueError:
            return
        if allowed > 0 and interval:
            self.interval = max(self.policy.floor_interval, interval / allowed * 1.25)

    def _backoff(self, attempt: int) -> float:
        return min(self.policy.max_wait, self.policy.backoff_base * (2 ** attempt) * (0.5 + self._rng()))

    def get(self, url: str, *, accept: str = "application/json") -> FetchOutcome:
        policy = self.policy
        if not policy.online:
            return FetchOutcome(LookupState.NOT_ATTEMPTED, detail="online lookups are switched off")
        parts = urlsplit(url)
        if parts.scheme != "https" and not (parts.scheme == "http" and (parts.hostname or "") in _LOOPBACK):
            raise ValueError(f"refusing to send a request over {parts.scheme or 'no scheme'}: {url!r}")
        headers = {"User-Agent": policy.user_agent(), "Accept": accept}
        last = FetchOutcome(LookupState.TRANSIENT_ERROR, detail="no attempt was made")
        for attempt in range(policy.max_retries + 1):
            if self.budget_left <= 0:
                return FetchOutcome(LookupState.NOT_ATTEMPTED, attempts=attempt,
                                    detail=f"the request budget ({policy.request_budget}) for this run is used up")
            self._pace()
            self.requests_made += 1
            try:
                response: HttpResponse = self.transport.get(url, headers, policy.timeout, policy.max_bytes)
            except TransportError as error:
                state = LookupState.OFFLINE if error.kind in ("dns", "connection") else (
                    LookupState.PROVIDER_UNAVAILABLE if error.kind == "tls" else LookupState.TRANSIENT_ERROR)
                last = FetchOutcome(state, attempts=attempt + 1, detail=f"{error.kind}: {error}")
                if error.kind == "tls":
                    return last  # a certificate problem will not heal by retrying
                if attempt < policy.max_retries:
                    self._wait(self._backoff(attempt))
                continue
            self._learn_pace(response.headers)
            outcome = self._interpret(response, attempt + 1)
            if outcome.state in (LookupState.RATE_LIMITED, LookupState.PROVIDER_UNAVAILABLE, LookupState.TRANSIENT_ERROR) \
                    and response.status in (429, 500, 502, 503, 504):
                last = outcome
                if attempt >= policy.max_retries:
                    return outcome
                wait = outcome.retry_after if outcome.retry_after is not None else self._backoff(attempt)
                if wait > policy.max_wait:
                    return outcome  # the server asked for longer than this run will wait
                self._wait(wait)
                continue
            return outcome
        return last

    def _interpret(self, response: HttpResponse, attempts: int) -> FetchOutcome:
        status = response.status
        base = {"status": status, "headers": response.headers, "attempts": attempts}
        if status == 200:
            if response.truncated:
                return FetchOutcome(LookupState.PROVIDER_UNAVAILABLE, detail=f"the response was larger than {self.policy.max_bytes} bytes", **base)
            return FetchOutcome(LookupState.SUCCESS, body=response.body, **base)
        if status == 404:
            return FetchOutcome(LookupState.NO_MATCH, detail="HTTP 404", **base)
        if status in (401, 403):
            return FetchOutcome(LookupState.UNAUTHORIZED, detail=f"HTTP {status}", **base)
        retry = _retry_after(response.headers.get("retry-after"), self._wall())
        if status == 429:
            return FetchOutcome(LookupState.RATE_LIMITED, detail="HTTP 429", retry_after=retry, **base)
        if status == 503:
            return FetchOutcome(LookupState.PROVIDER_UNAVAILABLE, detail="HTTP 503", retry_after=retry, **base)
        if status >= 500:
            return FetchOutcome(LookupState.TRANSIENT_ERROR, detail=f"HTTP {status}", retry_after=retry, **base)
        return FetchOutcome(LookupState.PROVIDER_UNAVAILABLE, detail=f"HTTP {status}", **base)
