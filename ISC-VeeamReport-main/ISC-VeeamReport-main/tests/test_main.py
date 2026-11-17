import json
import logging
import smtplib
import subprocess
import sys
from datetime import datetime, timezone
from email import message_from_string
from email.policy import default
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from vdr import __main__ as app
from vdr.model import Collected, JobSession, Task

UTC = timezone.utc


def valid_config():
    return {
        "enterprise_manager": {"url": "https://em.example.test"},
        "report": {"timezone": "UTC"},
        "tools": [{"label": "Production", "servers": ["vbr01"]}],
        "job_types": {"backup": ["Backup"]},
        "smtp": {
            "enabled": True,
            "host": "smtp.example.test",
            "port": 587,
            "starttls": True,
            "from": "report@example.test",
            "to": ["recipient@example.test"],
        },
    }


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-02-28", "2026-02-28"),
        ("2026-02-30", None),
        ("11/02/2026", None),
    ],
)
def test_report_date_validation(value, expected):
    if expected is None:
        with pytest.raises(app.argparse.ArgumentTypeError):
            app._report_date(value)
    else:
        assert app._report_date(value).isoformat() == expected


def test_config_validation_rejects_malformed_sections(monkeypatch):
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="YAML mapping"):
        app._validate_config(None)

    config = valid_config()
    config["tools"] = []
    with pytest.raises(RuntimeError, match="non-empty list"):
        app._validate_config(config)

    config = valid_config()
    config["smtp"]["port"] = 70000
    with pytest.raises(RuntimeError, match="smtp.port"):
        app._validate_config(config, send=True)

    config = valid_config()
    config["smtp"]["enabled"] = False
    with pytest.raises(RuntimeError, match="smtp.enabled is false"):
        app._validate_config(config, send=True)

    config = valid_config()
    config["smtp"]["starttls"] = False
    with pytest.raises(RuntimeError, match="STARTTLS is required when sending"):
        app._validate_config(config, send=True)

    config = valid_config()
    config["smtp"]["auth"] = {"enabled": True}
    config["smtp"]["starttls"] = False
    with pytest.raises(RuntimeError, match="STARTTLS is required"):
        app._validate_config(config, send=True)

    config["smtp"]["starttls"] = True
    with pytest.raises(RuntimeError, match="SMTP_USER and SMTP_PASSWORD are required"):
        app._validate_config(config, send=True)


