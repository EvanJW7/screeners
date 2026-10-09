#!/usr/bin/env python3
"""Screen for a strong one- or two-week run that has pulled back to the 9-day average."""

import argparse
import datetime
import os
import re
import smtplib
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from email.message import EmailMessage
from html import escape
from io import BytesIO, StringIO
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape
from zipfile import ZIP_DEFLATED, ZipFile
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

MIN_RUN = 0.50
RUN_LENGTHS = range(5, 11)
MIN_EXTENSION = 0.15
NEAR_SMA = 0.04
MIN_VOLUME = 500_000
MAX_PULLBACK = 10
SMA_DAYS = 9
PULLBACK_SMA_DAYS = 10
HISTORY_DAYS = 40

KEY_FILE = Path.home() / ".config" / "two-day-screener" / "env"
CONFIG = Path.home() / ".config" / "nine-sma-bounce"
EMAIL_STAMP = CONFIG / "last-email-date"
EMAIL_PASSWORD_WARNING = CONFIG / "last-password-warning"
EMAIL_LOG = CONFIG / "email.log"
EASTERN = ZoneInfo("America/New_York")
EMAIL_TO = "evan.wright16@gmail.com"
SEND_AFTER = (16, 16)
GIVE_UP = (18, 0)
GROUPED_URL = "https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{day}"
DAILY_URL = "https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/{start}/{end}"
FINVIZ_QUOTE_URL = "https://finviz.com/quote.ashx"
NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
}
COLUMNS = [
    "Date",
    "Symbol",
    "Price",
    "% Change",
    "Run %",
    "Days Since High",
    "To Run High",
    "Vs 9 SMA",
    "Volume",
    "Finviz Shares Float",
    "Finviz Short % of Float",
]


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def load_api_key() -> str:
    key = os.environ.get("POLYGON_API_KEY", "").strip()
    if key:
        return key
    if KEY_FILE.exists():
        for line in KEY_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("POLYGON_API_KEY="):
                key = line.split("=", 1)[1].strip()
                if key:
                    return key
    raise RuntimeError(f"Polygon API key not found in {KEY_FILE}")


def normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper().replace(".", "-").replace("/", "-")


def read_symbol_file(url: str, cache_name: str) -> str:
    cache = KEY_FILE.parent / cache_name
    text = ""
    try:
        response = requests.get(url, headers=HEADERS, timeout=30)
        response.raise_for_status()
        text = response.text
    except requests.RequestException:
        text = ""
    if "Security Name|" in text and not text.lstrip().startswith("<"):
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text, encoding="utf-8")
        return text
    if cache.exists():
        log(f"Using saved {cache_name}; the live Nasdaq file was unavailable")
        return cache.read_text(encoding="utf-8")
    raise RuntimeError(f"Nasdaq symbol file {cache_name} was unavailable")


def load_symbols() -> set[str]:
    sources = [
        (NASDAQ_LISTED, "nasdaqlisted.txt", "Symbol"),
        (OTHER_LISTED, "otherlisted.txt", "ACT Symbol"),
    ]
    symbols: list[str] = []
    for url, cache_name, symbol_column in sources:
        lines = [
            line
            for line in read_symbol_file(url, cache_name).splitlines()
            if line and not line.startswith("File Creation Time")
        ]
        header = lines[0].split("|")
        symbol_index = header.index(symbol_column)
        name_index = header.index("Security Name")
        test_index = header.index("Test Issue")
        for line in lines[1:]:
            parts = line.split("|")
            if len(parts) <= max(symbol_index, name_index, test_index):
                continue
            if parts[test_index] == "Y":
                continue
            if "warrant" in parts[name_index].lower():
                continue
            symbol = normalize_symbol(parts[symbol_index])
            if symbol and symbol != "N/A":
                symbols.append(symbol)
    return set(symbols)


def recent_weekdays(count: int, as_of: date) -> list[date]:
    days: list[date] = []
    cursor = as_of
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(days))


