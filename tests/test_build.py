from datetime import date
from datetime import datetime
from datetime import timezone
from zoneinfo import ZoneInfo

import pytest

from vdr.build import (
    load_comments,
    report_window,
    status_of,
)
from vdr.build import ReportBuilder


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
