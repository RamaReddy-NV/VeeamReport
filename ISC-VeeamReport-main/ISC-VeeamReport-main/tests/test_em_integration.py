from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from vdr.__main__ import _validate_config
from vdr.build import DONE_STATES, ReportBuilder, report_window, status_of
from vdr.collector_em import EMClient, EMCollector, _quote
from vdr.model import JobSession, parse_ts

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_EM_INTEGRATION", "").lower() != "true",
        reason="Set RUN_EM_INTEGRATION=true to query the live Enterprise Manager",
    ),
]


def _report_date(timezone_name: str) -> date:
    value = os.getenv("EM_INTEGRATION_REPORT_DATE")
    if value:
        return date.fromisoformat(value)
    return datetime.now(ZoneInfo(timezone_name)).date() - timedelta(days=1)


def _older_than(start: datetime) -> Callable[[dict], bool]:
    def stop(entity: dict) -> bool:
        created = parse_ts(entity.get("CreationTimeUTC"))
        return created is not None and created < start

    return stop


def _assert_newest_first(client: EMClient, query_type: str) -> list[dict]:
    rows = list(
        client.query(
            query_type,
            sort_desc="CreationTime",
            page_size=100,
            max_pages=1,
        )
    )
    assert len(rows) > 1, f"{query_type}: need at least two records to verify sorting"

    timestamps = [parse_ts(row.get("CreationTimeUTC")) for row in rows]
    assert all(ts is not None for ts in timestamps), (
        f"{query_type}: a first-page record has no valid CreationTimeUTC"
    )
    valid_timestamps = [ts for ts in timestamps if ts is not None]
    assert all(
        newer >= older for newer, older in zip(valid_timestamps, valid_timestamps[1:])
    ), f"{query_type}: sortDesc=CreationTime did not return newest-first data"
    return rows


def _assert_retry_flag(
    raw_sessions: list[dict],
    sessions: list[JobSession],
    start: datetime,
    end: datetime,
) -> str:
    missing_flags = [row for row in raw_sessions if "IsRetry" not in row]
    assert not missing_flags, (
        f"IsRetry is absent from {len(missing_flags)} recent job-session records"
    )
    invalid_flags = [
        row for row in raw_sessions if not isinstance(row.get("IsRetry"), bool)
    ]
    assert not invalid_flags, (
        f"IsRetry is not a JSON boolean in {len(invalid_flags)} records"
    )

    requested_job = os.getenv("EM_INTEGRATION_RETRY_JOB", "").strip()
    retries = [
        session
        for session in sessions
        if session.is_retry
        and session.created >= end
        and (not requested_job or session.job.casefold() == requested_job.casefold())
    ]
    assert retries, (
        "No after-midnight retry was found for the report date. "
        "Set EM_INTEGRATION_REPORT_DATE and, if needed, EM_INTEGRATION_RETRY_JOB."
    )

    retry = retries[0]
    assert any(
        not session.is_retry
        and session.server == retry.server
        and session.job == retry.job
        and start <= session.created < end
        for session in sessions
    ), f"Retry {retry.job!r} has no matching session in the report window"
    return retry.job


def _assert_schedule_fields(client: EMClient) -> str:
    requested_job = os.getenv("EM_INTEGRATION_DISABLED_JOB", "").strip()
    disabled_jobs = [
        job
        for job in client.query("Job")
        if job.get("ScheduleConfigured") is True
        and job.get("ScheduleEnabled") is False
        and (
            not requested_job
            or str(job.get("Name", "")).casefold() == requested_job.casefold()
        )
    ]
    assert disabled_jobs, (
        "No job with ScheduleConfigured=true and ScheduleEnabled=false was found. "
        "Set EM_INTEGRATION_DISABLED_JOB to a known disabled job if necessary."
    )
    return str(disabled_jobs[0].get("Name", "unknown"))


