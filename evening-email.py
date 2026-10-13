#!/usr/bin/env python3
"""Send the large-cap inside-day screen and top option volume at 8:00 PM Central.

OCC's cleared volume is not up at the 3:16 PM report, so this waits until that
file exists and then sends a separate email. It retries every few minutes until
11:00 PM Central.
"""

import datetime
import fcntl
import importlib.util
import smtplib
import sys
import time
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CONFIG = Path.home() / ".config" / "evening-options-email"
EMAIL_STAMP = CONFIG / "last-email-date"
CLOSED_STAMP = CONFIG / "last-closed-date"
EMAIL_LOG = Path.home() / ".config" / "screeners-email" / "email.log"
EMAIL_PASSWORD_WARNING = CONFIG / "last-password-warning"
LOCK_FILE = CONFIG / "run.lock"
CENTRAL = ZoneInfo("America/Chicago")
SEND_AFTER = (20, 0)
GIVE_UP = (23, 0)
SCREENS = (
    ("Large cap double inside days", ROOT / "large-cap-tightness" / "screener.py"),
    ("Top 10 options volume", ROOT / "options-volume-screener" / "screener.py"),
)
HELPER = ROOT / "inside-day-screener" / "screener.py"


def email_log(message: str) -> None:
    EMAIL_LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(CENTRAL).isoformat(timespec="seconds")
    with EMAIL_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {message}\n")


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def already_handled(today: str) -> bool:
    return EMAIL_STAMP.exists() and EMAIL_STAMP.read_text(encoding="utf-8").strip() == today


def already_closed(today: str) -> bool:
    return CLOSED_STAMP.exists() and CLOSED_STAMP.read_text(encoding="utf-8").strip() == today


def mark_handled(today: str, modules: list) -> None:
    CONFIG.mkdir(parents=True, exist_ok=True)
    EMAIL_STAMP.write_text(today + "\n", encoding="utf-8")
    for module in modules:
        module.mark_handled(today)


def mark_closed(today: str) -> None:
    CONFIG.mkdir(parents=True, exist_ok=True)
    CLOSED_STAMP.write_text(today + "\n", encoding="utf-8")


def acquire_lock():
    CONFIG.mkdir(parents=True, exist_ok=True)
    handle = LOCK_FILE.open("w")
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def due(now: datetime.datetime) -> bool:
    today = now.date().isoformat()
    if now.weekday() >= 5 or (now.hour, now.minute) < SEND_AFTER:
        return False
    if already_handled(today) or already_closed(today):
        return False
    return True


def send_email(daily, screen_date: str, sections: list[dict], problems: list[str], password: str, recipient: str, helper, started: float) -> None:
    text_parts = [f"Options Report {screen_date}", ""]
    if sections or problems:
        text_parts.append("After the close")
        text_parts.append("")
        for section in sections:
            text_parts.append(section["title"])
            text_parts.append(section["body"])
            text_parts.append("")
        text_parts.extend(problems)
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(recipient, password)
        summary = f"Completed in {time.perf_counter() - started:.2f} seconds"
        text_parts.append(summary)
        message = EmailMessage()
        message["From"] = recipient
        message["To"] = recipient
        message["Subject"] = f"Options Report {screen_date}"
        message.set_content("\n".join(text_parts).strip() + "\n")
        message.add_alternative(
            daily.report_html(
                screen_date,
                sections,
                problems,
                summary,
                helper,
                heading="Options Report",
                eyebrow="OPTIONS · LARGE CAP · VOLUME",
                part_title="After the close",
            ),
            subtype="html",
        )
        smtp.send_message(message)


def send_scheduled_email() -> int:
    now = datetime.datetime.now(CENTRAL)
    if not due(now):
        return 0

    lock = acquire_lock()
    try:
        now = datetime.datetime.now(CENTRAL)
        today = now.date().isoformat()
        if already_handled(today) or already_closed(today):
            return 0
        if (now.hour, now.minute) >= GIVE_UP:
            email_log(f"Evening window for {today} closed before the options report was sent")
            mark_closed(today)
            return 0

        daily = load_module(ROOT / "daily-email.py", "daily_email")
        options = load_module(SCREENS[1][1], "options_volume")
        try:
            published = bool(options.fetch_volumes(now.date()))
        except Exception as error:
            email_log(f"Option volume check failed: {error}")
            return 0
        if not published:
            email_log(f"Option volume for {today} is not published yet")
            return 0

        started = time.perf_counter()
        modules = [load_module(path, f"evening_screen_{index}") for index, (_title, path) in enumerate(SCREENS)]
        helper = load_module(HELPER, "inside_day_helper")
        env = helper.load_env()
        password = env.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
        recipient = env.get("EMAIL_TO", "").strip()
        if not password or not recipient:
            warned = (
                EMAIL_PASSWORD_WARNING.exists()
                and EMAIL_PASSWORD_WARNING.read_text(encoding="utf-8").strip() == today
            )
            if not warned:
                email_log("Gmail app password or EMAIL_TO missing; not sending the options report")
                EMAIL_PASSWORD_WARNING.write_text(today + "\n", encoding="utf-8")
            return 0

        sections = []
        pending = []
        for (title, _path), module in zip(SCREENS, modules):
            try:
                result = daily.run_screen(module, now.date())
            except Exception as error:
                pending.append(f"{title} did not finish")
                email_log(f"{title} failed: {error}")
                continue
            if result["screen_date"] != today:
                pending.append(f"{title} is not ready yet")
                continue
            sections.append({"title": title, "body": result["body"]})

        if pending:
            email_log("; ".join(pending))
            return 0
        if not sections:
            email_log(f"No options screens ready for {today}; not sending")
            return 0
        send_email(daily, today, sections, pending, password, recipient, helper, started)
        mark_handled(today, modules)
        names = ", ".join(section["title"] for section in sections)
        email_log(f"Sent {today} options report ({names})")
        return 0
    except Exception as error:
        email_log(f"Options report failed: {error}")
        raise
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    raise SystemExit(send_scheduled_email())