def test_main_logs_structured_error_event(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    with pytest.raises(RuntimeError, match="Unable to read configuration"):
        app.main(["--config", str(tmp_path / "missing.yaml")])

    assert "event=vdr_run status=error" in caplog.text
    assert "error=" in caplog.text


def test_main_em_path_uses_environment_credentials_and_closes_client(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("VEEAM_EM_USER", "em-user")
    monkeypatch.setenv("VEEAM_EM_PASSWORD", "em-password")
    client = Mock()
    em_client = Mock(return_value=client)
    collector = Mock()
    collector.collect.side_effect = RuntimeError("EM collection failed")
    em_collector = Mock(return_value=collector)
    monkeypatch.setattr(app, "EMClient", em_client)
    monkeypatch.setattr(app, "EMCollector", em_collector)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(valid_config()), encoding="utf-8")

    with pytest.raises(RuntimeError, match="EM collection failed"):
        app.main(
            [
                "--config",
                str(config_path),
                "--date",
                "2026-01-01",
                "--out",
                str(tmp_path / "out"),
            ]
        )

    em_client.assert_called_once_with(
        "https://em.example.test",
        "em-user",
        "em-password",
        verify_tls=True,
        page_size=1000,
        dump_dir=None,
    )
    em_collector.assert_called_once_with(client)
    client.close.assert_called_once_with()


def test_main_cli_config_error_exits_two_with_one_error_line(tmp_path):
    config_path = tmp_path / "invalid.yaml"
    config_path.write_text("[]", encoding="utf-8")
    repo_root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [sys.executable, "-m", "vdr", "--config", str(config_path)],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    error_lines = [line for line in result.stderr.splitlines() if " ERROR " in line]
    assert result.returncode == 2
    assert len(error_lines) == 1, result.stderr
    assert "event=vdr_run status=error" in error_lines[0]


def test_send_mail_uses_tls_and_escapes_plain_text(monkeypatch):
    smtp = Mock()
    smtp.__enter__ = Mock(return_value=smtp)
    smtp.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(app.smtplib, "SMTP", Mock(return_value=smtp))
    monkeypatch.setenv("SMTP_USER", "smtp-user")
    monkeypatch.setenv("SMTP_PASSWORD", "smtp-password")

    config = valid_config()
    config["smtp"]["auth"] = {"enabled": True}
    config["smtp"]["bcc"] = ["hidden@example.test"]
    body = "<script>alert('x')</script>\nDaily report"

    app.send_mail(config, "Daily report", "<p>report</p>", body)

    smtp.starttls.assert_called_once()
    smtp.login.assert_called_once_with("smtp-user", "smtp-password")
    sender, recipients, raw_message = smtp.sendmail.call_args.args
    message = message_from_string(raw_message, policy=default)
    html_part = next(
        part for part in message.walk() if part.get_content_type() == "text/html"
    )
    assert sender == "report@example.test"
    assert recipients == ["recipient@example.test", "hidden@example.test"]
    assert "Bcc" not in message
    assert "&lt;script&gt;" in html_part.get_content()
    assert "<script>" not in html_part.get_content()
    plain_text = next(
        part.get_content()
        for part in message.walk()
        if part.get_content_type() == "text/plain"
    )
    assert body in plain_text.replace("\r\n", "\n")


def test_send_mail_requires_credentials_only_when_auth_enabled(monkeypatch):
    smtp = Mock()
    smtp.__enter__ = Mock(return_value=smtp)
    smtp.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(app.smtplib, "SMTP", Mock(return_value=smtp))
    monkeypatch.setenv("SMTP_USER", "unexpected-user")
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)

    app.send_mail(valid_config(), "Daily report", "<p>report</p>")
    smtp.login.assert_not_called()

    config = valid_config()
    config["smtp"]["auth"] = {"enabled": True}
    with pytest.raises(RuntimeError, match="SMTP_USER and SMTP_PASSWORD are required"):
        app.send_mail(config, "Daily report", "<p>report</p>")


@pytest.mark.parametrize("auth_enabled", [False, True])
def test_send_mail_requires_tls(auth_enabled):
    config = valid_config()
    config["smtp"]["starttls"] = False
    config["smtp"]["auth"] = {"enabled": auth_enabled}
    with pytest.raises(RuntimeError, match="STARTTLS is required"):
        app.send_mail(config, "Daily report", "<p>report</p>")


def test_atomic_write_replaces_complete_file(tmp_path):
    destination = tmp_path / "report.json"
    destination.write_text("partial", encoding="utf-8")

    app._atomic_write_text(destination, '{"complete": true}')

    assert destination.read_text(encoding="utf-8") == '{"complete": true}'
    assert list(tmp_path.iterdir()) == [destination]


def test_comments_path_is_relative_to_config_file(tmp_path):
    config_path = tmp_path / "settings" / "report.yaml"
    assert app._comments_path("comments.csv", config_path) == str(
        config_path.parent / "comments.csv"
    )
    assert app._comments_path(None, config_path) is None