def test_live_em_assumptions() -> None:
    config_path = Path(os.getenv("VEEAM_EM_CONFIG", "config.yaml"))
    cfg = _validate_config(
        yaml.safe_load(config_path.read_text(encoding="utf-8")),
    )
    user = os.getenv("VEEAM_EM_USER")
    password = os.getenv("VEEAM_EM_PASSWORD")
    if not user or not password:
        pytest.fail("VEEAM_EM_USER and VEEAM_EM_PASSWORD are required")

    timezone_name = cfg.get("report", {}).get("timezone", "UTC")
    report_date = _report_date(timezone_name)
    start, end = report_window(report_date, ZoneInfo(timezone_name))
    collection_start = start - timedelta(hours=1)
    em_config = cfg["enterprise_manager"]
    dump_dir = Path(os.getenv("EM_INTEGRATION_DUMP_DIR", ".em-integration/raw"))

    with EMClient(
        em_config["url"],
        user,
        password,
        verify_tls=em_config.get("verify_tls", True),
        page_size=em_config.get("page_size", 1000),
        dump_dir=dump_dir,
    ) as client:
        first_pages = {
            query_type: _assert_newest_first(client, query_type)
            for query_type in (
                "BackupJobSession",
                "ReplicaJobSession",
                "BackupTaskSession",
            )
        }

        raw_sessions = list(
            client.query(
                "BackupJobSession",
                sort_desc="CreationTime",
                stop=_older_than(collection_start),
            )
        )
        data = EMCollector(client).collect(collection_start)
        retry_job = _assert_retry_flag(
            raw_sessions,
            data.job_sessions,
            start,
            end,
        )

        task_rows = first_pages["BackupTaskSession"]
        workloads = sorted(
            {
                str(row["VmDisplayName"])
                for row in task_rows
                if isinstance(row.get("VmDisplayName"), str) and row["VmDisplayName"]
            }
        )
        assert len(workloads) > 1, (
            "The first BackupTaskSession page needs at least two workloads "
            "to verify that the server applies VmDisplayName filtering"
        )
        workload = workloads[0]
        filtered_tasks = list(
            client.query(
                "BackupTaskSession",
                filter=f"VmDisplayName=={_quote(workload)}",
                sort_desc="CreationTime",
                page_size=100,
                max_pages=1,
            )
        )
        assert filtered_tasks, f"VmDisplayName filter returned no rows for {workload!r}"
        assert all(task.get("VmDisplayName") == workload for task in filtered_tasks), (
            "VmDisplayName filter returned records for other workloads"
        )

        observed_states = {
            str(row["State"]).strip()
            for rows in first_pages.values()
            for row in rows
            if row.get("State") is not None and str(row["State"]).strip()
        }
        assert observed_states, "No State values were returned in the first pages"

        confirmed_terminal_states = {
            state.strip().lower()
            for state in os.getenv("EM_INTEGRATION_TERMINAL_STATES", "").split(",")
            if state.strip()
        }
        state_values = sorted(observed_states | confirmed_terminal_states)
        state_results = {state: status_of("", state) for state in state_values}
        for state, result in state_results.items():
            expected = "Failed" if state.lower() in DONE_STATES else "Running"
            assert result == expected

        missing_terminal_states = confirmed_terminal_states - DONE_STATES
        assert not missing_terminal_states, (
            "Console-confirmed finished states are missing from DONE_STATES: "
            f"{sorted(missing_terminal_states)}"
        )

        configured_job_types = {
            str(job_type).casefold()
            for category in cfg.get("job_types", {}).values()
            for job_type in category
        }
        observed_job_types = {
            session.job_type
            for session in data.job_sessions
            if start <= session.created < end
        }
        unmapped_job_types = {
            job_type
            for job_type in observed_job_types
            if job_type.casefold() not in configured_job_types
        }
        assert not unmapped_job_types, (
            f"Unmapped JobType values in the report window: "
            f"{sorted(unmapped_job_types)}"
        )

        report_model = ReportBuilder(
            cfg,
            report_date,
            datetime.now(timezone.utc),
        ).build(data, [])
        unmapped_servers = [
            warning
            for warning in report_model["data_warnings"]
            if "not mapped to a tool" in warning
        ]
        assert not unmapped_servers, f"Unmapped server references: {unmapped_servers}"
        unmapped_type_warnings = [
            warning
            for warning in report_model["data_warnings"]
            if warning.startswith("Job type ")
        ]
        assert not unmapped_type_warnings, (
            f"Unmapped job types appear in data quality warnings: "
            f"{unmapped_type_warnings}"
        )

        disabled_job = _assert_schedule_fields(client)
        print(
            "LIVE EM ASSUMPTIONS: "
            + json.dumps(
                {
                    "first_page_newest_first": {
                        query_type: len(rows)
                        for query_type, rows in first_pages.items()
                    },
                    "after_midnight_retry_job": retry_job,
                    "filtered_workload": workload,
                    "observed_states": sorted(observed_states),
                    "state_classification": state_results,
                    "console_confirmed_terminal_states": sorted(
                        confirmed_terminal_states
                    ),
                    "terminal_state_review": (
                        "explicit states were confirmed against the VBR console"
                        if confirmed_terminal_states
                        else "manual VBR console review required"
                    ),
                    "job_types_in_report_window": sorted(observed_job_types),
                    "disabled_job": disabled_job,
                    "unmapped_servers": [],
                    "unmapped_job_types": [],
                    "raw_dumps": str(dump_dir),
                },
                sort_keys=True,
            )
        )
