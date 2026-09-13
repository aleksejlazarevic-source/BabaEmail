"""Source adapter contract.

Every retailer adapter returns a SourceResult. Contract rule 1: adapters
extract EVERYTHING the retailer publishes and never filter on preferences —
selection happens later, in select.py, so completeness stays provable.
"""

from __future__ import annotations

import abc
from datetime import date, datetime
from pathlib import Path

import httpx

from ..models import SourceResult, SourceStatus

# A plain desktop UA. Identifying honestly rather than impersonating a person.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

SNAPSHOT_DIR = Path("snapshots")


class Source(abc.ABC):
    """One retailer."""

    #: stable key used in preferences.yaml and the database
    name: str
    #: human name shown to grandma
    display_name: str

    @abc.abstractmethod
    def fetch(self, today: date) -> SourceResult:
        """Fetch and parse everything this retailer publishes for `today`.

        Must never raise for an expected failure (blocked, no leaflet, parse
        error) — return a SourceResult with the appropriate status instead, so
        the run continues and the failure reaches the email banner and the
        alert. Only genuinely unexpected bugs should propagate.
        """

    # -- helpers shared by adapters -------------------------------------

    def result(self, status: SourceStatus, url: str, **kw) -> SourceResult:
        return SourceResult(retailer=self.name, status=status, source_url=url,
                            fetched_at=datetime.now(), **kw)

    def client(self, headers: dict[str, str] | None = None, timeout: float = 120.0) -> httpx.Client:
        return httpx.Client(headers=headers if headers is not None else {"User-Agent": UA},
                            timeout=timeout, follow_redirects=True)

    def snapshot_path(self, today: date, suffix: str) -> Path:
        d = SNAPSHOT_DIR / today.isoformat()
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{self.name}{suffix}"


def classify_http_failure(status_code: int, body: str = "") -> SourceStatus:
    """Map an HTTP failure onto a SourceStatus.

    Cloudflare/bot walls and rate limits are BLOCKED — never NO_LEAFLET —
    because 'we were stopped' and 'there is nothing today' must never be
    confused. Reporting a block as 'no leaflet' would quietly hide a whole
    store from grandma on a day it actually had offers.
    """
    if status_code in (401, 403, 429) or "just a moment" in body.lower() or "trenutek" in body.lower():
        return SourceStatus.BLOCKED
    if status_code == 404:
        return SourceStatus.NO_LEAFLET
    return SourceStatus.PARSE_FAILED
