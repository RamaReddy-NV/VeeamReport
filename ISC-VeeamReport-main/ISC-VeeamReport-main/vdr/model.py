"""Normalised data model shared by every collector (Veeam EM, demo, later NetBackup)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

UTC = timezone.utc
_FRACTION = re.compile(r"\.(\d+)")


def parse_ts(value: str | None) -> datetime | None:
    """Parse Veeam timestamps into UTC datetimes.

    Example: '2021-06-05T01:04:29.56Z'.
    """
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    v = _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), v, count=1)
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


@dataclass
class JobSession:
    uid: str
    server: str  # VBR server name (as known by Enterprise Manager)
    job: str
    job_type: str  # raw Veeam job type, mapped to a category through config
    created: datetime
    ended: datetime | None
    state: str
    result: str  # Success / Warning / Failed / None
    is_retry: bool = False


@dataclass
class Task:
    """One workload (VM, agent computer...) processed inside a job session."""

    session_uid: str
    server: str
    job: str
    obj: str  # workload display name
    created: datetime
    ended: datetime | None
    state: str
    result: str
    reason: str = ""


@dataclass
class Job:
    server: str
    name: str
    job_type: str
    schedule_configured: bool
    schedule_enabled: bool
    schedule_fields_missing: tuple[str, ...] = ()


@dataclass
class Repo:
    server: str
    name: str
    capacity: float  # bytes
    free: float  # bytes

    @property
    def free_pct(self) -> float:
        return 100.0 * self.free / self.capacity if self.capacity else 100.0


@dataclass
class Collected:
    job_sessions: list[JobSession]
    replica_sessions: list[JobSession]
    tasks: list[Task]
    jobs: list[Job]
    repos: list[Repo]


class Collector(Protocol):
    def collect(self, since: datetime) -> Collected: ...

    def history(
        self, server: str, job: str, obj: str, max_items: int = 60
    ) -> list[Task]: ...