def fetch_grouped(api_key: str, day: date) -> list[dict]:
    for attempt in range(4):
        response = requests.get(
            GROUPED_URL.format(day=day.isoformat()),
            params={"adjusted": "true", "apiKey": api_key},
            timeout=60,
        )
        if response.status_code == 429:
            time.sleep(15 * (attempt + 1))
            continue
        try:
            body = response.json()
        except ValueError:
            body = {}
        message = str(body.get("message") or body.get("error") or "")
        if response.status_code == 403 and "before end of day" in message.lower():
            return []
        if response.status_code != 200:
            raise RuntimeError(message or f"Polygon returned HTTP {response.status_code} for {day.isoformat()}")
        if body.get("status") not in {None, "OK", "DELAYED"}:
            raise RuntimeError(message or str(body.get("status")))
        return body.get("results") or []
    raise RuntimeError(f"Polygon rate limit while fetching {day.isoformat()}")


def load_history(api_key: str, as_of: date) -> list[tuple[date, dict[str, tuple[float, float, float, float]]]]:
    sessions = []
    newest_first = list(reversed(recent_weekdays(HISTORY_DAYS + 15, as_of)))
    for day in newest_first:
        rows = fetch_grouped(api_key, day)
        if not rows:
            log(f"{day.isoformat()}: no bars")
            continue
        bars: dict[str, tuple[float, float, float, float]] = {}
        for row in rows:
            symbol = normalize_symbol(str(row.get("T") or ""))
            close = row.get("c")
            volume = row.get("v")
            high = row.get("h")
            low = row.get("l")
            if not symbol or None in (close, volume, high, low):
                continue
            bars[symbol] = (float(close), float(volume), float(high), float(low))
        sessions.append((day, bars))
        log(f"{day.isoformat()}: {len(bars)} symbols")
        if len(sessions) == HISTORY_DAYS:
            break
    sessions.reverse()
    if len(sessions) < 30:
        raise RuntimeError("Not enough daily sessions to measure a 9-day average and a prior run")
    return sessions


def sma(closes: list[float], end: int, length: int = SMA_DAYS) -> float:
    return sum(closes[end - length + 1 : end + 1]) / length


def qualifying_run(closes: list[float], peak: int) -> tuple[float, int] | None:
    best: tuple[float, int] | None = None
    for length in RUN_LENGTHS:
        start = peak - length
        if start < 0 or peak - SMA_DAYS + 1 < 0:
            continue
        base = closes[start]
        if base <= 0:
            continue
        change = closes[peak] / base - 1
        if change < MIN_RUN:
            continue
        if best is None or change > best[0]:
            best = (change, length)
    return best


def screen_symbol(bars: list[tuple[float, float, float, float]]) -> dict | None:
    closes = [bar[0] for bar in bars]
    volumes = [bar[1] for bar in bars]
    highs = [bar[2] for bar in bars]
    last = len(bars) - 1
    if last < 30 or volumes[last] <= MIN_VOLUME:
        return None
    average = sma(closes, last)
    if average <= 0:
        return None
    distance = closes[last] / average - 1
    if abs(distance) > NEAR_SMA:
        return None
    peak = max(range(last - MAX_PULLBACK, last), key=lambda day: closes[day])
    if closes[last] >= closes[peak]:
        return None
    if closes[peak] < sma(closes, peak) * (1 + MIN_EXTENSION):
        return None
    run = qualifying_run(closes, peak)
    if run is None:
        return None
    for day in range(peak + 1, last + 1):
        if closes[day] < sma(closes, day, PULLBACK_SMA_DAYS):
            return None
    change, length = run
    run_high = max(highs[peak - length : peak + 1])
    previous = closes[last - 1]
    return {
        "price": closes[last],
        "day_change": (closes[last] - previous) / previous * 100 if previous else 0,
        "run_change": change * 100,
        "run_days": length,
        "pullback_days": last - peak,
        "vs_high": (run_high / closes[last] - 1) * 100 if closes[last] else 0,
        "run_high": run_high,
        "distance": distance * 100,
        "volume": volumes[last],
    }


def is_six_month_high(api_key: str, symbol: str, screen_day: date, run_high: float) -> bool:
    start = screen_day - timedelta(days=183)
    response = requests.get(
        DAILY_URL.format(symbol=symbol, start=start.isoformat(), end=screen_day.isoformat()),
        params={"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": api_key},
        timeout=60,
    )
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200:
        message = str(body.get("message") or body.get("error") or "")
        raise RuntimeError(message or f"Polygon returned HTTP {response.status_code} for {symbol}")
    highs = [float(row["h"]) for row in body.get("results") or [] if row.get("h") is not None]
    if not highs:
        return False
    return run_high >= max(highs) * 0.999


