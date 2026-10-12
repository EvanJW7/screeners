#!/usr/bin/env python3
"""Send the weekday screens in one email, then the S&P 500 and QQQ year-to-date leaders."""

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
    ("Inside days", ROOT / "inside-day-screener" / "screener.py"),
    ("Double inside days", ROOT / "double-inside-day-screener" / "screener.py"),
    ("9 SMA bounce candidates", ROOT / "9sma-screener" / "screener.py"),
    ("Large cap tightness", ROOT / "large-cap-inside-screener" / "screener.py"),
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


PAGE = "#070b10"
CARD = "#10161d"
INK = "#e7eef6"
MUTED = "#8b9aab"
LINE = "#243140"
UP = "#3ddc97"
DOWN = "#ff5d73"
AMBER = "#e2b657"
SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"


def report_date(screen_date: str) -> str:
    try:
        day = datetime.date.fromisoformat(screen_date)
    except ValueError:
        return screen_date
    return day.strftime("%A, %B %-d, %Y")


def quote_color(value: str) -> str | None:
    text = value.strip()
    if text.startswith("+"):
        return UP
    if text.startswith("-") and any(char.isdigit() for char in text):
        return DOWN
    return None


def is_figure(value: str) -> bool:
    return any(char.isdigit() for char in value)


def data_rows(rows: list[list[str]]) -> list[list[str]]:
    return [row for row in rows if any(cell.strip() for cell in row)]


def blotter_table(columns: list[str], rows: list[list[str]]) -> str:
    header = "".join(
        "<th style=\"padding:10px 12px;border-bottom:1px solid {line};background:{card};"
        "color:{muted};font-family:{sans};font-size:10px;font-weight:600;"
        "letter-spacing:0.08em;text-transform:uppercase;text-align:{align};"
        "white-space:nowrap;\">{name}</th>".format(
            line=LINE,
            card="#0c1218",
            muted=MUTED,
            sans=SANS,
            align="left" if column in {"Date", "Symbol", "Country"} else "right",
            name=escape(column),
        )
        for column in columns
    )
    body: list[str] = []
    shown = 0
    for row in rows:
        if not any(cell.strip() for cell in row):
            body.append(
                f'<tr><td colspan="{len(columns)}" style="height:8px;background:{PAGE};'
                f'border:none;padding:0;line-height:8px;">&nbsp;</td></tr>'
            )
            continue
        stripe = "#0d141b" if shown % 2 else CARD
        shown += 1
        cells: list[str] = []
        for column, value in zip(columns, row):
            color = quote_color(value) or INK
            if column == "Symbol":
                style = (
                    f"padding:9px 12px;background:{stripe};border-bottom:1px solid {LINE};"
                    f"color:{INK};font-family:{MONO};font-size:13px;font-weight:700;"
                    "text-align:left;white-space:nowrap;letter-spacing:0.04em;"
                )
            elif column == "Date":
                style = (
                    f"padding:9px 12px;background:{stripe};border-bottom:1px solid {LINE};"
                    f"color:{MUTED};font-family:{MONO};font-size:12px;text-align:left;white-space:nowrap;"
                )
            elif column == "Country" or not is_figure(value):
                style = (
                    f"padding:9px 12px;background:{stripe};border-bottom:1px solid {LINE};"
                    f"color:{INK};font-family:{SANS};font-size:12px;text-align:left;white-space:nowrap;"
                )
            else:
                weight = "700" if quote_color(value) else "500"
                style = (
                    f"padding:9px 12px;background:{stripe};border-bottom:1px solid {LINE};"
                    f"color:{color};font-family:{MONO};font-size:12px;font-weight:{weight};"
                    "text-align:right;white-space:nowrap;"
                )
            cells.append(f'<td style="{style}">{escape(value) or "&nbsp;"}</td>')
        body.append(f"<tr>{''.join(cells)}</tr>")
    return (
        "<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" "
        f"style=\"border-collapse:collapse;background:{CARD};\">"
        f"<thead><tr>{header}</tr></thead><tbody>{''.join(body)}</tbody></table>"
    )


