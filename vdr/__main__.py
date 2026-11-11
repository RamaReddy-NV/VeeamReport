"""
Daily Veeam backup report.

    python -m vdr --config config.yaml
    python -m vdr --config config.yaml --send
    python -m vdr --config config.yaml --demo
    python -m vdr --config config.yaml --date 2026-10-26 --dump-raw out/raw
"""

from __future__ import annotations

import argparse
import email.policy
import json
import logging
import os
import smtplib
import ssl
import sys
import tempfile
import time

from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

from jinja2 import (
    Environment,
    FileSystemLoader,
    select_autoescape
)

from .build import (
    ReportBuilder,
    load_comments,
    report_window
)

log = logging.getLogger("vdr")


def _report_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be a valid YYYY-MM-DD value") from exc
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD format")
    return parsed


def _validate_config(cfg: object, send: bool = False) -> dict:
    if not isinstance(cfg, dict):
        raise RuntimeError("Configuration file must contain a YAML mapping")

    required_sections = ("enterprise_manager", "tools", "smtp")
    for section in required_sections:
        if section not in cfg:
            raise RuntimeError(f"Missing configuration section: {section}")

    em = cfg["enterprise_manager"]
    if not isinstance(em, dict) or not isinstance(em.get("url"), str) or not em["url"].strip():
        raise RuntimeError("enterprise_manager.url must be configured")
    if "verify_tls" in em and not isinstance(em["verify_tls"], bool):
        raise RuntimeError("enterprise_manager.verify_tls must be a boolean")
    page_size = em.get("page_size", 1000)
    if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size < 1:
        raise RuntimeError("enterprise_manager.page_size must be a positive integer")
    if "report" in cfg and not isinstance(cfg["report"], dict):
        raise RuntimeError("report must be a configuration mapping")
    if "comments_file" in cfg and cfg["comments_file"] is not None and not isinstance(cfg["comments_file"], str):
        raise RuntimeError("comments_file must be a path string")

    tools = cfg["tools"]
    if not isinstance(tools, list) or not tools:
        raise RuntimeError("tools must be a non-empty list")
    for index, tool in enumerate(tools, 1):
        if (
            not isinstance(tool, dict)
            or not isinstance(tool.get("label"), str)
            or not tool["label"].strip()
            or not isinstance(tool.get("servers"), list)
            or not tool["servers"]
            or not all(isinstance(server, str) and server.strip() for server in tool["servers"])
        ):
            raise RuntimeError(f"tools entry {index} must have a label and non-empty server list")

    smtp = cfg["smtp"]
    if not isinstance(smtp, dict):
        raise RuntimeError("smtp must be a configuration mapping")
    if "auth" in smtp and not isinstance(smtp["auth"], dict):
        raise RuntimeError("smtp.auth must be a configuration mapping")
    auth = smtp.get("auth", {})
    if "enabled" in auth and not isinstance(auth["enabled"], bool):
        raise RuntimeError("smtp.auth.enabled must be a boolean")
    if "enabled" in smtp and not isinstance(smtp["enabled"], bool):
        raise RuntimeError("smtp.enabled must be a boolean")
    if "starttls" in smtp and not isinstance(smtp["starttls"], bool):
        raise RuntimeError("smtp.starttls must be a boolean")
    for key in ("to", "cc", "bcc"):
        recipients = smtp.get(key, [])
        if (
            not isinstance(recipients, list)
            or not all(isinstance(recipient, str) and recipient.strip() for recipient in recipients)
        ):
            raise RuntimeError(f"smtp.{key} must be a list of non-empty email addresses")
    if send:
        if not smtp.get("enabled", True):
            raise RuntimeError("Email sending was requested but smtp.enabled is false")
        for key in ("host", "from"):
            if not isinstance(smtp.get(key), str) or not smtp[key].strip():
                raise RuntimeError(f"smtp.{key} must be configured when sending email")
        if not isinstance(smtp.get("to"), list) or not smtp["to"]:
            raise RuntimeError("smtp.to must contain at least one recipient when sending email")
        port = smtp.get("port", 25)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise RuntimeError("smtp.port must be an integer between 1 and 65535")
        if auth.get("enabled"):
            if not smtp.get("starttls"):
                raise RuntimeError("SMTP STARTTLS is required when SMTP authentication is enabled")
            if not (os.getenv("SMTP_USER") and os.getenv("SMTP_PASSWORD")):
                raise RuntimeError("SMTP_USER and SMTP_PASSWORD are required when smtp.auth.enabled is true")
        for key in ("subject", "body"):
            if key in smtp and not isinstance(smtp[key], str):
                raise RuntimeError(f"smtp.{key} must be a string")

    return cfg


