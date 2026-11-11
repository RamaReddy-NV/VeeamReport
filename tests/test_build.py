from datetime import date
from datetime import datetime
from datetime import timezone
from zoneinfo import ZoneInfo

import pytest

from vdr.build import (
    Comment,
    load_comments,
    report_window,
    status_of,
)
from vdr.build import ReportBuilder
from vdr.model import Collected, Job, JobSession, Repo, Task


@pytest.mark.parametrize(
    "result,state,expected",
    [
        ("Success", "Stopped", "Success"),
        ("Warning", "Stopped", "Warning"),
        ("Failed", "Stopped", "Failed"),
        ("", "Working", "Running"),
        ("", "Stopped", "Failed"),
        (None, "Working", "Running"),
        (None, "Stopped", "Failed"),
    ],
)
def test_status_matrix(result, state, expected):
    assert status_of(result, state) == expected


def test_report_window_utc():
    start, end = report_window(
        date(2026, 1, 1),
        ZoneInfo("UTC"),
    )

    assert start.hour == 0
    assert start.tzinfo == timezone.utc
    assert (end - start).days == 1


def test_comments_missing_file():
    comments = load_comments(
        "does_not_exist.csv",
        date(2026, 1, 1),
    )

    assert comments == []


def test_comments_expiration(tmp_path):
    comments_file = tmp_path / "comments.csv"

    comments_file.write_text(
        (
            "scope,tool,job,server,mitigation,comment,expires\n"
            "failure,*,*,*,na,test,2020-01-01\n"
        ),
        encoding="utf-8",
    )

    comments = load_comments(
        str(comments_file),
        date(2026, 1, 1),
    )

    assert comments == []


def test_comments_valid_record(tmp_path):
    comments_file = tmp_path / "comments.csv"

    comments_file.write_text(
        (
            "scope,tool,job,server,mitigation,comment,expires\n"
            "failure,*,*,*,restart,test comment,2030-01-01\n"
        ),
        encoding="utf-8",
    )

    comments = load_comments(
        str(comments_file),
        date(2026, 1, 1),
    )

    assert len(comments) == 1

    comment = comments[0]

    assert comment.scope == "failure"
    assert comment.comment == "test comment"
    assert comment.mitigation == "restart"


def test_comments_invalid_expiry(tmp_path):
    comments_file = tmp_path / "comments.csv"

    comments_file.write_text(
        (
            "scope,tool,job,server,mitigation,comment,expires\n"
            "failure,*,*,*,restart,test,INVALID\n"
        ),
        encoding="utf-8",
    )

    comments = load_comments(
        str(comments_file),
        date(2026, 1, 1),
    )

    assert comments == []


def build_cfg():
    return {
        "report": {
            "timezone": "UTC"
        },
        "tools": [
            {
                "label": "Veeam Prod",
                "short": "PROD",
                "servers": ["vbr01"]
            }
        ],
        "job_types": {
            "backup": [
                "vmware backup"
            ]
        },
        "location": {
            "default": "PAR"
        },
    }


def create_builder():
    return ReportBuilder(
        cfg=build_cfg(),
        report_date=date(2026, 1, 1),
        now=datetime.now(timezone.utc),
    )


def test_tool_mapping():
    builder = create_builder()

    tool = builder.tool_of("vbr01")

    assert tool["label"] == "Veeam Prod"


def test_tool_mapping_unmapped_server():
    builder = create_builder()

    tool = builder.tool_of("unknown-server")

    assert "Unmapped" in tool["label"]
    assert len(builder.data_warnings) == 1


def test_unmapped_server_warning_message():
    builder = create_builder()

    builder.tool_of("unknown-server")

    assert len(builder.data_warnings) == 1
    assert "unknown-server" in builder.data_warnings[0]


def test_category_mapping():
    builder = create_builder()

    assert builder.category("vmware backup") == "backup"


def test_category_unmapped():
    builder = create_builder()

    assert builder.category("something-new") == "ignored"
    assert builder.data_warnings


def test_location_default():
    builder = create_builder()

    assert builder.location_of(
        "BackupJob",
        "vbr01",
    ) == "PAR"


def test_location_from_job_name():
    cfg = build_cfg()

    cfg["location"]["job_name_regex"] = r"^([A-Z]{3})-"

    builder = ReportBuilder(
        cfg=cfg,
        report_date=date(2026, 1, 1),
        now=datetime.now(timezone.utc),
    )

    assert builder.location_of(
        "LON-Nightly",
        "vbr01",
    ) == "LON"


