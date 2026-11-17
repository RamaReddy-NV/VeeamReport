"""Collector for the Veeam Backup Enterprise Manager REST API (default port 9398).

One EM instance aggregates every connected VBR server, so one login covers all three.
Docs: https://helpcenter.veeam.com/docs/vbr/em_rest/overview.html

Field names below follow the EM documentation; anything not 100% certain is read with
.get() and a fallback, and --dump-raw lets you check the real payloads on first run.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from types import TracebackType
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .model import Collected, Job, JobSession, Repo, Task, parse_ts

log = logging.getLogger(__name__)

_RETRY_SUFFIX = re.compile(
    r"\s*\(Retry \d+\)$",
    re.IGNORECASE,
)


def _links(entity: dict) -> list[dict]:
    links = entity.get("Links") or []
    if isinstance(links, dict):
        links = links.get("Links") or links.get("Link") or []
    return links if isinstance(links, list) else []


def _link_name(entity: dict, link_type: str) -> str | None:
    for link in _links(entity):
        if link.get("Type") == link_type:
            return link.get("Name")
    return None


def _job_from_session_name(name: str | None) -> str:
    if not name:
        return "?"
    return _RETRY_SUFFIX.sub("", name.rsplit("@", 1)[0]).strip()


def _entities(payload: dict) -> list[dict]:
    if not isinstance(payload, dict) or "Entities" not in payload:
        raise ValueError(
            "Enterprise Manager response is missing the Entities collection"
        )

    ents = payload.get("Entities") or {}
    if not isinstance(ents, dict):
        if ents == []:
            return []
        raise ValueError("Enterprise Manager returned an invalid Entities collection")

    for value in ents.values():
        if isinstance(value, list):
            return value

        if isinstance(value, dict):
            for inner in value.values():
                if isinstance(inner, list):
                    return inner

    return []


def _quote(value: str) -> str:
    if not re.search(r'[\s;,=<>()"\\]', value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _dump_query_payload(dump_dir: Path, qtype: str, payload: dict) -> Path:
    dump_dir.mkdir(parents=True, exist_ok=True)
    suffix = 1
    while True:
        filename = f"{qtype}.json" if suffix == 1 else f"{qtype}-{suffix}.json"
        path = dump_dir / filename
        try:
            with path.open("x", encoding="utf-8") as dump_file:
                json.dump(payload, dump_file, indent=2)
                dump_file.write("\n")
        except FileExistsError:
            suffix += 1
            continue
        return path


def _required_timestamp(entity: dict, field: str, entity_type: str) -> datetime:
    raw_value = entity.get(field)
    if not isinstance(raw_value, str):
        raw_value = None
    try:
        value = parse_ts(raw_value)
    except ValueError as exc:
        uid = entity.get("UID") or entity.get("Name") or "unknown entity"
        raise ValueError(
            f"{entity_type} {uid!r} has an invalid timestamp {field}"
        ) from exc
    if value is None:
        uid = entity.get("UID") or entity.get("Name") or "unknown entity"
        raise ValueError(f"{entity_type} {uid!r} is missing required timestamp {field}")
    return value


class EMClient:
    def __init__(
        self,
        url: str,
        user: str,
        password: str,
        *,
        verify_tls: bool = True,
        page_size: int = 1000,
        dump_dir: Path | None = None,
    ) -> None:
        parsed_url = urlsplit(url)
        try:
            valid_authority = bool(parsed_url.hostname) and parsed_url.port != 0
        except ValueError:
            valid_authority = False
        if (
            parsed_url.scheme.lower() != "https"
            or not parsed_url.netloc
            or not valid_authority
            or parsed_url.username
            or parsed_url.password
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ValueError(
                "Enterprise Manager URL must be an HTTPS URL "
                "without embedded credentials"
            )
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or page_size < 1
        ):
            raise ValueError("Enterprise Manager page_size must be a positive integer")

        self.base = url.rstrip("/")
        self.timeout = 60
        self.page_size = page_size
        self.dump_dir = dump_dir
        self._logon_id: str | None = None

        self.http = requests.Session()
        self.http.verify = verify_tls
        retry = Retry(
            total=5,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.http.mount("http://", adapter)
        self.http.mount("https://", adapter)

        try:
            self._login(user, password)
        except Exception:
            self.http.close()
            raise

    def _login(
        self,
        user: str,
        password: str,
    ) -> None:

        r = self.http.post(
            f"{self.base}/sessionMngr/?v=latest",
            auth=(user, password),
            timeout=self.timeout,
        )

        try:
            r.raise_for_status()
        except requests.HTTPError as exc:
            raise RuntimeError(
                f"Enterprise Manager login failed: {r.status_code} {r.reason}"
            ) from exc

        session_id = r.headers.get("X-RestSvcSessionId")

        if not session_id:
            raise RuntimeError("Enterprise Manager did not return X-RestSvcSessionId")

        self.http.headers["X-RestSvcSessionId"] = session_id

        try:
            payload = r.json()
            self._logon_id = payload.get("SessionId")
        except ValueError:
            self._logon_id = None

        log.info(
            "Logged on to Enterprise Manager %s",
            self.base,
        )

    def __enter__(self) -> EMClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:

        if self._logon_id:
            try:
                self.http.delete(
                    f"{self.base}/logonSessions/{self._logon_id}",
                    timeout=30,
                )
            except requests.RequestException as exc:
                log.warning(
                    "Failed to close Enterprise Manager session: %s",
                    exc,
                )

        self.http.close()

    # ------------------------------------------------------------------
    # Generic query service
    # ------------------------------------------------------------------

    def _query_page(
        self,
        qtype: str,
        page: int,
        filter: str | None,
        sort_desc: str | None,
        page_size: int | None,
    ) -> dict:
        params: dict[str, str | int] = {
            "type": qtype,
            "format": "Entities",
            "pageSize": page_size or self.page_size,
            "page": page,
        }
        if filter:
            params["filter"] = filter
        if sort_desc:
            params["sortDesc"] = sort_desc

        response = self.http.get(
            f"{self.base}/query",
            params=params,
            timeout=self.timeout,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise RuntimeError(
                f"Enterprise Manager query failed "
                f"(type={qtype}, page={page}) "
                f"with HTTP {response.status_code}"
            ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Enterprise Manager returned invalid JSON (type={qtype}, page={page})"
            ) from exc

        if not isinstance(payload, dict):
            # Treat malformed EM payloads as runtime protocol failures.
            raise RuntimeError(  # noqa: TRY004
                f"Enterprise Manager returned an invalid response "
                f"(type={qtype}, page={page}): expected a JSON object"
            )

        if self.dump_dir and page == 1:
            dump_path = _dump_query_payload(self.dump_dir, qtype, payload)
            log.info("Saved first page of %s query to %s", qtype, dump_path)
        return payload

    @staticmethod
    def _has_more_pages(
        items: list[dict],
        page: int,
        pages: int,
        max_pages: int | None,
    ) -> bool:
        return bool(items) and page < pages and not (max_pages and page >= max_pages)

    def query(
        self,
        qtype: str,
        *,
        filter: str | None = None,
        sort_desc: str | None = None,
        stop: Callable[[dict], bool] | None = None,
        max_pages: int | None = None,
        page_size: int | None = None,
    ) -> Iterator[dict]:
        page = 1
        while True:
            payload = self._query_page(
                qtype,
                page,
                filter,
                sort_desc,
                page_size,
            )
            items = _entities(payload)
            for item in items:
                if stop and stop(item):
                    return
                yield item
            pages = int((payload.get("PagingInfo") or {}).get("PagesCount") or page)
            if not self._has_more_pages(items, page, pages, max_pages):
                return
            page += 1


class EMCollector:
    def __init__(self, client: EMClient) -> None:
        self.c = client

    @staticmethod
    def _older_than(start: datetime) -> Callable[[dict], bool]:
        def stop(entity: dict) -> bool:
            ts = parse_ts(entity.get("CreationTimeUTC"))
            return ts is not None and ts < start

        return stop

    def _job_session(self, e: dict) -> JobSession:
        return JobSession(
            uid=e.get("UID", ""),
            server=_link_name(e, "BackupServerReference") or "?",
            job=e.get("JobName")
            or _link_name(e, "JobReference")
            or _job_from_session_name(e.get("Name")),
            job_type=e.get("JobType") or "?",
            created=_required_timestamp(e, "CreationTimeUTC", "Job session"),
            ended=parse_ts(e.get("EndTimeUTC")),
            state=e.get("State") or "",
            result=e.get("Result") or "None",
            is_retry=bool(e.get("IsRetry", False)),
        )

    def _task(self, e: dict) -> Task:
        return Task(
            session_uid=e.get("JobSessionUid", ""),
            server=_link_name(e, "BackupServerReference") or "?",
            job=_job_from_session_name(_link_name(e, "BackupJobSessionReference")),
            obj=e.get("VmDisplayName") or e.get("Name", "?").rsplit("@", 1)[0],
            created=_required_timestamp(e, "CreationTimeUTC", "Task session"),
            ended=parse_ts(e.get("EndTimeUTC")),
            state=e.get("State") or "",
            result=e.get("Result") or "None",
            reason=(e.get("Reason") or "").strip(),
        )

    def collect(self, since: datetime) -> Collected:
        """Collect records created since `since` (UTC), newest first."""
        stop = self._older_than(since)
        log.info("Collecting EM data created since %s", since.isoformat())
        job_sessions = [
            self._job_session(e)
            for e in self.c.query(
                "BackupJobSession", sort_desc="CreationTime", stop=stop
            )
        ]
        replica_sessions = [
            self._job_session(e)
            for e in self.c.query(
                "ReplicaJobSession", sort_desc="CreationTime", stop=stop
            )
        ]
        tasks = [
            self._task(e)
            for e in self.c.query(
                "BackupTaskSession", sort_desc="CreationTime", stop=stop
            )
        ]

        jobs = []
        for e in self.c.query("Job"):
            missing_schedule_fields = tuple(
                field
                for field in ("ScheduleConfigured", "ScheduleEnabled")
                if e.get(field) is None
            )
            if missing_schedule_fields:
                log.warning(
                    "Job %s on %s is missing schedule fields: %s",
                    e.get("Name", "?"),
                    _link_name(e, "BackupServerReference") or "?",
                    ", ".join(missing_schedule_fields),
                )
            jobs.append(
                Job(
                    server=_link_name(e, "BackupServerReference") or "?",
                    name=e.get("Name", "?"),
                    job_type=e.get("JobType", "?"),
                    schedule_configured=bool(e.get("ScheduleConfigured", False)),
                    schedule_enabled=bool(e.get("ScheduleEnabled", False)),
                    schedule_fields_missing=missing_schedule_fields,
                )
            )

        repos = []
        for e in self.c.query("Repository"):
            repos.append(
                Repo(
                    server=_link_name(e, "BackupServerReference") or "?",
                    name=e.get("Name", "?"),
                    capacity=float(e.get("Capacity") or 0),
                    free=float(e.get("FreeSpace") or 0),
                )
            )

        log.info(
            "Collected %d backup job sessions, %d replica sessions, %d task sessions, "
            "%d jobs, %d repositories",
            len(job_sessions),
            len(replica_sessions),
            len(tasks),
            len(jobs),
            len(repos),
        )
        return Collected(job_sessions, replica_sessions, tasks, jobs, repos)

    def history(
        self, server: str, job: str, obj: str, max_items: int = 60
    ) -> list[Task]:
        """Return recent task sessions for a workload in one job."""
        out = []
        for e in self.c.query(
            "BackupTaskSession",
            filter=f"VmDisplayName=={_quote(obj)}",
            sort_desc="CreationTime",
            page_size=max_items,
            max_pages=1,
        ):
            t = self._task(e)
            if t.job == job and t.server == server and t.obj == obj:
                out.append(t)
        return out
