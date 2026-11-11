"""Turns normalised Veeam data into the daily report model (what the HTML template renders)."""
from __future__ import annotations

import csv
import fnmatch
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from .model import Collected, JobSession, Task

log = logging.getLogger(__name__)

DONE_STATES = {"stopped", "completed", "idle", "finished"}
HistoryFn = Callable[[str, str, str], list[Task]]


# ---------------------------------------------------------------- helpers
def status_of(result: str, state: str) -> str:
    r = (result or "").lower()
    if r in ("success", "warning", "failed"):
        return r.capitalize()
    if (state or "").lower() not in DONE_STATES:
        return "Running"
    return "Failed"  # finished without a result (e.g. cancelled): workload not protected


def _uid(value: str) -> str:
    return (value or "").rsplit(":", 1)[-1].lower()


def report_window(report_date: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(report_date, time.min, tzinfo=tz)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def _first_line(text: str, limit: int = 160) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= limit else line[: limit - 1] + "…"


@dataclass
class Comment:
    scope: str
    tool: str
    job: str
    server: str
    mitigation: str
    comment: str
    expires: Optional[date]


def load_comments(path: Optional[str], report_date: date) -> list[Comment]:
    """
    Comments register maintained by L1/L2.

    Invalid rows are skipped instead of failing the entire report.
    """

    if not path or not Path(path).exists():
        if path:
            log.warning(
                "Comments register %s not found, continuing without it",
                path,
            )
        return []

    out = []

    with open(
        path,
        newline="",
        encoding="utf-8-sig",
    ) as fh:

        for row in csv.DictReader(fh):

            row = {
                k.strip().lower(): (v or "").strip()
                for k, v in row.items()
                if k
            }

            exp = row.get("expires")

            try:
                exp_date = (
                    date.fromisoformat(exp)
                    if exp
                    else None
                )
            except (ValueError, TypeError):
                log.warning(
                    "Invalid expiry date '%s' in comments file %s. Row skipped.",
                    exp,
                    path,
                )
                continue

            if exp_date and exp_date < report_date:
                continue

            out.append(
                Comment(
                    row.get("scope", "").lower(),
                    row.get("tool", "*") or "*",
                    row.get("job", "*") or "*",
                    row.get("server", "*") or "*",
                    row.get("mitigation", ""),
                    row.get("comment", ""),
                    exp_date,
                )
            )

    return out

def _match(pattern: str, value: str) -> bool:
    return fnmatch.fnmatch(value.lower(), (pattern or "*").lower())


# ---------------------------------------------------------------- builder
@dataclass
class ReportBuilder:
    cfg: dict
    report_date: date
    now: datetime
    history_fn: Optional[HistoryFn] = None
    data_warnings: list[str] = field(default_factory=list)

    def __post_init__(self):
        rc = self.cfg.get("report", {})
        self.tz = ZoneInfo(rc.get("timezone", "UTC"))
        self.start, self.end = report_window(self.report_date, self.tz)
        self.tools = self.cfg["tools"]
        jt = self.cfg.get("job_types", {})
        self.type_cat = {}
        for cat, names in jt.items():
            for n in names:
                self.type_cat[n.lower()] = cat
        loc = self.cfg.get("location", {})
        self.loc_re = re.compile(loc.get("job_name_regex", r"^([A-Za-z]{3})-"))
        self.loc_by_server = {k.lower(): v for k, v in (loc.get("default_by_server") or {}).items()}
        self.loc_default = loc.get("default", "N/A")

    # -- mapping ------------------------------------------------------------
    def tool_of(self, server: str) -> dict:
        s = server.lower()
        for t in self.tools:
            for cand in t["servers"]:
                c = cand.lower()
                if s == c or s.split(".")[0] == c.split(".")[0]:
                    return t
        msg = f"VBR server '{server}' is not mapped to a tool in config (tools[].servers)"
        if msg not in self.data_warnings:
            self.data_warnings.append(msg)
            self.tools.append({"label": f"Unmapped: {server}", "short": server, "servers": [server]})
        return self.tools[-1]

    def category(self, job_type: str) -> str:
        cat = self.type_cat.get((job_type or "").lower())
        if cat is None:
            msg = f"Job type '{job_type}' is not mapped in config (job_types); ignored"
            if msg not in self.data_warnings:
                self.data_warnings.append(msg)
            return "ignored"
        return cat

    def location_of(self, job: str, server: str) -> str:
        m = self.loc_re.search(job or "")
        if m:
            return m.group(1).upper()
        return self.loc_by_server.get(server.lower(), self.loc_by_server.get(server.lower().split(".")[0],
                                                                             self.loc_default))

    # -- selection ----------------------------------------------------------
    def _sessions_in_scope(self, sessions: list[JobSession], category: Optional[str]) -> list[JobSession]:
        """Sessions created on the report day, plus retries of those jobs that ran after midnight."""
        include_retries = self.cfg.get("report", {}).get("include_retries_after_window", True)
        selected = [s for s in sessions if self.start <= s.created < self.end
                    and (category is None or self.category(s.job_type) == category)]
        jobs_in_window = {(s.server, s.job) for s in selected}
        if include_retries:
            selected += [s for s in sessions if s.is_retry and s.created >= self.end
                         and (s.server, s.job) in jobs_in_window]
        return selected

    def consecutive_failures(self, server: str, job: str, obj: str) -> Optional[int]:
        if not self.history_fn:
            return None
        days = set()
        for t in self.history_fn(server, job, obj):
            st = status_of(t.result, t.state)
            if st == "Running":
                continue
            if st != "Failed":
                break
            days.add(t.created.astimezone(self.tz).date())
        return len(days) or 1

    # -- main ---------------------------------------------------------------
    def build(self, data: Collected, comments: list[Comment]) -> dict:
        backup_sessions = self._sessions_in_scope(data.job_sessions, "backup")
        uids = {_uid(s.uid) for s in backup_sessions}

        # final result per workload = latest task among the in-scope sessions
        finals: dict[tuple[str, str, str], Task] = {}
        orphan = 0
        for t in data.tasks:
            if _uid(t.session_uid) not in uids:
                if self.start <= t.created < self.end and not t.session_uid:
                    orphan += 1
                continue
            key = (t.server, t.job, t.obj)
            if key not in finals or t.created > finals[key].created:
                finals[key] = t
        if orphan:
            self.data_warnings.append(f"{orphan} task sessions had no parent job session and were skipped")

        summary = self._summary(finals, comments)
        failures = self._failures(finals, comments)
        protection = self._protection(data)
        attention = self._jobs_attention(data)
        repos = self._repos(data)
        notes = [c.comment for c in comments if c.scope == "note" and c.comment]

        rc = self.cfg.get("report", {})
        return {
            "title": rc.get("title", "Backup Report"),
            "date_str": self.report_date.strftime(rc.get("date_format", "%d/%m/%Y")),
            "window": f"{self.start.astimezone(self.tz):%d/%m/%Y %H:%M} → "
                      f"{self.end.astimezone(self.tz):%d/%m/%Y %H:%M} ({self.tz.key})",
            "generated": self.now.astimezone(self.tz).strftime("%d/%m/%Y %H:%M %Z"),
            "summary": summary,
            "failures": failures,
            "show_error_column": self.cfg.get("failures", {}).get("show_error_column", True),
            "protection": protection,
            "tool_shorts": [t.get("short", t["label"]) for t in self.tools
                            if not t["label"].startswith("Unmapped")],
            "attention": attention,
            "repos": repos,
            "repo_threshold": self.cfg.get("repositories", {}).get("free_space_warning_pct", 10),
            "notes": notes,
            "data_warnings": self.data_warnings,
        }

    def _summary(self, finals, comments) -> list[dict]:
        per_tool: dict[str, Counter] = defaultdict(Counter)
        warn_reasons: dict[str, Counter] = defaultdict(Counter)
        for (server, _job, _obj), t in finals.items():
            tool = self.tool_of(server)["label"]
            st = status_of(t.result, t.state)
            per_tool[tool][st] += 1
            if st == "Warning" and t.reason:
                warn_reasons[tool][_first_line(t.reason, 70)] += 1
        rows = []
        for tool in self.tools:
            label = tool["label"]
            c = per_tool.get(label, Counter())
            manual = [x.comment for x in comments if x.scope == "summary"
                      and (_match(x.tool, label) or _match(x.tool, tool.get("short", "")))]
            auto = ""
            if c["Warning"]:
                top = warn_reasons[label].most_common(1)
                auto = f"{c['Warning']} workloads completed with warnings"
                if top:
                    auto += f" (most frequent: {top[0][0]} — {top[0][1]}x)"
            rows.append({"tool": label, "total": sum(c.values()), "success": c["Success"],
                         "warning": c["Warning"], "failed": c["Failed"], "running": c["Running"],
                         "comment": " ".join(manual) or auto})
        return rows

    def _failures(self, finals, comments) -> list[dict]:
        fcfg = self.cfg.get("failures", {})
        collapse = fcfg.get("collapse_all_servers", True)
        sample = fcfg.get("history_sample_for_all_servers", 3)
        by_job: dict[tuple[str, str], list[Task]] = defaultdict(list)
        failed_by_job: dict[tuple[str, str], list[Task]] = defaultdict(list)
        for (server, job, _obj), t in finals.items():
            by_job[(server, job)].append(t)
            if status_of(t.result, t.state) == "Failed":
                failed_by_job[(server, job)].append(t)

        rows = []
        for (server, job), failed in sorted(failed_by_job.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            tool = self.tool_of(server)
            loc = self.location_of(job, server)
            all_failed = collapse and len(failed) >= 2 and len(failed) == len(by_job[(server, job)])
            groups = [("All Servers", failed)] if all_failed else [(t.obj, [t]) for t in sorted(failed, key=lambda x: x.obj)]
            for name, tasks in groups:
                consec = [self.consecutive_failures(server, job, t.obj) for t in tasks[:sample]]
                consec = max((c for c in consec if c is not None), default=None)
                match = next((c for c in comments if c.scope == "failure" and _match(c.job, job)
                              and (_match(c.server, name) or c.server in ("*", ""))
                              and (_match(c.tool, tool["label"]) or _match(c.tool, tool.get("short", "")))),
                             None)
                rows.append({
                    "location": loc, "server": name if name != "All Servers" else f"All Servers ({len(tasks)})",
                    "tool": "Veeam", "vbr": server, "job": job, "consecutive": consec,
                    "mitigation": match.mitigation if match else "",
                    "comment": match.comment if match else "",
                    "needs_comment": match is None,
                    "error": _first_line(tasks[0].reason),
                })
        return rows

    def _final_by_job(self, sessions: list[JobSession]) -> dict[tuple[str, str], JobSession]:
        out = {}
        for s in sessions:
            k = (s.server, s.job)
            if k not in out or s.created > out[k].created:
                out[k] = s
        return out

    def _protection(self, data: Collected) -> list[dict]:
        rows = []
        for label, sessions in (("Backup Copy", self._sessions_in_scope(data.job_sessions, "backup_copy")),
                                ("DR Replication", self._sessions_in_scope(data.replica_sessions, None))):
            counts = Counter(self.tool_of(s.server).get("short") for s in sessions)
            failed_jobs = sorted(j for (srv, j), s in self._final_by_job(sessions).items()
                                 if status_of(s.result, s.state) == "Failed")
            if failed_jobs:
                shown = ", ".join(failed_jobs[:5]) + ("…" if len(failed_jobs) > 5 else "")
                status = f"{len(failed_jobs)} job(s) failed: {shown}"
            else:
                status = "Healthy"
            rows.append({"type": label, "counts": counts, "status": status, "ok": not failed_jobs})
        return rows

    def _jobs_attention(self, data: Collected) -> list[dict]:
        jc = self.cfg.get("jobs_check", {})
        ignore = [re.compile(p) for p in jc.get("ignore_regex", [])]
        ran = {(s.server, s.job) for s in data.job_sessions + data.replica_sessions if s.created >= self.start}
        out = []
        for j in sorted(data.jobs, key=lambda x: (x.name, x.server)):
            if any(p.search(j.name) for p in ignore):
                continue
            tool = self.tool_of(j.server)["label"]
            if jc.get("flag_disabled", True) and j.schedule_configured and not j.schedule_enabled:
                out.append({"tool": tool, "vbr": j.server, "job": j.name, "issue": "Schedule disabled"})
            elif (jc.get("flag_not_run", True) and j.schedule_enabled
                  and (j.server, j.name) not in ran):
                out.append({"tool": tool, "vbr": j.server, "job": j.name,
                            "issue": f"Enabled but no session since {self.report_date:%d/%m}"})
        return out

    def _repos(self, data: Collected) -> list[dict]:
        threshold = self.cfg.get("repositories", {}).get("free_space_warning_pct", 10)
        return [{"tool": self.tool_of(r.server)["label"], "name": r.name,
                 "free_pct": round(r.free_pct, 1), "free_tb": round(r.free / 1024 ** 4, 2),
                 "capacity_tb": round(r.capacity / 1024 ** 4, 2)}
                for r in sorted(data.repos, key=lambda r: r.free_pct)
                if r.capacity and r.free_pct < threshold]