def test_build_summary_failures_comments_and_orphan_warning():
    builder = create_builder()
    builder.history_fn = lambda server, job, obj: [
        Task(
            "history",
            server,
            job,
            obj,
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            None,
            "Stopped",
            "Failed",
        ),
        Task(
            "history",
            server,
            job,
            obj,
            datetime(2025, 12, 31, tzinfo=timezone.utc),
            None,
            "Stopped",
            "Failed",
        ),
        Task(
            "history",
            server,
            job,
            obj,
            datetime(2025, 12, 30, tzinfo=timezone.utc),
            None,
            "Stopped",
            "Success",
        ),
    ]
    session = JobSession(
        "urn:veeam:BackupJobSession:1",
        "vbr01",
        "PAR-Backup",
        "vmware backup",
        datetime(2026, 1, 1, 10, tzinfo=timezone.utc),
        None,
        "Stopped",
        "Success",
    )
    retry = JobSession(
        "urn:veeam:BackupJobSession:2",
        "vbr01",
        "PAR-Backup",
        "vmware backup",
        datetime(2026, 1, 2, 1, tzinfo=timezone.utc),
        None,
        "Stopped",
        "Success",
        is_retry=True,
    )
    tasks = [
        Task(session.uid, "vbr01", "PAR-Backup", "vm-success",
             datetime(2026, 1, 1, 11, tzinfo=timezone.utc), None, "Stopped", "Failed"),
        Task(retry.uid, "vbr01", "PAR-Backup", "vm-success",
             datetime(2026, 1, 2, 1, tzinfo=timezone.utc), None, "Stopped", "Success"),
        Task(session.uid, "vbr01", "PAR-Backup", "vm-failed",
             datetime(2026, 1, 1, 12, tzinfo=timezone.utc), None, "Stopped", "Failed",
             "first error\nadditional details"),
        Task("", "vbr01", "PAR-Backup", "orphan",
             datetime(2026, 1, 1, 12, tzinfo=timezone.utc), None, "Stopped", "Success"),
    ]
    comments = [
        Comment("summary", "PROD", "*", "*", "", "Reviewed", None),
        Comment("failure", "PROD", "PAR-*", "vm-failed", "Restarted", "Resolved", None),
        Comment("note", "*", "*", "*", "", "Maintenance completed", None),
    ]

    model = builder.build(
        Collected([session, retry], [], tasks, [], []),
        comments,
    )

    assert model["summary"][0] == {
        "tool": "Veeam Prod",
        "total": 2,
        "success": 1,
        "warning": 0,
        "failed": 1,
        "running": 0,
        "comment": "Reviewed",
    }
    assert model["failures"][0]["server"] == "vm-failed"
    assert model["failures"][0]["consecutive"] == 2
    assert model["failures"][0]["mitigation"] == "Restarted"
    assert model["failures"][0]["error"] == "first error"
    assert model["notes"] == ["Maintenance completed"]
    assert model["data_warnings"] == [
        "1 task sessions had no parent job session and were skipped"
    ]


def test_backup_copy_replication_attention_and_repository_alerts():
    cfg = build_cfg()
    cfg["job_types"]["backup_copy"] = ["BackupSync"]
    cfg["jobs_check"] = {
        "flag_disabled": True,
        "flag_not_run": True,
        "ignore_regex": ["^TEST-"],
    }
    cfg["repositories"] = {"free_space_warning_pct": 10}
    builder = ReportBuilder(
        cfg=cfg,
        report_date=date(2026, 1, 1),
        now=datetime.now(timezone.utc),
    )
    created = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
    copy = JobSession("copy", "vbr01", "PAR-Copy", "BackupSync",
                      created, None, "Stopped", "Failed")
    replica = JobSession("replica", "vbr01", "PAR-Replica", "Replica",
                         created, None, "Stopped", "Success")
    data = Collected(
        [copy],
        [replica],
        [],
        [
            Job("vbr01", "PAR-Disabled", "Backup", True, False),
            Job("vbr01", "PAR-NotRun", "Backup", True, True),
            Job("vbr01", "TEST-Ignore", "Backup", True, True),
        ],
        [
            Repo("vbr01", "low-space", 100, 5),
            Repo("vbr01", "healthy", 100, 20),
            Repo("vbr01", "empty", 0, 0),
        ],
    )

    protection = builder._protection(data)
    attention = builder._jobs_attention(data)
    repos = builder._repos(data)

    assert protection[0]["status"] == "1 job(s) failed: PAR-Copy"
    assert protection[1]["status"] == "Healthy"
    assert [row["job"] for row in attention] == ["PAR-Disabled", "PAR-NotRun"]
    assert repos[0]["name"] == "low-space"
    assert repos[0]["free_pct"] == 5.0