def fmt_percent(value: float) -> str:
    return f"{value:+.2f}%"


def fmt_volume(value: float) -> str:
    return f"{round(value):,}"


def finviz_float(ticker: str) -> tuple[str, str]:
    response = requests.get(
        FINVIZ_QUOTE_URL,
        params={"t": ticker},
        headers=HEADERS,
        timeout=20,
    )
    response.raise_for_status()
    cells = BeautifulSoup(response.text, "html.parser").select("td.snapshot-td2")
    snapshot: dict[str, str] = {}
    for index in range(0, len(cells) - 1, 2):
        label = cells[index].get_text(" ", strip=True)
        value = cells[index + 1].get_text(" ", strip=True)
        if label:
            snapshot[label] = value if value and value != "-" else "—"
    return snapshot.get("Shs Float") or "—", snapshot.get("Short Float") or "—"


def add_finviz(row: dict) -> dict:
    try:
        shares, short = finviz_float(row["Symbol"])
    except Exception:
        shares, short = "—", "—"
    row["Finviz Shares Float"] = shares
    row["Finviz Short % of Float"] = short
    return row


def fmt_price(value: float) -> str:
    return f"{value:.2f}"


def print_table(rows: list[dict]) -> None:
    widths = {
        column: max(len(column), *(len(str(row[column])) for row in rows))
        for column in COLUMNS
    }
    print("  ".join(column.center(widths[column]) for column in COLUMNS))
    print("  ".join("-" * widths[column] for column in COLUMNS))
    for row in rows:
        print("  ".join(str(row[column]).center(widths[column]) for column in COLUMNS))


def run_screen(as_of: date) -> int:
    started = time.perf_counter()
    log("Loading US-listed symbols...")
    universe = load_symbols()
    log(f"Universe has {len(universe)} symbols")
    history = load_history(load_api_key(), as_of)
    screen_day = history[-1][0]
    by_symbol: dict[str, list[tuple[float, float, float, float]]] = {}
    for _, bars in history:
        for symbol, bar in bars.items():
            if symbol in universe:
                by_symbol.setdefault(symbol, []).append(bar)
    session_count = len(history)
    matches = []
    for symbol, bars in by_symbol.items():
        if len(bars) != session_count:
            continue
        match = screen_symbol(bars)
        if match is None:
            continue
        matches.append(
            {
                "Date": screen_day.isoformat(),
                "Symbol": symbol,
                "Price": fmt_price(match["price"]),
                "% Change": fmt_percent(match["day_change"]),
                "Run %": f"{fmt_percent(match['run_change'])} / {match['run_days']}d",
                "Days Since High": str(match["pullback_days"]),
                "To Run High": fmt_percent(match["vs_high"]),
                "Vs 9 SMA": fmt_percent(match["distance"]),
                "Volume": fmt_volume(match["volume"]),
                "run_high": match["run_high"],
                "sort": match["run_change"],
            }
        )
    api_key = load_api_key()
    confirmed = []
    for row in matches:
        if is_six_month_high(api_key, row["Symbol"], screen_day, row["run_high"]):
            confirmed.append(row)
    if len(confirmed) != len(matches):
        log(f"Dropped {len(matches) - len(confirmed)} names whose run high was under a high from the past 6 months")
    matches = confirmed
    matches.sort(key=lambda row: row["sort"], reverse=True)
    if matches:
        log(f"Fetching Finviz details for {len(matches)} matches...")
        with ThreadPoolExecutor(max_workers=6) as pool:
            matches = list(pool.map(add_finviz, matches))
    if not matches:
        print(f"No names are at the 9-day average on {screen_day.isoformat()}.")
    else:
        print_table(matches)
    now = datetime.datetime.now(EASTERN)
    if screen_day == now.date() and (now.hour, now.minute) < (16, 0):
        log("The session is still open, so these are the latest prints.")
    log(f"Completed in {time.perf_counter() - started:.2f} seconds")
    return 0


