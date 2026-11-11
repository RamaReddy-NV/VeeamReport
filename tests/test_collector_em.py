from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from vdr.collector_em import (
    EMClient,
    EMCollector,
    _entities,
    _job_from_session_name,
    _link_name,
    _quote,
)
from vdr.model import parse_ts


def test_em_client_initializes_connection_and_logs_in(tmp_path, monkeypatch):
    session = Mock()
    session.headers = {}
    response = Mock()
    response.headers = {"X-RestSvcSessionId": "session-token"}
    response.json.return_value = {"SessionId": "logon-id"}
    session.post.return_value = response
    monkeypatch.setattr("vdr.collector_em.requests.Session", lambda: session)

    client = EMClient(
        "https://em.example.test:9398/",
        "report-user",
        "report-password",
        verify_tls=False,
        page_size=250,
        dump_dir=tmp_path,
    )

    assert client.base == "https://em.example.test:9398"
    assert client.timeout == 60
    assert client.page_size == 250
    assert client.dump_dir == Path(tmp_path)
    assert client._logon_id == "logon-id"
    assert client.http.headers["X-RestSvcSessionId"] == "session-token"
    assert client.http.verify is False
    assert session.mount.call_count == 2
    session.post.assert_called_once_with(
        "https://em.example.test:9398/sessionMngr/?v=latest",
        auth=("report-user", "report-password"),
        timeout=60,
    )

    client.close()


@pytest.mark.parametrize(
    "url,page_size",
    [
        ("http://em.example.test", 100),
        ("https://user:password@em.example.test", 100),
        ("https://em.example.test/path?token=value", 100),
        ("https://em.example.test", 0),
        ("https://em.example.test", True),
    ],
)
def test_em_client_rejects_unsafe_or_invalid_options(url, page_size):
    with pytest.raises(ValueError):
        EMClient(url, "user", "password", page_size=page_size)


def test_em_client_closes_session_when_login_fails(monkeypatch):
    session = Mock()
    session.post.side_effect = requests.ConnectionError("connection refused")
    monkeypatch.setattr("vdr.collector_em.requests.Session", lambda: session)

    with pytest.raises(requests.ConnectionError):
        EMClient("https://em.example.test", "user", "password")

    session.close.assert_called_once()


def test_em_client_query_pages_and_dumps_first_page(tmp_path):
    client = object.__new__(EMClient)
    client.base = "https://em.example.test"
    client.timeout = 60
    client.page_size = 100
    client.dump_dir = tmp_path

    first = Mock()
    first.json.return_value = {
        "Entities": {"BackupJobSession": [{"UID": "first"}]},
        "PagingInfo": {"PagesCount": 2},
    }
    second = Mock()
    second.json.return_value = {
        "Entities": {"BackupJobSession": [{"UID": "second"}]},
        "PagingInfo": {"PagesCount": 2},
    }
    client.http = Mock()
    client.http.get.side_effect = [first, second]

    rows = list(client.query("BackupJobSession", sort_desc="CreationTime"))

    assert rows == [{"UID": "first"}, {"UID": "second"}]
    assert client.http.get.call_count == 2
    assert client.http.get.call_args_list[0].kwargs["params"] == {
        "type": "BackupJobSession",
        "format": "Entities",
        "pageSize": 100,
        "page": 1,
        "sortDesc": "CreationTime",
    }
    assert (tmp_path / "BackupJobSession.json").is_file()


@pytest.mark.parametrize(
    "response,expected_message",
    [
        ("http", "query failed"),
        ("json", "invalid JSON"),
    ],
)
def test_em_client_query_reports_api_errors(response, expected_message):
    client = object.__new__(EMClient)
    client.base = "https://em.example.test"
    client.timeout = 60
    client.page_size = 100
    client.dump_dir = None
    client.http = Mock()
    result = Mock()
    result.status_code = 503
    if response == "http":
        result.raise_for_status.side_effect = requests.HTTPError("unavailable")
    else:
        result.json.side_effect = ValueError("not JSON")
    client.http.get.return_value = result

    with pytest.raises(RuntimeError, match=expected_message):
        list(client.query("BackupJobSession"))


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Backup Job@session (Retry 2)", "Backup Job"),
        ("Backup Job@session", "Backup Job"),
        (None, "?"),
    ],
)
def test_session_name_normalization(value, expected):
    assert _job_from_session_name(value) == expected


