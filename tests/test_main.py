from email import message_from_string
from email.policy import default
from pathlib import Path
from unittest.mock import Mock

import pytest

from vdr import __main__ as app


def valid_config():
    return {
        "enterprise_manager": {"url": "https://em.example.test"},
        "tools": [{"label": "Production", "servers": ["vbr01"]}],
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
    config["smtp"]["auth"] = {"enabled": True}
    config["smtp"]["starttls"] = False
    with pytest.raises(RuntimeError, match="STARTTLS is required"):
        app._validate_config(config, send=True)

    config["smtp"]["starttls"] = True
    with pytest.raises(RuntimeError, match="SMTP_USER and SMTP_PASSWORD are required"):
        app._validate_config(config, send=True)


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
    html_part = next(part for part in message.walk() if part.get_content_type() == "text/html")
    assert sender == "report@example.test"
    assert recipients == ["recipient@example.test", "hidden@example.test"]
    assert "Bcc" not in message
    assert "&lt;script&gt;" in html_part.get_content()
    assert "<script>" not in html_part.get_content()
    plain_text = next(
        part.get_content() for part in message.walk() if part.get_content_type() == "text/plain"
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


def test_send_mail_requires_tls_for_authentication():
    config = valid_config()
    config["smtp"]["starttls"] = False
    config["smtp"]["auth"] = {"enabled": True}
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
