"""
Daily Veeam backup report.

    python -m vdr --config config.yaml
    python -m vdr --config config.yaml --send
    python -m vdr --config config.yaml --demo
    python -m vdr --config config.yaml --date 2026-10-26 --dump-raw out/raw
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import smtplib
import sys
import time

from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

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

    msg = MIMEMultipart("alternative")

    msg["Subject"] = subject
    msg["From"] = smtp["from"]
    msg["To"] = ", ".join(
        smtp["to"]
    )

    if smtp.get("cc"):
        msg["Cc"] = ", ".join(
            smtp["cc"]
        )

    if body_text:
        msg.attach(
            MIMEText(
                body_text,
                "plain",
                "utf-8"
            )
        )

    html_content = html

    if body_text:

        html_body = body_text.replace(
            "\n",
            "<br>"
        )

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

    msg.attach(
        MIMEText(
            html_content,
            "html",
            "utf-8"
        )
    )

    with smtplib.SMTP(
        smtp["host"],
        smtp.get("port", 25),
        timeout=60
    ) as s:

        if smtp.get("starttls"):
            s.starttls()

        smtp_user = os.getenv(
            "SMTP_USER"
        )

        smtp_password = os.getenv(
            "SMTP_PASSWORD"
        )

        if smtp_user and smtp_password:
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
            msg.as_string()
        )

    log.info(
        "Report sent to %s",
        msg["To"]
    )


def main(argv=None) -> int:

    run_start = time.time()

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

    if not config_path.exists():
        raise RuntimeError(
            f"Configuration file not found: {args.config}"
        )

    cfg = yaml.safe_load(
        config_path.read_text(
            encoding="utf-8"
        )
    )

    required_sections = [
        "enterprise_manager",
        "tools",
        "smtp"
    ]

    for section in required_sections:

        if section not in cfg:
            raise RuntimeError(
                f"Missing configuration section: {section}"
            )

    tz = ZoneInfo(
        cfg.get(
            "report",
            {}
        ).get(
            "timezone",
            "UTC"
        )
    )

    now = datetime.now(
        timezone.utc
    )

    report_date = (
        date.fromisoformat(args.date)
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
            load_comments(
                cfg.get("comments_file"),
                report_date
            )
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

    html_file.write_text(
        html,
        encoding="utf-8"
    )

    json_file.write_text(
        json.dumps(
            model,
            indent=2,
            default=str
        ),
        encoding="utf-8"
    )

    if not html_file.exists() or html_file.stat().st_size == 0:
        raise RuntimeError(
            "report.html not generated"
        )

    if not json_file.exists() or json_file.stat().st_size == 0:
        raise RuntimeError(
            "report.json not generated"
        )

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

        subject = smtp_cfg.get(
            "subject",
            f"{model['title']} - {model['date_str']}"
        ).format(
            report_date=model["date_str"]
        )

        try:

            body_text = smtp_cfg.get(
                "body",
                ""
            ).format(
                report_date=model["date_str"],
                success_count=success_count,
                warning_count=warning_count,
                failure_count=failure_count
            )

        except KeyError as exc:

            raise RuntimeError(
                f"Invalid SMTP body placeholder: {exc}"
            )

        for attempt in range(3):

            try:

                send_mail(
                    cfg,
                    subject,
                    html,
                    body_text
                )

                break

            except Exception:

                log.exception(
                    "Email attempt %s failed",
                    attempt + 1
                )

                if attempt == 2:
                    raise

                time.sleep(30)

    duration = round(
        time.time() - run_start,
        2
    )

    log.info(
        "Execution completed in %.2f seconds",
        duration
    )

    return 0
if __name__ == "__main__":
    sys.exit(main())