def render(model: dict) -> str:

    env = Environment(
        loader=FileSystemLoader(
            Path(__file__).parent / "templates"
        ),
        autoescape=select_autoescape(
            ["html", "j2"]
        )
    )

    return env.get_template(
        "report.html.j2"
    ).render(**model)


def send_mail(
    cfg: dict,
    subject: str,
    html: str,
    body_text: str = ""
) -> None:

    smtp = cfg["smtp"]
    msg = EmailMessage(policy=email.policy.SMTP)
    msg["Subject"] = subject
    msg["From"] = smtp["from"]
    msg["To"] = ", ".join(smtp["to"])
    if smtp.get("cc"):
        msg["Cc"] = ", ".join(smtp["cc"])

    msg.set_content(body_text or "Please view the HTML report.")
    if body_text:
        html_body = escape(body_text).replace("\n", "<br>\n")
        html_content = f"""
        <html>
        <body>
            <div style="font-family:Calibri,Arial,sans-serif;font-size:14px;">
                {html_body}
            </div>

            <br>
            <hr>
            <br>

            {html}
        </body>
        </html>
        """
    else:
        html_content = html
    msg.add_alternative(html_content, subtype="html")

    auth = smtp.get("auth", {})
    if auth.get("enabled") and not smtp.get("starttls"):
        raise RuntimeError("SMTP STARTTLS is required when SMTP authentication is enabled")
    smtp_user: str | None = os.getenv("SMTP_USER")
    smtp_password: str | None = os.getenv("SMTP_PASSWORD")
    if auth.get("enabled") and not (smtp_user and smtp_password):
        raise RuntimeError("SMTP_USER and SMTP_PASSWORD are required when smtp.auth.enabled is true")
    with smtplib.SMTP(
        smtp["host"],
        smtp.get("port", 25),
        timeout=60
    ) as s:

        if smtp.get("starttls"):
            s.starttls(context=ssl.create_default_context())

        if auth.get("enabled"):
            assert smtp_user is not None and smtp_password is not None
            s.login(
                smtp_user,
                smtp_password
            )

        recipients = (
            smtp["to"]
            + smtp.get("cc", [])
            + smtp.get("bcc", [])
        )

        s.sendmail(
            smtp["from"],
            recipients,
            msg.as_string(policy=email.policy.SMTP)
        )

    log.info(
        "Report sent to %s",
        msg["To"]
    )


