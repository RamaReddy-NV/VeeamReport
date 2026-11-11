"""Normalised data model shared by every collector (Veeam EM, demo, later NetBackup)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

_FRACTION = re.compile(r"\.(\d+)")


def parse_ts(value: Optional[str]) -> Optional[datetime]:
    """Parse Veeam timestamps such as '2021-06-05T01:04:29.56Z' into aware UTC datetimes."""
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    v = _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), v, count=1)
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class JobSession:
    uid: str
    server: str            # VBR server name (as known by Enterprise Manager)
    job: str
    job_type: str          # raw Veeam job type, mapped to a category through config
    created: datetime
    ended: Optional[datetime]
    state: str
    result: str            # Success / Warning / Failed / None
    is_retry: bool = False


@dataclass
class Task:
    """One workload (VM, agent computer...) processed inside a job session."""
    session_uid: str
    server: str
    job: str
    obj: str               # workload display name
    created: datetime
    ended: Optional[datetime]
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


@dataclass
class Repo:
    server: str
    name: str
    capacity: float        # bytes
    free: float            # bytes

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
