#!/usr/bin/env python3
"""Send the inside-day, double inside-day, 9 SMA, large-cap inside-day, and top options-volume screens in one email."""

import datetime
import fcntl
import importlib.util
import re
import smtplib
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from email.message import EmailMessage
from html import escape
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CONFIG = Path.home() / ".config" / "screeners-email"
EMAIL_STAMP = CONFIG / "last-email-date"
EMAIL_LOG = CONFIG / "email.log"
EMAIL_PASSWORD_WARNING = CONFIG / "last-password-warning"
LOCK_FILE = CONFIG / "run.lock"
EASTERN = ZoneInfo("America/New_York")
SEND_AFTER = (16, 16)
GIVE_UP = (18, 0)
SCREENS = (
    ("Inside day", ROOT / "inside-day-screener" / "screener.py"),
    ("Double inside day", ROOT / "double-inside-day-screener" / "screener.py"),
    ("9 SMA bounce", ROOT / "9sma-screener" / "screener.py"),
    ("Large cap inside day", ROOT / "large-cap-inside-screener" / "screener.py"),
    ("Top 10 options volume", ROOT / "options-volume-screener" / "screener.py"),
)


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def email_log(message: str) -> None:
    CONFIG.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(EASTERN).isoformat(timespec="seconds")
    with EMAIL_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {message}\n")


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def already_handled(today: str) -> bool:
    return EMAIL_STAMP.exists() and EMAIL_STAMP.read_text(encoding="utf-8").strip() == today


def mark_handled(today: str, modules: list) -> None:
    CONFIG.mkdir(parents=True, exist_ok=True)
    EMAIL_STAMP.write_text(today + "\n", encoding="utf-8")
    for module in modules:
        module.mark_handled(today)


def acquire_lock():
    CONFIG.mkdir(parents=True, exist_ok=True)
    handle = LOCK_FILE.open("w")
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def run_screen(module, today: datetime.date) -> dict:
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = module.run_screen(today)
    if code != 0:
        detail = stderr.getvalue().strip()[-500:]
        raise RuntimeError(detail or f"{module.__name__} exited {code}")
    dates = re.findall(r"^(\d{4}-\d{2}-\d{2}): \d+ symbols", stderr.getvalue(), re.MULTILINE)
    lines = [
        line
        for line in stdout.getvalue().splitlines()
        if not line.startswith("Completed in")
    ]
    body = "\n".join(lines).strip("\n")
    if not body.strip():
        body = "No stocks matched."
    return {"screen_date": dates[0] if dates else "", "body": body}


def section_html(module, title: str, body: str) -> str:
    columns, rows, elapsed = module.parse_table(body)
    heading = f'<h2 style="font-family:Arial,sans-serif;font-size:18px;">{escape(title)}</h2>'
    if not columns:
        return heading + f'<p style="font-family:Arial,sans-serif;">{escape(body)}</p>'
    return heading + module.html_table(columns, rows, elapsed)


def send_email(
    screen_date: str,
    sections: list[dict],
    problems: list[str],
    password: str,
    recipient: str,
    module,
    started: float,
) -> None:
    text_parts = [f"Stock screens for {screen_date}", ""]
    html_parts = [
        "<div>",
        f'<p style="font-family:Arial,sans-serif;">Stock screens for {escape(screen_date)}</p>',
    ]
    for section in sections:
        text_parts.append(section["title"])
        text_parts.append(section["body"])
        text_parts.append("")
        html_parts.append(section_html(module, section["title"], section["body"]))
    for problem in problems:
        text_parts.append(problem)
        html_parts.append(f'<p style="font-family:Arial,sans-serif;">{escape(problem)}</p>')
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(recipient, password)
        summary = f"Completed in {time.perf_counter() - started:.2f} seconds"
        text_parts.append(summary)
        html_parts.append(f'<p style="font-family:Arial,sans-serif;">{escape(summary)}</p>')
        message = EmailMessage()
        message["From"] = recipient
        message["To"] = recipient
        message["Subject"] = f"Daily Report {screen_date}"
        message.set_content("\n".join(text_parts).strip() + "\n")
        message.add_alternative("".join(html_parts) + "</div>", subtype="html")
        smtp.send_message(message)


def send_scheduled_email() -> int:
    now = datetime.datetime.now(EASTERN)
    today = now.date().isoformat()
    if now.weekday() >= 5 or (now.hour, now.minute) < SEND_AFTER or already_handled(today):
        return 0

    lock = acquire_lock()
    try:
        now = datetime.datetime.now(EASTERN)
        today = now.date().isoformat()
        if already_handled(today):
            return 0
        started = time.perf_counter()
        modules = [
            load_module(path, f"screen_{index}")
            for index, (_title, path) in enumerate(SCREENS)
        ]
        helper = modules[0]
        env = helper.load_env()
        password = env.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
        recipient = env.get("EMAIL_TO", "").strip()
        if not password or not recipient:
            warned = (
                EMAIL_PASSWORD_WARNING.exists()
                and EMAIL_PASSWORD_WARNING.read_text(encoding="utf-8").strip() == today
            )
            if not warned:
                email_log("Gmail app password or EMAIL_TO missing; not sending")
                EMAIL_PASSWORD_WARNING.write_text(today + "\n", encoding="utf-8")
            return 0

        sections = []
        pending = []
        for (title, _path), module in zip(SCREENS, modules):
            try:
                result = run_screen(module, now.date())
            except Exception as error:
                pending.append(f"{title} did not finish")
                email_log(f"{title} failed: {error}")
                continue
            if result["screen_date"] != today:
                pending.append(f"{title} is not ready yet")
                continue
            sections.append({
                "title": title,
                "body": result["body"],
            })

        if pending and (now.hour, now.minute) < GIVE_UP:
            email_log("; ".join(pending))
            return 0
        if not sections:
            mark_handled(today, modules)
            email_log(f"No screens ready for {today}; not sending")
            return 0
        send_email(today, sections, pending, password, recipient, helper, started)
        mark_handled(today, modules)
        names = ", ".join(section["title"] for section in sections)
        email_log(f"Sent {today} combined screen ({names})")
        return 0
    except Exception as error:
        email_log(f"Email run failed: {error}")
        raise
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    raise SystemExit(send_scheduled_email())