def _atomic_write_text(path: Path, content: str) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main(argv=None) -> int:

    run_start = time.monotonic()

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    ap.add_argument(
        "--config",
        default="config.yaml"
    )

    ap.add_argument(
        "--date",
        type=_report_date,
        help="report day YYYY-MM-DD (default: yesterday in report timezone)"
    )

    ap.add_argument(
        "--out",
        default="out",
        help="output folder for report.html / report.json"
    )

    ap.add_argument(
        "--demo",
        action="store_true",
        help="use synthetic data instead of Enterprise Manager"
    )

    ap.add_argument(
        "--send",
        action="store_true",
        help="e-mail the report (smtp section of config)"
    )

    ap.add_argument(
        "--dump-raw",
        metavar="DIR",
        help="save first page of EM API results"
    )

    ap.add_argument(
        "-v",
        "--verbose",
        action="store_true"
    )

    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    config_path = Path(args.config)

    try:
        cfg = _validate_config(
            yaml.safe_load(config_path.read_text(encoding="utf-8")),
            send=args.send,
        )
    except OSError as exc:
        raise RuntimeError(f"Unable to read configuration file {args.config!r}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Invalid YAML in configuration file {args.config!r}: {exc}") from exc

    try:
        tz = ZoneInfo(cfg.get("report", {}).get("timezone", "UTC"))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("report.timezone must be a valid IANA timezone") from exc

    now = datetime.now(
        timezone.utc
    )

    report_date = (
        args.date
        if args.date
        else (
            now.astimezone(tz).date()
            - timedelta(days=1)
        )
    )

    start, _ = report_window(
        report_date,
        tz
    )

    if args.demo:

        from .demo import DemoCollector

        collector = DemoCollector(
            report_date,
            tz.key
        )

        client = None

    else:

        from .collector_em import (
            EMClient,
            EMCollector
        )

        em = cfg["enterprise_manager"]

        user = os.getenv(
            "VEEAM_EM_USER"
        )

        password = os.getenv(
            "VEEAM_EM_PASSWORD"
        )

        if not user:
            raise RuntimeError(
                "VEEAM_EM_USER not defined"
            )

        if not password:
            raise RuntimeError(
                "VEEAM_EM_PASSWORD not defined"
            )

        client = EMClient(
            em["url"],
            user,
            password,
            verify_tls=em.get(
                "verify_tls",
                True
            ),
            page_size=em.get(
                "page_size",
                1000
            ),
            dump_dir=(
                Path(args.dump_raw)
                if args.dump_raw
                else None
            )
        )

        collector = EMCollector(
            client
        )

    try:

        data = collector.collect(
            since=start - timedelta(hours=1)
        )

        builder = ReportBuilder(
            cfg,
            report_date,
            now,
            history_fn=collector.history
        )

        model = builder.build(
            data,
            load_comments(_comments_path(cfg.get("comments_file"), config_path), report_date)
        )

    finally:

        if client:
            client.close()

    out = Path(args.out)

    out.mkdir(
        parents=True,
        exist_ok=True
    )

    html = render(model)

    html_file = out / "report.html"
    json_file = out / "report.json"

    _atomic_write_text(html_file, html)
    _atomic_write_text(json_file, json.dumps(model, indent=2, default=str))

    log.info(
        "Report written to %s",
        html_file
    )

    for warning in model.get(
        "data_warnings",
        []
    ):
        log.warning(
            warning
        )

    if args.send:

        summary = model.get(
            "summary",
            []
        )

        success_count = sum(
            row.get("success", 0)
            for row in summary
        )

        warning_count = sum(
            row.get("warning", 0)
            for row in summary
        )

        failure_count = sum(
            row.get("failed", 0)
            for row in summary
        )

        smtp_cfg = cfg.get(
            "smtp",
            {}
        )

        try:
            subject = smtp_cfg.get(
                "subject",
                f"{model['title']} - {model['date_str']}"
            ).format(report_date=model["date_str"])
            body_text = smtp_cfg.get(
                "body",
                ""
            ).format(
                report_date=model["date_str"],
                success_count=success_count,
                warning_count=warning_count,
                failure_count=failure_count
            )

        except (IndexError, KeyError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid SMTP subject/body template: {exc}"
            ) from exc

        for attempt in range(3):

            try:

                send_mail(
                    cfg,
                    subject,
                    html,
                    body_text
                )

                break

            except smtplib.SMTPResponseException as exc:
                if not 400 <= exc.smtp_code < 500 or attempt == 2:
                    raise
                log.warning(
                    "Transient SMTP response %s on attempt %s",
                    exc.smtp_code,
                    attempt + 1,
                )
                time.sleep(min(5 * (2 ** attempt), 30))
            except (OSError, smtplib.SMTPServerDisconnected) as exc:
                if attempt == 2:
                    raise
                log.warning("Transient email delivery failure on attempt %s: %s", attempt + 1, exc)
                time.sleep(min(5 * (2 ** attempt), 30))

    duration = round(
        time.monotonic() - run_start,
        2
    )

    log.info(
        "Execution completed in %.2f seconds",
        duration
    )

    return 0


def _comments_path(value: str | None, config_path: Path) -> str | None:
    if not value:
        return value
    path = Path(value)
    if not path.is_absolute():
        path = config_path.parent / path
    return str(path)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (
        RuntimeError,
        ValueError,
        OSError,
        yaml.YAMLError,
        requests.RequestException,
        smtplib.SMTPException,
    ) as exc:
        logging.basicConfig(
            level=logging.ERROR,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        log.error("%s", exc)
        sys.exit(2)