def section_card(module, title: str, body: str) -> str:
    columns, rows, _elapsed = module.parse_table(body)
    names = data_rows(rows)
    count = f"{len(names)} name" + ("" if len(names) == 1 else "s")
    if not columns:
        count = "No matches"
        content = (
            f'<p style="margin:0;padding:16px 18px;color:{MUTED};font-family:{SANS};'
            f'font-size:13px;line-height:1.5;">{escape(body)}</p>'
        )
    else:
        content = blotter_table(columns, rows)
    return (
        "<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" "
        f"style=\"margin:0 0 18px;background:{CARD};border:1px solid {LINE};border-radius:12px;\">"
        "<tr><td style=\"padding:0;\">"
        "<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\">"
        "<tr>"
        f'<td style="padding:14px 18px 12px;border-bottom:1px solid {LINE};">'
        f'<div style="color:{INK};font-family:{SANS};font-size:18px;'
        f'font-weight:650;letter-spacing:-0.02em;">{escape(title)}</div>'
        "</td>"
        f'<td align="right" style="padding:14px 18px 12px;border-bottom:1px solid {LINE};'
        f'color:{MUTED};font-family:{MONO};font-size:12px;white-space:nowrap;">{count}</td>'
        "</tr></table>"
        f"{content}</td></tr></table>"
    )


OPTIONS_TITLES = {"Large cap tightness", "Top 10 options volume"}
GAINERS = ROOT / "ytd-gainers" / "gainers.py"


def part_heading(title: str, first: bool, eyebrow: str = "") -> str:
    gap = "6px" if first else "28px"
    label = ""
    if eyebrow:
        label = (
            f'<div style="color:{AMBER};font-family:{MONO};font-size:11px;letter-spacing:0.16em;">'
            f"{escape(eyebrow)}</div>"
        )
    title_gap = "margin-top:4px;" if eyebrow else ""
    return (
        f'<div style="margin:{gap} 6px 14px;">'
        f"{label}"
        f'<div style="{title_gap}color:{INK};font-family:{SANS};font-size:22px;'
        f'font-weight:650;letter-spacing:-0.02em;">{escape(title)}</div>'
        "</div>"
    )


def warning_html(problem: str) -> str:
    return (
        "<p style=\"margin:0 0 10px;padding:10px 14px;background:#2a2114;border:1px solid #6b5420;"
        f"border-radius:8px;color:{AMBER};font-family:{SANS};font-size:13px;\">{escape(problem)}</p>"
    )


def gainer_card(columns: list[str], title: str, rows: list[list[str]]) -> str:
    count = f"{len(rows)} name" + ("" if len(rows) == 1 else "s")
    content = blotter_table(columns, rows) if rows else (
        f'<p style="margin:0;padding:16px 18px;color:{MUTED};font-family:{SANS};font-size:13px;">No prices found.</p>'
    )
    return (
        "<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" "
        f"style=\"background:{CARD};border:1px solid {LINE};border-radius:12px;\">"
        "<tr><td style=\"padding:0;\">"
        "<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\">"
        "<tr>"
        f'<td style="padding:14px 14px 12px;border-bottom:1px solid {LINE};">'
        f'<div style="color:{INK};font-family:{SANS};font-size:18px;font-weight:650;">{escape(title)}</div>'
        "</td>"
        f'<td align="right" style="padding:14px 14px 12px;border-bottom:1px solid {LINE};'
        f'color:{MUTED};font-family:{MONO};font-size:12px;">{count}</td>'
        "</tr></table>"
        f"{content}</td></tr></table>"
    )


def gainer_pair(cards: list[str]) -> str:
    cells = []
    last = len(cards) - 1
    for index, card in enumerate(cards):
        if len(cards) == 1:
            gap = "padding:0 0 18px;"
        elif index == 0:
            gap = "padding:0 9px 18px 0;"
        elif index == last:
            gap = "padding:0 0 18px 9px;"
        else:
            gap = "padding:0 9px 18px;"
        cells.append(
            f'<td width="50%" valign="top" style="width:50%;vertical-align:top;{gap}">{card}</td>'
        )
    return (
        "<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\">"
        f"<tr>{''.join(cells)}</tr></table>"
    )


def gainer_text(columns: list[str], title: str, rows: list[list[str]]) -> str:
    lines = ["  ".join(columns)]
    for row in rows:
        lines.append("  ".join(row))
    return title + "\n" + "\n".join(lines)


def index_gainers(screen_day: datetime.date) -> tuple[str, str, list[str]]:
    """Format the S&P 500 and QQQ leaderboards from ytd-gainers."""
    module = load_module(GAINERS, "ytd_gainers")
    html_parts = []
    text_parts = []
    for title, rows in module.leaderboards(screen_day):
        html_parts.append(gainer_card(module.COLUMNS, title, rows))
        text_parts.append(gainer_text(module.COLUMNS, title, rows))
    heading = part_heading("Largest gainers", False, "YEAR TO DATE")
    return heading + gainer_pair(html_parts), "\n\n".join(text_parts), []