def test_missing_tool_data_is_flagged_in_report_and_returns_failure(
    tmp_path, monkeypatch
):
    session = JobSession(
        "session-1",
        "vbr01",
        "PAR-Backup",
        "Backup",
        datetime(2026, 1, 1, 10, tzinfo=UTC),
        None,
        "Stopped",
        "Success",
    )
    task = Task(
        session.uid,
        session.server,
        session.job,
        "workload-1",
        datetime(2026, 1, 1, 11, tzinfo=UTC),
        None,
        "Stopped",
        "Success",
    )

    class StubDemoCollector:
        def __init__(self, *_):
            pass

        def collect(self, since=None):
            return Collected([session], [], [task], [], [])

        def history(self, *_):
            return []

    monkeypatch.setattr(app, "DemoCollector", StubDemoCollector)
    send_mail = Mock()
    monkeypatch.setattr(app, "send_mail", send_mail)
    config = {
        "enterprise_manager": {"url": "https://em.example.test"},
        "report": {"timezone": "UTC"},
        "tools": [
            {"label": "Production", "servers": ["vbr01"]},
            {"label": "Disaster Recovery", "servers": ["vbr02"]},
        ],
        "job_types": {"backup": ["Backup"]},
        "smtp": {
            "enabled": True,
            "host": "smtp.example.test",
            "from": "report@example.test",
            "to": ["recipient@example.test"],
            "starttls": True,
        },
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    output = tmp_path / "out"

    result = app.main(
        [
            "--config",
            str(config_path),
            "--date",
            "2026-01-01",
            "--demo",
            "--out",
            str(output),
            "--send",
        ]
    )

    assert result == 3
    send_mail.assert_called_once()
    html = (output / "report.html").read_text(encoding="utf-8")
    data = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert "NO DATA" in html
    assert "No failed workload after retries" not in html
    assert data["no_data_tools"] == ["Disaster Recovery"]


def test_demo_report_in_process_has_expected_totals_per_tool(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    output_dir = tmp_path / "out"

    result = app.main(
        [
            "--config",
            str(repo_root / "config.yaml"),
            "--demo",
            "--date",
            "2026-10-26",
            "--out",
            str(output_dir),
        ]
    )

    report = json.loads((output_dir / "report.json").read_text(encoding="utf-8"))
    totals = {row["tool"]: row["total"] for row in report["summary"]}

    assert result == 0
    assert totals == {
        "Veeam DCU": 424,
        "Veeam Premium": 600,
        "Veeam Premium DR": 360,
    }


def test_main_sends_report_with_subject_and_body_template(
    tmp_path, monkeypatch, caplog
):
    caplog.set_level(logging.INFO)
    session = JobSession(
        "session-1",
        "vbr01",
        "PAR-Backup",
        "Backup",
        datetime(2026, 1, 1, 10, tzinfo=UTC),
        None,
        "Stopped",
        "Success",
    )
    task = Task(
        session.uid,
        session.server,
        session.job,
        "workload-1",
        datetime(2026, 1, 1, 11, tzinfo=UTC),
        None,
        "Stopped",
        "Success",
    )

    class StubDemoCollector:
        def __init__(self, *_):
            pass

        def collect(self, since=None):
            return Collected([session], [], [task], [], [])

        def history(self, *_):
            return []

    monkeypatch.setattr(app, "DemoCollector", StubDemoCollector)
    send_mail = Mock()
    monkeypatch.setattr(app, "send_mail", send_mail)
    monkeypatch.setattr(app.time, "sleep", Mock())

    config = {
        "enterprise_manager": {"url": "https://em.example.test"},
        "report": {"timezone": "UTC"},
        "tools": [{"label": "Production", "servers": ["vbr01"]}],
        "job_types": {"backup": ["Backup"]},
        "smtp": {
            "enabled": True,
            "host": "smtp.example.test",
            "from": "report@example.test",
            "to": ["recipient@example.test"],
            "starttls": True,
            "subject": "[VEEAM] Daily Backup Report - {report_date}",
            "body": "Weekly summary for {report_date}: success={success_count}, "
            + "warnings={warning_count}, failures={failure_count}",
        },
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    result = app.main(
        [
            "--config",
            str(config_path),
            "--date",
            "2026-01-01",
            "--demo",
            "--send",
            "--out",
            str(tmp_path / "out"),
        ]
    )

    assert result == 0
    send_mail.assert_called_once()
    _, subject, html, body_text = send_mail.call_args.args
    assert subject == "[VEEAM] Daily Backup Report - 01/01/2026"
    assert "success=1, warnings=0, failures=0" in body_text
    assert "Weekly summary for 01/01/2026" in body_text
    assert html
    assert 'event=vdr_run status=ok report_date="2026-01-01"' in caplog.text


def test_send_retries_transient_4xx_then_succeeds(tmp_path, monkeypatch):
    session = JobSession(
        "session-1",
        "vbr01",
        "PAR-Backup",
        "Backup",
        datetime(2026, 1, 1, 10, tzinfo=UTC),
        None,
        "Stopped",
        "Success",
    )
    task = Task(
        session.uid,
        session.server,
        session.job,
        "workload-1",
        datetime(2026, 1, 1, 11, tzinfo=UTC),
        None,
        "Stopped",
        "Success",
    )

    class StubDemoCollector:
        def __init__(self, *_):
            pass

        def collect(self, since=None):
            return Collected([session], [], [task], [], [])

        def history(self, *_):
            return []

    monkeypatch.setattr(app, "DemoCollector", StubDemoCollector)
    sleep = Mock()
    monkeypatch.setattr(app.time, "sleep", sleep)

    send_mail = Mock(
        side_effect=[smtplib.SMTPResponseException(451, "Try later"), None]
    )
    monkeypatch.setattr(app, "send_mail", send_mail)

    config = valid_config()
    config["report"] = {"timezone": "UTC"}
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    result = app.main(
        [
            "--config",
            str(config_path),
            "--date",
            "2026-01-01",
            "--demo",
            "--send",
            "--out",
            str(tmp_path / "out"),
        ]
    )

    assert result == 0
    assert send_mail.call_count == 2
    sleep.assert_called_once_with(5)


def test_send_does_not_retry_5xx(monkeypatch):
    send_mail = Mock(side_effect=smtplib.SMTPResponseException(550, "Rejected"))
    sleep = Mock()
    monkeypatch.setattr(app, "send_mail", send_mail)
    monkeypatch.setattr(app.time, "sleep", sleep)

    with pytest.raises(smtplib.SMTPResponseException, match="Rejected"):
        app._send_report(
            valid_config(),
            {"summary": [], "title": "Report", "date_str": "01/01/2026"},
            "<p>report</p>",
        )

    send_mail.assert_called_once()
    sleep.assert_not_called()


def test_main_retries_transient_smtp_disconnect(tmp_path, monkeypatch):
    session = JobSession(
        "session-1",
        "vbr01",
        "PAR-Backup",
        "Backup",
        datetime(2026, 1, 1, 10, tzinfo=UTC),
        None,
        "Stopped",
        "Success",
    )
    task = Task(
        session.uid,
        session.server,
        session.job,
        "workload-1",
        datetime(2026, 1, 1, 11, tzinfo=UTC),
        None,
        "Stopped",
        "Success",
    )

    class StubDemoCollector:
        def __init__(self, *_):
            pass

        def collect(self, since=None):
            return Collected([session], [], [task], [], [])

        def history(self, *_):
            return []

    monkeypatch.setattr(app, "DemoCollector", StubDemoCollector)
    sleep = Mock()
    monkeypatch.setattr(app.time, "sleep", sleep)

    send_mail = Mock(
        side_effect=[smtplib.SMTPServerDisconnected("connection lost"), None]
    )
    monkeypatch.setattr(app, "send_mail", send_mail)

    config = valid_config()
    config["report"] = {"timezone": "UTC"}
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    result = app.main(
        [
            "--config",
            str(config_path),
            "--date",
            "2026-01-01",
            "--demo",
            "--send",
            "--out",
            str(tmp_path / "out"),
        ]
    )

    assert result == 0
    assert send_mail.call_count == 2
    sleep.assert_called_once_with(5)


def test_main_raises_after_exhausting_smtp_retries(tmp_path, monkeypatch):
    session = JobSession(
        "session-1",
        "vbr01",
        "PAR-Backup",
        "Backup",
        datetime(2026, 1, 1, 10, tzinfo=UTC),
        None,
        "Stopped",
        "Success",
    )
    task = Task(
        session.uid,
        session.server,
        session.job,
        "workload-1",
        datetime(2026, 1, 1, 11, tzinfo=UTC),
        None,
        "Stopped",
        "Success",
    )

    class StubDemoCollector:
        def __init__(self, *_):
            pass

        def collect(self, since=None):
            return Collected([session], [], [task], [], [])

        def history(self, *_):
            return []

    monkeypatch.setattr(app, "DemoCollector", StubDemoCollector)
    monkeypatch.setattr(app.time, "sleep", Mock())

    send_mail = Mock(
        side_effect=[
            smtplib.SMTPResponseException(451, "Temporary failure"),
            smtplib.SMTPResponseException(451, "Temporary failure"),
            smtplib.SMTPResponseException(451, "Temporary failure"),
        ]
    )
    monkeypatch.setattr(app, "send_mail", send_mail)

    config = valid_config()
    config["report"] = {"timezone": "UTC"}
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    with pytest.raises(smtplib.SMTPResponseException):
        app.main(
            [
                "--config",
                str(config_path),
                "--date",
                "2026-01-01",
                "--demo",
                "--send",
                "--out",
                str(tmp_path / "out"),
            ]
        )

    assert send_mail.call_count == 3