def test_enterprise_manager_payload_helpers():
    entity = {
        "Links": {
            "Link": [
                {"Type": "BackupServerReference", "Name": "vbr01"},
            ],
        },
    }

    assert _link_name(entity, "BackupServerReference") == "vbr01"
    assert _link_name(entity, "Missing") is None
    assert _entities({"Entities": {"Items": [{"UID": "one"}]}}) == [{"UID": "one"}]
    assert _entities({"Entities": {"Items": {"Nested": [{"UID": "two"}]}}}) == [
        {"UID": "two"}
    ]
    assert _entities({"Entities": []}) == []
    assert _quote("plain") == "plain"
    assert _quote("job name") == '"job name"'
    with pytest.raises(ValueError, match="missing the Entities collection"):
        _entities({})


def test_em_collector_maps_sessions_tasks_and_history():
    created = "2026-01-01T10:00:00Z"
    links = [
        {"Type": "BackupServerReference", "Name": "vbr01"},
        {"Type": "JobReference", "Name": "PAR-Backup"},
        {"Type": "BackupJobSessionReference", "Name": "PAR-Backup@session"},
    ]
    session = {
        "UID": "urn:veeam:BackupJobSession:1",
        "Links": links,
        "Name": "PAR-Backup@session",
        "JobType": "Backup",
        "CreationTimeUTC": created,
        "EndTimeUTC": created,
        "State": "Stopped",
        "Result": "Success",
    }
    task = {
        "JobSessionUid": session["UID"],
        "Links": links,
        "VmDisplayName": "workload01",
        "CreationTimeUTC": created,
        "EndTimeUTC": created,
        "State": "Stopped",
        "Result": "Failed",
        "Reason": "disk full",
    }

    class StubClient(EMClient):
        def __init__(self):
            self.calls = []

        def query(self, qtype, **kwargs):
            self.calls.append((qtype, kwargs))
            if qtype in ("BackupJobSession", "ReplicaJobSession"):
                return iter([session] if qtype == "BackupJobSession" else [])
            if qtype == "BackupTaskSession":
                return iter([task])
            if qtype == "Job":
                return iter([{
                    "Links": links,
                    "Name": "PAR-Backup",
                    "JobType": "Backup",
                    "ScheduleConfigured": True,
                    "ScheduleEnabled": False,
                }])
            if qtype == "Repository":
                return iter([{
                    "Links": links,
                    "Name": "repo",
                    "Capacity": "100",
                    "FreeSpace": "25",
                }])
            return iter([])

    client = StubClient()
    collector = EMCollector(client)
    since = parse_ts(created)
    assert since is not None
    data = collector.collect(since)

    assert data.job_sessions[0].job == "PAR-Backup"
    assert data.job_sessions[0].server == "vbr01"
    assert data.tasks[0].obj == "workload01"
    assert data.tasks[0].reason == "disk full"
    assert data.jobs[0].schedule_enabled is False
    assert data.repos[0].free == 25

    history = collector.history("vbr01", "PAR-Backup", "workload01")
    assert len(history) == 1
    assert history[0].result == "Failed"
    assert client.calls[-1][1]["filter"] == "VmDisplayName==workload01"


def test_em_collector_rejects_records_without_required_timestamps():
    collector = EMCollector(Mock())
    with pytest.raises(ValueError, match="missing required timestamp CreationTimeUTC"):
        collector._job_session({"UID": "missing-time"})
    with pytest.raises(ValueError, match="missing required timestamp CreationTimeUTC"):
        collector._task({"UID": "missing-time"})
    with pytest.raises(ValueError, match="invalid timestamp CreationTimeUTC"):
        collector._job_session({"UID": "invalid-time", "CreationTimeUTC": "not-a-date"})