def split_report(sections: list[dict], problems: list[str]) -> tuple[list[dict], list[str], list[dict], list[str], list[str]]:
    stock_sections = []
    options_sections = []
    for section in sections:
        if section["title"] in OPTIONS_TITLES:
            options_sections.append(section)
        else:
            stock_sections.append(section)
    stock_problems = []
    options_problems = []
    other_problems = []
    for problem in problems:
        if problem.startswith(tuple(OPTIONS_TITLES)):
            options_problems.append(problem)
        elif any(problem.startswith(title) for title, _path in SCREENS if title not in OPTIONS_TITLES):
            stock_problems.append(problem)
        else:
            other_problems.append(problem)
    return stock_sections, stock_problems, options_sections, options_problems, other_problems


def part_html(module, title: str, sections: list[dict], problems: list[str], first: bool) -> str:
    if not sections and not problems:
        return ""
    cards = "".join(
        section_card(module, section["title"], section["body"])
        for section in sections
    )
    notes = "".join(warning_html(problem) for problem in problems)
    return part_heading(title, first) + cards + notes


def report_html(
    screen_date: str,
    sections: list[dict],
    problems: list[str],
    summary: str,
    module,
    gainer_html: str = "",
    gainer_problems: list[str] | None = None,
) -> str:
    stock_sections, stock_problems, options_sections, options_problems, other_problems = split_report(
        sections, problems
    )
    cards = part_html(module, "Stock plays", stock_sections, stock_problems, True)
    cards += part_html(module, "Options plays", options_sections, options_problems, not cards)
    cards += "".join(warning_html(problem) for problem in other_problems)
    cards += gainer_html
    cards += "".join(warning_html(problem) for problem in (gainer_problems or []))
    when = escape(report_date(screen_date))
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="color-scheme" content="dark">
<meta name="supported-color-schemes" content="dark">
</head>
<body bgcolor="{PAGE}" style="margin:0;padding:0;background:{PAGE};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="{PAGE}" style="background:{PAGE};">
<tr><td align="center" style="padding:28px 12px 36px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:1200px;">
<tr><td style="padding:0 6px 22px;border-bottom:1px solid {LINE};">
<div style="color:{AMBER};font-family:{MONO};font-size:11px;letter-spacing:0.16em;">EQUITY SCREENS · US LISTED · CLOSE</div>
<div style="margin-top:8px;color:{INK};font-family:{SANS};font-size:32px;font-weight:680;letter-spacing:-0.03em;">Daily Report</div>
<div style="margin-top:4px;color:{MUTED};font-family:{MONO};font-size:13px;">{when}</div>
</td></tr>
<tr><td style="padding-top:22px;">{cards}
<div style="padding-top:4px;color:{MUTED};font-family:{MONO};font-size:12px;letter-spacing:0.04em;">{escape(summary)}</div>
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""


def send_email(
    screen_date: str,
    sections: list[dict],
    problems: list[str],
    password: str,
    recipient: str,
    module,
    started: float,
) -> None:
    text_parts = [f"Daily Report {screen_date}", ""]
    stock_sections, stock_problems, options_sections, options_problems, other_problems = split_report(
        sections, problems
    )
    for heading, grouped, notes in (
        ("Stock plays", stock_sections, stock_problems),
        ("Options plays", options_sections, options_problems),
    ):
        if not grouped and not notes:
            continue
        text_parts.append(heading)
        text_parts.append("")
        for section in grouped:
            text_parts.append(section["title"])
            text_parts.append(section["body"])
            text_parts.append("")
        text_parts.extend(notes)
    text_parts.extend(other_problems)
    try:
        gainer_html, gainer_text_body, gainer_problems = index_gainers(
            datetime.date.fromisoformat(screen_date)
        )
    except Exception as error:
        email_log(f"Year-to-date gainers failed: {error}")
        gainer_html = ""
        gainer_text_body = ""
        gainer_problems = ["Year-to-date gainers did not finish"]
    if gainer_text_body:
        text_parts.append("")
        text_parts.append("Year to date")
        text_parts.append(gainer_text_body)
    text_parts.extend(gainer_problems)
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
        message["Subject"] = f"Daily Report {screen_date}"
        message.set_content("\n".join(text_parts).strip() + "\n")
        message.add_alternative(
            report_html(
                screen_date,
                sections,
                problems,
                summary,
                module,
                gainer_html,
                gainer_problems,
            ),
            subtype="html",
        )
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