def email_log(message: str) -> None:
    EMAIL_LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(EASTERN).isoformat(timespec="seconds")
    with EMAIL_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {message}\n")


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not KEY_FILE.exists():
        return values
    for line in KEY_FILE.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def already_handled(today: str) -> bool:
    return EMAIL_STAMP.exists() and EMAIL_STAMP.read_text(encoding="utf-8").strip() == today


def mark_handled(today: str) -> None:
    EMAIL_STAMP.parent.mkdir(parents=True, exist_ok=True)
    EMAIL_STAMP.write_text(today + "\n", encoding="utf-8")


def split_row(line: str, widths: list[int]) -> list[str]:
    cells: list[str] = []
    pos = 0
    for index, width in enumerate(widths):
        cells.append(line[pos : pos + width].strip())
        pos += width
        if index < len(widths) - 1:
            pos += 2
    return cells


def parse_table(body: str) -> tuple[list[str], list[list[str]], str]:
    elapsed = ""
    lines: list[str] = []
    for line in body.splitlines():
        if line.startswith("Completed in"):
            elapsed = line
            continue
        lines.append(line)
    while lines and not lines[0].strip():
        lines.pop(0)
    if len(lines) < 2 or "  " not in lines[1]:
        return [], [], elapsed
    widths = [len(part) for part in lines[1].split("  ")]
    columns = split_row(lines[0], widths)
    rows: list[list[str]] = []
    for line in lines[2:]:
        if not line.strip():
            rows.append([""] * len(columns))
            continue
        rows.append(split_row(line, widths))
    while rows and not any(rows[-1]):
        rows.pop()
    return columns, rows, elapsed


def column_letter(index: int) -> str:
    letters = ""
    number = index + 1
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def spreadsheet_bytes(columns: list[str], rows: list[list[str]]) -> bytes:
    """Excel file with centered cells and a locked header row."""
    xml_rows: list[str] = []
    sheet_rows = [columns, *rows]
    for row_number, row in enumerate(sheet_rows, start=1):
        style = "1" if row_number == 1 else "0"
        cells: list[str] = []
        for index, value in enumerate(row):
            ref = f"{column_letter(index)}{row_number}"
            text = f"<is><t>{xml_escape(value)}</t></is>" if value else "<is><t></t></is>"
            cells.append(f'<c r="{ref}" t="inlineStr" s="{style}">{text}</c>')
        xml_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    widths = []
    for index, column in enumerate(columns):
        longest = len(column)
        for row in rows:
            if index < len(row):
                longest = max(longest, len(row[index]))
        width = max(longest, len(column)) * 1.3 + 4
        widths.append(
            f'<col min="{index + 1}" max="{index + 1}" width="{width:.2f}" customWidth="1"/>'
        )
    last_column = column_letter(max(len(columns) - 1, 0))
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<cols>{''.join(widths)}</cols>"
        f"<sheetData>{''.join(xml_rows)}</sheetData>"
        '<sheetProtection sheet="1" objects="1" scenarios="1" selectLockedCells="1" selectUnlockedCells="1"/>'
        f'<autoFilter ref="A1:{last_column}{max(len(sheet_rows), 1)}"/>'
        "</worksheet>"
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="2">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1" applyProtection="1">'
        '<alignment horizontal="center" vertical="center"/>'
        '<protection locked="0"/></xf>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1" applyProtection="1">'
        '<alignment horizontal="center" vertical="center"/>'
        '<protection locked="1"/></xf>'
        "</cellXfs></styleSheet>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        "</Types>"
    )
    package_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Screen" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        "</Relationships>"
    )
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as workbook_zip:
        workbook_zip.writestr("[Content_Types].xml", content_types)
        workbook_zip.writestr("_rels/.rels", package_rels)
        workbook_zip.writestr("xl/workbook.xml", workbook)
        workbook_zip.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        workbook_zip.writestr("xl/styles.xml", styles)
        workbook_zip.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


