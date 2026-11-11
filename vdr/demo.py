"""Synthetic data reproducing the current manual report, to preview the output without EM access."""
from __future__ import annotations

import itertools
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .model import Collected, Job, JobSession, Repo, Task

_ids = itertools.count(1)
WARN = ("Failed to prepare guest for hot backup: VSS snapshot timeout. Guest processing skipped, "
        "crash-consistent backup created.")
JAB_ERR = "Error: Failed to write data to the file [\\\\JAB_SL52T141\\Backups\\...vbk]. Storage device I/O error."
ZUC_ERR = "Error: There is not enough space on the disk. Failed to write data to target repository."


class DemoCollector:
    def __init__(self, report_date: date, tz: str):
        self.tz = ZoneInfo(tz)
        self.day = report_date
        self._history: dict[tuple[str, str, str], list[Task]] = {}

    def _at(self, hh: int, mm: int = 0, day: date | None = None) -> datetime:
        return datetime.combine(day or self.day, time(hh, mm), tzinfo=self.tz)

    def _session(self, server, job, jtype, start, result="Success", state="Stopped", retry=False):
        return JobSession(f"urn:veeam:BackupJobSession:{next(_ids):08d}", server, job, jtype,
                          start, start + timedelta(hours=1), state, result, retry)

    def collect(self, since=None) -> Collected:
        sessions, replicas, tasks, jobs = [], [], [], []

        def backup_job(server, job, n_objects, outcome, prefix, start_hh=22):
            """outcome(i) -> (result, state, reason)."""
            s = self._session(server, job, "Backup", self._at(start_hh))
            sessions.append(s)
            jobs.append(Job(server, job, "Backup", True, True))
            for i in range(n_objects):
                result, state, reason = outcome(i)
                tasks.append(Task(s.uid, server, job, f"{prefix}{i:04d}", s.created + timedelta(minutes=i % 50),
                                  None, state, result, reason))
            return s

        # ---- DCU: 424 workloads, 240 success / 179 warning / 5 running
        def dcu(i):
            if i < 179:
                return "Warning", "Stopped", WARN
            if i < 184:
                return "None", "Working", ""
            return "Success", "Stopped", ""
        for j in range(8):
            backup_job("vbr-dcu01", f"FFM-B-VM-{j + 1:02d}", 53, lambda i, j=j: dcu(i + 53 * j), f"FFMV{j}")

        # ---- Premium JAB (vbr-prm01)
        def jab_lr(i):
            if i in (0, 1):
                return "Failed", "Stopped", JAB_ERR
            if i < 30:
                return "Warning", "Stopped", WARN
            if i < 45:
                return "None", "Working", ""
            return "Success", "Stopped", ""
        lr = backup_job("vbr-prm01", "JAB-B-LR", 300, jab_lr, "SWJL")
        # rename the two failing objects to match the real report
        failing = {"SWJL0000": "SW02B102", "SWJL0001": "SW37H162"}
        for t in tasks:
            if t.session_uid == lr.uid and t.obj in failing:
                t.obj = failing[t.obj]
        # first attempt: 12 more objects failed, the retry at 23:30 fixed them
        retry = self._session("vbr-prm01", "JAB-B-LR", "Backup", self._at(23, 30), retry=True)
        sessions.append(retry)
        for i in range(2, 14):
            obj = f"SWJL{i:04d}"
            tasks.append(Task(retry.uid, "vbr-prm01", "JAB-B-LR", obj, self._at(23, 31), None, "Stopped", "Success"))
        for name in failing.values():
            tasks.append(Task(retry.uid, "vbr-prm01", "JAB-B-LR", name, self._at(23, 32), None, "Stopped", "Failed", JAB_ERR))

        def jab_sr(i):
            if i == 0:
                return "Failed", "Stopped", JAB_ERR
            if i < 20:
                return "Warning", "Stopped", WARN
            if i < 33:
                return "None", "Working", ""
            return "Success", "Stopped", ""
        sr = backup_job("vbr-prm01", "JAB-B-SR", 200, jab_sr, "SWJS")
        for t in tasks:
            if t.session_uid == sr.uid and t.obj == "SWJS0000":
                t.obj = "SW50Y445"
        backup_job("vbr-prm01", "JAB-B-SQL", 100, lambda i: ("Warning", "Stopped", WARN) if i < 6
                   else ("Success", "Stopped", ""), "SQLJ")

        # ---- Premium ZUC (vbr-prm02): two jobs fully failed on low space
        backup_job("vbr-prm02", "ZUC-B-LR", 20, lambda i: ("Failed", "Stopped", ZUC_ERR), "SWZL", 21)
        backup_job("vbr-prm02", "ZUC-B-SR", 26, lambda i: ("Failed", "Stopped", ZUC_ERR), "SWZS", 21)
        backup_job("vbr-prm02", "ZUC-B-APP", 314, lambda i: ("Warning", "Stopped", WARN) if i < 10
                   else ("Success", "Stopped", ""), "SWZA")

        # ---- Backup copy and replication sessions
        for n, server, loc in ((172, "vbr-dcu01", "DCU"), (2044, "vbr-prm01", "JAB"), (2044, "vbr-prm02", "ZUC")):
            for i in range(n):
                sessions.append(self._session(server, f"{loc}-BCJ-{i % 6}",
                                              "BackupSync", self._at(0) + timedelta(seconds=20 * i)))
        for i in range(97):
            replicas.append(self._session("vbr-dcu01", f"FFM-DR-{i % 4}", "Replica", self._at(1) + timedelta(minutes=10 * i)))
        for i in range(2):
            replicas.append(self._session("vbr-prm01", "JAB-DR-CRIT", "Replica", self._at(2 + 10 * i)))
        for name, server in (("DCU-BCJ-0", "vbr-dcu01"), ("FFM-DR-0", "vbr-dcu01"), ("JAB-DR-CRIT", "vbr-prm01")):
            jobs.append(Job(server, name, "BackupSync" if "BCJ" in name else "Replica", True, True))

        # ---- jobs someone forgot to re-enable / that did not run
        jobs.append(Job("vbr-prm02", "ZUC-B-SQL-LOG", "Backup", True, False))
        jobs.append(Job("vbr-prm01", "JAB-B-FILESRV", "Backup", True, True))

        repos = [Repo("vbr-prm01", "JAB_SL52T141", 120 * 1024 ** 4, 41 * 1024 ** 4),
                 Repo("vbr-prm02", "ZUC_SL05Q663", 80 * 1024 ** 4, 4.4 * 1024 ** 4),
                 Repo("vbr-dcu01", "FFM_SL89Q840", 200 * 1024 ** 4, 61 * 1024 ** 4)]

        self._build_history(tasks)
        return Collected(sessions, replicas, tasks, jobs, repos)

    def _build_history(self, tasks: list[Task]) -> None:
        for t in tasks:
            if t.result != "Failed":
                continue
            key = (t.server, t.job, t.obj)
            fail_days = 5 if t.job.startswith("JAB") else 1
            hist = []
            for d in range(0, 8):
                day = self.day - timedelta(days=d)
                res = "Failed" if d < fail_days else "Success"
                hist.append(Task(t.session_uid, t.server, t.job, t.obj, self._at(22, day=day), None, "Stopped", res))
            self._history[key] = hist

    def history(self, server: str, job: str, obj: str, max_items: int = 60) -> list[Task]:
        return self._history.get((server, job, obj), [])