def html_table(columns: list[str], rows: list[list[str]], elapsed: str) -> str:
    header = "".join(
        "<th style=\"border:1px solid #d0d0d0;padding:8px 12px;background:#f3f3f3;"
        "text-align:center;white-space:nowrap;\">"
        f"{escape(column)}</th>"
        for column in columns
    )
    body_rows: list[str] = []
    for row in rows:
        if not any(row):
            body_rows.append(
                f'<tr><td colspan="{len(columns)}" style="height:12px;border:none;"></td></tr>'
            )
            continue
        cells = "".join(
            "<td style=\"border:1px solid #d0d0d0;padding:8px 12px;text-align:center;white-space:nowrap;\">"
            f"{escape(value) or '&nbsp;'}</td>"
            for value in row
        )
        body_rows.append(f"<tr>{cells}</tr>")
    footer = ""
    if elapsed:
        footer += f"<p style=\"font-family:Arial,sans-serif;\">{escape(elapsed)}</p>"
    return (
        "<table style=\"border-collapse:collapse;font-family:Arial,sans-serif;font-size:14px;\">"
        f"<thead><tr>{header}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>{footer}"
    )


def send_email(screen_date: str, body: str, password: str) -> None:
    columns, rows, elapsed = parse_table(body)
    message = EmailMessage()
    message["From"] = EMAIL_TO
    message["To"] = EMAIL_TO
    message["Subject"] = f"9 SMA bounce screen {screen_date}"
    message.set_content(body)
    if columns:
        message.add_alternative(html_table(columns, rows, elapsed), subtype="html")
        message.add_attachment(
            spreadsheet_bytes(columns, rows),
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=f"nine-sma-bounce-{screen_date}.xlsx",
        )
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(EMAIL_TO, password)
        smtp.send_message(message)


def send_scheduled_email() -> int:
    now = datetime.datetime.now(EASTERN)
    today = now.date().isoformat()
    if now.weekday() >= 5 or (now.hour, now.minute) < SEND_AFTER or already_handled(today):
        return 0

    password = load_env().get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    if not password:
        warned = (
            EMAIL_PASSWORD_WARNING.exists()
            and EMAIL_PASSWORD_WARNING.read_text(encoding="utf-8").strip() == today
        )
        if not warned:
            email_log("Gmail app password missing; not sending")
            EMAIL_PASSWORD_WARNING.parent.mkdir(parents=True, exist_ok=True)
            EMAIL_PASSWORD_WARNING.write_text(today + "\n", encoding="utf-8")
        return 0

    stdout = StringIO()
    stderr = StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = run_screen(now.date())
    except Exception as error:
        email_log(f"Screener failed: {error}")
        if (now.hour, now.minute) >= GIVE_UP:
            mark_handled(today)
        return 1
    if code != 0:
        email_log(f"Screener failed: {stderr.getvalue().strip()[-500:]}")
        if (now.hour, now.minute) >= GIVE_UP:
            mark_handled(today)
        return 1

    dates = re.findall(r"^(\d{4}-\d{2}-\d{2}): \d+ symbols", stderr.getvalue(), re.MULTILINE)
    screen_date = dates[0] if dates else ""
    if screen_date != today:
        if (now.hour, now.minute) >= GIVE_UP:
            mark_handled(today)
            email_log(f"No trading session for {today}; not sending")
        else:
            email_log(f"Session for {today} is not ready yet")
        return 0

    body = stdout.getvalue().strip("\n")
    for line in stderr.getvalue().splitlines():
        if line.startswith("Completed in"):
            body = f"{body}\n\n{line}" if body else line
            break
    if not body.strip():
        body = "No stocks matched."
    send_email(screen_date, body, password)
    mark_handled(today)
    email_log(f"Sent {screen_date} screen to {EMAIL_TO}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Screen for a pullback to the 9-day average after a strong run.")
    parser.add_argument("--date", help="Screen date, YYYY-MM-DD. Defaults to today.")
    parser.add_argument(
        "--email",
        action="store_true",
        help="Email today's screen after 4:16 PM Eastern. Used by the weekday job.",
    )
    args = parser.parse_args()
    if args.email:
        try:
            return send_scheduled_email()
        except Exception as error:
            email_log(f"Email run failed: {error}")
            raise
    as_of = date.fromisoformat(args.date) if args.date else datetime.datetime.now(EASTERN).date()
    return run_screen(as_of)


if __name__ == "__main__":
    raise SystemExit(main())
