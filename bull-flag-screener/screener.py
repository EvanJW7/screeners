#!/usr/bin/env python3
"""Alert when an intraday bull flag matches the eight labeled winners.

The flag is a same-day push of at least 20% on a stock that opened at $10 or
less, followed by a 4 to 30 minute pause that gives back 8% to 16% and holds
above the low where that push started. An alert also requires the stock to be
up at least 40% from the prior close and the last price within 10% of the
session high. At the labeled breaks, every winner was up at least about 50%
on the day, and the last price was within 8% of the high. The list was
winners only, so a match is a lookalike.
"""

import argparse
import datetime
import fcntl
import json
import os
import smtplib
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from email.message import EmailMessage
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

MIN_PUSH = 0.20
MIN_POLE_MINUTES = 3
MAX_POLE_MINUTES = 90
MIN_PAUSE_MINUTES = 4
MAX_PAUSE_MINUTES = 30
MIN_PULLBACK = 0.08
MAX_PULLBACK = 0.16
MAX_PULLBACK_OF_POLE = 0.70
MAX_OPEN = 10.0
MIN_OPEN = 0.20
MIN_RVOL = 8.0
MIN_VOLUME = 100_000
MIN_UP_ON_DAY = 0.40
MAX_OFF_HIGH = 0.10
MAX_CANDIDATES = 40
BREAK_WINDOW_MINUTES = 6
HISTORY_SESSIONS = 20
SESSION_MINUTES = 390

KEY_FILE = Path.home() / ".config" / "two-day-screener" / "env"
CONFIG = Path.home() / ".config" / "bull-flag-screener"
EMAIL_LOG = CONFIG / "email.log"
AVERAGE_CACHE = CONFIG / "volume-averages.json"
LOCK_FILE = CONFIG / "run.lock"
EASTERN = ZoneInfo("America/New_York")
MARKET_OPEN = (9, 30)
MARKET_CLOSE = (16, 5)
GROUPED_URL = "https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{day}"
SNAPSHOT_URL = "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers"
MINUTE_URL = "https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/minute/{day}/{day}"
NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
}
COLUMNS = ["Time", "Symbol", "Open", "Last", "Day %", "Off high", "RVOL", "Pole", "Pause", "Pullback", "Flag high", "State"]


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def email_log(message: str) -> None:
    CONFIG.mkdir(parents=True, exist_ok=True)
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


def load_api_key() -> str:
    key = os.environ.get("POLYGON_API_KEY", "").strip() or load_env().get("POLYGON_API_KEY", "").strip()
    if key:
        return key
    raise RuntimeError(f"Polygon API key not found in {KEY_FILE}")


def polygon_get(url: str, api_key: str, params: dict | None = None, timeout: int = 60) -> dict:
    query = dict(params or {})
    query["apiKey"] = api_key
    last_status = 0
    for attempt in range(4):
        response = requests.get(url, params=query, timeout=timeout)
        last_status = response.status_code
        if response.status_code == 429:
            time.sleep(15 * (attempt + 1))
            continue
        try:
            body = response.json()
        except ValueError:
            body = {}
        message = str(body.get("message") or body.get("error") or "")
        if response.status_code != 200:
            raise RuntimeError(message or f"Polygon returned HTTP {response.status_code}")
        if body.get("status") not in {None, "OK", "DELAYED"}:
            raise RuntimeError(message or str(body.get("status")))
        return body
    raise RuntimeError(f"Polygon rate limit (HTTP {last_status})")


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
    symbols: set[str] = set()
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
                symbols.add(symbol)
    return symbols


def recent_weekdays(count: int, as_of: date) -> list[date]:
    days: list[date] = []
    cursor = as_of - timedelta(days=1)
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(days))


def average_volumes(api_key: str, as_of: date) -> dict[str, float]:
    CONFIG.mkdir(parents=True, exist_ok=True)
    if AVERAGE_CACHE.exists():
        try:
            cached = json.loads(AVERAGE_CACHE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cached = {}
        if cached.get("as_of") == as_of.isoformat() and isinstance(cached.get("averages"), dict):
            return {symbol: float(volume) for symbol, volume in cached["averages"].items()}

    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    found = 0
    for day in reversed(recent_weekdays(HISTORY_SESSIONS + 8, as_of)):
        rows = polygon_get(GROUPED_URL.format(day=day.isoformat()), api_key, {"adjusted": "true"}).get("results") or []
        if not rows:
            log(f"{day.isoformat()}: no bars")
            continue
        for row in rows:
            symbol = normalize_symbol(str(row.get("T") or ""))
            volume = row.get("v")
            if not symbol or volume is None:
                continue
            totals[symbol] = totals.get(symbol, 0.0) + float(volume)
            counts[symbol] = counts.get(symbol, 0) + 1
        found += 1
        log(f"{day.isoformat()}: {len(rows)} symbols")
        if found == HISTORY_SESSIONS:
            break
    if found < 10:
        raise RuntimeError("Not enough daily sessions to measure a 20-day volume average")
    averages = {
        symbol: totals[symbol] / counts[symbol]
        for symbol in totals
        if counts[symbol] >= 10 and totals[symbol] > 0
    }
    AVERAGE_CACHE.write_text(
        json.dumps({"as_of": as_of.isoformat(), "averages": averages}),
        encoding="utf-8",
    )
    log(f"Saved 20-day volume averages for {len(averages)} symbols")
    return averages


def clock(minutes: int) -> str:
    hour, minute = divmod(minutes, 60)
    suffix = "PM" if hour >= 12 else "AM"
    hour12 = hour % 12 or 12
    return f"{hour12}:{minute:02d} {suffix}"


def money(price: float) -> str:
    return f"${price:.3f}" if price < 1 else f"${price:.2f}"


def fetch_minutes(api_key: str, symbol: str, day: date) -> list[dict]:
    body = polygon_get(
        MINUTE_URL.format(symbol=symbol, day=day.isoformat()),
        api_key,
        {"adjusted": "true", "sort": "asc", "limit": 50000},
        timeout=30,
    )
    bars = []
    for row in body.get("results") or []:
        stamp = row.get("t")
        high, low, close = row.get("h"), row.get("l"), row.get("c")
        if None in (stamp, high, low, close):
            continue
        when = datetime.datetime.fromtimestamp(stamp / 1000, tz=EASTERN)
        minute = when.hour * 60 + when.minute
        if minute < 9 * 60 + 30 or minute >= 16 * 60:
            continue
        bars.append({"minute": minute, "h": float(high), "l": float(low), "c": float(close)})
    return bars


def find_flag(bars: list[dict]) -> dict | None:
    count = len(bars)
    if count < MIN_POLE_MINUTES + MIN_PAUSE_MINUTES:
        return None
    latest = bars[-1]["minute"]
    match: dict | None = None
    for peak in range(count - MIN_PAUSE_MINUTES):
        peak_high = bars[peak]["h"]
        if peak_high <= 0:
            continue
        pause_end = peak
        broken_at = None
        for cursor in range(peak + 1, count):
            if bars[cursor]["c"] > peak_high:
                broken_at = cursor
                break
            pause_end = cursor
        if pause_end == peak:
            continue
        pause_minutes = bars[pause_end]["minute"] - bars[peak]["minute"]
        if pause_minutes < MIN_PAUSE_MINUTES or pause_minutes > MAX_PAUSE_MINUTES:
            continue
        if any(bars[cursor]["h"] > peak_high for cursor in range(peak + 1, pause_end + 1)):
            continue
        if broken_at is None:
            state = "In the flag"
            age = latest - bars[pause_end]["minute"]
            if age > 2:
                continue
        else:
            age = latest - bars[broken_at]["minute"]
            if age > BREAK_WINDOW_MINUTES:
                continue
            state = f"Broke {clock(bars[broken_at]['minute'])}"
        start = None
        for origin in range(peak - 1, -1, -1):
            span = bars[peak]["minute"] - bars[origin]["minute"]
            if span > MAX_POLE_MINUTES:
                break
            if span < MIN_POLE_MINUTES or bars[origin]["l"] <= 0:
                continue
            if peak_high / bars[origin]["l"] - 1 >= MIN_PUSH:
                start = origin
                break
        if start is None:
            continue
        pole_low = min(bars[cursor]["l"] for cursor in range(start, peak + 1))
        if pole_low <= 0:
            continue
        push = peak_high / pole_low - 1
        if push < MIN_PUSH:
            continue
        flag_low = min(bars[cursor]["l"] for cursor in range(peak + 1, pause_end + 1))
        if flag_low <= pole_low:
            continue
        pullback = (peak_high - flag_low) / peak_high
        if pullback < MIN_PULLBACK or pullback > MAX_PULLBACK or pullback > push * MAX_PULLBACK_OF_POLE:
            continue
        match = {
            "peak_minute": bars[peak]["minute"],
            "pole_minutes": bars[peak]["minute"] - bars[start]["minute"],
            "push": push,
            "pause_minutes": pause_minutes,
            "pullback": pullback,
            "flag_high": peak_high,
            "state": state,
            "as_of": latest,
        }
    return match


def session_minutes(now: datetime.datetime) -> int:
    minute = now.hour * 60 + now.minute
    open_minute = 9 * 60 + 30
    close_minute = 16 * 60
    return min(max(minute - open_minute, 1), close_minute - open_minute)


def in_session(now: datetime.datetime) -> bool:
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= (now.hour, now.minute) < MARKET_CLOSE


def candidates(snapshot: list[dict], symbols: set[str], averages: dict[str, float], elapsed: int) -> list[dict]:
    rows = []
    for item in snapshot:
        symbol = normalize_symbol(str(item.get("ticker") or ""))
        if symbol not in symbols:
            continue
        day = item.get("day") or {}
        previous = item.get("prevDay") or {}
        trade = item.get("lastTrade") or {}
        open_price = day.get("o")
        high = day.get("h")
        volume = day.get("v")
        last = trade.get("p") or day.get("c")
        prior = previous.get("c")
        average = averages.get(symbol)
        if None in (open_price, high, volume, last, prior) or not average:
            continue
        open_price = float(open_price)
        high = float(high)
        last = float(last)
        prior = float(prior)
        volume = float(volume)
        if (
            not (MIN_OPEN <= open_price <= MAX_OPEN)
            or volume < MIN_VOLUME
            or last <= 0
            or prior <= 0
            or high <= 0
        ):
            continue
        if last / prior - 1 < MIN_UP_ON_DAY or last < high * (1 - MAX_OFF_HIGH):
            continue
        expected = average * (elapsed / SESSION_MINUTES)
        if expected <= 0:
            continue
        rvol = volume / expected
        if rvol < MIN_RVOL:
            continue
        rows.append({
            "symbol": symbol,
            "open": open_price,
            "last": last,
            "prior": prior,
            "rvol": rvol,
            "volume": volume,
        })
    rows.sort(key=lambda row: row["rvol"], reverse=True)
    return rows[:MAX_CANDIDATES]


def scan(api_key: str, now: datetime.datetime) -> list[dict]:
    symbols = load_symbols()
    averages = average_volumes(api_key, now.date())
    snapshot = polygon_get(SNAPSHOT_URL, api_key, timeout=90).get("tickers") or []
    log(f"Snapshot: {len(snapshot)} tickers")
    pool = candidates(snapshot, symbols, averages, session_minutes(now))
    log(f"Checking minute bars for {len(pool)} names")
    found = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(fetch_minutes, api_key, row["symbol"], now.date()): row for row in pool}
        for future in as_completed(futures):
            row = futures[future]
            try:
                bars = future.result()
            except RuntimeError as error:
                log(f"{row['symbol']}: {error}")
                continue
            flag = find_flag(bars)
            if flag is None or not bars or row["prior"] <= 0:
                continue
            last = bars[-1]["c"]
            hod = max(bar["h"] for bar in bars)
            if hod <= 0 or last / row["prior"] - 1 < MIN_UP_ON_DAY:
                continue
            off_high = (hod - last) / hod
            if off_high > MAX_OFF_HIGH:
                continue
            found.append({
                **row,
                **flag,
                "last": last,
                "day_change": last / row["prior"] - 1,
                "off_high": off_high,
            })
    found.sort(key=lambda row: row["rvol"], reverse=True)
    return found


def alert_key(row: dict) -> str:
    return f"{row['symbol']}|{row['peak_minute']}"


def alert_path(day: date) -> Path:
    return CONFIG / f"alerted-{day.isoformat()}"


def new_setups(day: date, rows: list[dict]) -> list[dict]:
    path = alert_path(day)
    seen = set(path.read_text(encoding="utf-8").split()) if path.exists() else set()
    return [row for row in rows if alert_key(row) not in seen]


def mark_alerted(day: date, rows: list[dict]) -> None:
    path = alert_path(day)
    CONFIG.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(alert_key(row) + "\n")


def table_rows(rows: list[dict]) -> list[list[str]]:
    return [
        [
            clock(row["as_of"]),
            row["symbol"],
            money(row["open"]),
            money(row["last"]),
            f"+{row['day_change'] * 100:.0f}%",
            f"{row['off_high'] * 100:.0f}%",
            f"{row['rvol']:.0f}x",
            f"+{row['push'] * 100:.0f}% / {row['pole_minutes']}m",
            f"{row['pause_minutes']}m",
            f"{row['pullback'] * 100:.0f}%",
            money(row["flag_high"]),
            row["state"],
        ]
        for row in rows
    ]


def print_table(rows: list[dict]) -> None:
    rendered = table_rows(rows)
    widths = [len(column) for column in COLUMNS]
    for row in rendered:
        widths = [max(width, len(cell)) for width, cell in zip(widths, row)]
    def format_row(row: list[str]) -> str:
        return "  ".join(cell.center(width) for cell, width in zip(row, widths))
    print(format_row(COLUMNS))
    print(format_row(["-" * width for width in widths]))
    for row in rendered:
        print(format_row(row))


def html_table(rows: list[dict]) -> str:
    header = "".join(
        "<th style=\"border:1px solid #d0d0d0;padding:8px 12px;background:#f3f3f3;"
        "text-align:center;white-space:nowrap;\">"
        f"{escape(column)}</th>"
        for column in COLUMNS
    )
    body = []
    for row in table_rows(rows):
        cells = "".join(
            "<td style=\"border:1px solid #d0d0d0;padding:8px 12px;text-align:center;white-space:nowrap;\">"
            f"{escape(value)}</td>"
            for value in row
        )
        body.append(f"<tr>{cells}</tr>")
    note = (
        "<p style=\"font-family:Arial,sans-serif;font-size:14px;\">"
        "A push of at least 20% on a stock that opened at $10 or less, then a "
        "4 to 30 minute pause that gave back 8% to 16% and held above the start "
        "of the push. The stock is up at least 40% from the prior close and the "
        "last price is within 10% of the session high. Volume so far is at least "
        "8 times the usual pace versus the prior 20 sessions."
        "</p>"
    )
    return (
        note
        + "<table style=\"border-collapse:collapse;font-family:Arial,sans-serif;font-size:14px;\">"
        + f"<thead><tr>{header}</tr></thead><tbody>{''.join(body)}</tbody></table>"
    )


def send_email(when: datetime.datetime, rows: list[dict], password: str) -> None:
    recipient = load_env().get("EMAIL_TO", "").strip()
    if not recipient:
        raise RuntimeError("EMAIL_TO is not set")
    names = ", ".join(row["symbol"] for row in rows)
    subject_names = names if len(rows) <= 3 else f"{len(rows)} names"
    message = EmailMessage()
    message["From"] = recipient
    message["To"] = recipient
    message["Subject"] = f"Bull flag {subject_names} {clock(when.hour * 60 + when.minute)}"
    text = [f"Bull flag lookalikes at {when.strftime('%-I:%M %p')} Eastern.", ""]
    for row in table_rows(rows):
        text.append("  ".join(row))
    text.append("")
    text.append(
        "Each name is up at least 40% on the day and within 10% of the session high, "
        "after a 20% push that paused 4 to 30 minutes and gave back 8% to 16%."
    )
    message.set_content("\n".join(text))
    message.add_alternative(html_table(rows), subtype="html")
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(recipient, password)
        smtp.send_message(message)


def acquire_lock():
    CONFIG.mkdir(parents=True, exist_ok=True)
    handle = LOCK_FILE.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def run(now: datetime.datetime, email: bool) -> int:
    lock = acquire_lock()
    if lock is None:
        log("Another scan is still running")
        return 0
    started = time.perf_counter()
    try:
        if email and not in_session(now):
            return 0
        if not email and not in_session(now):
            print("The regular session is closed. This scan runs from 9:30 AM to 4:00 PM Eastern on weekdays.")
            return 0
        rows = scan(load_api_key(), now)
        if email:
            fresh = new_setups(now.date(), rows)
            if not fresh:
                log(f"No new bull flags. {len(rows)} already seen. Completed in {time.perf_counter() - started:.1f}s")
                return 0
            env = load_env()
            password = env.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
            recipient = env.get("EMAIL_TO", "").strip()
            if not password or not recipient:
                email_log("Gmail app password or EMAIL_TO missing; not sending")
                print_table(fresh)
                return 0
            send_email(now, fresh, password)
            mark_alerted(now.date(), fresh)
            email_log(f"Sent {', '.join(row['symbol'] for row in fresh)}")
            log(f"Emailed {len(fresh)} new names in {time.perf_counter() - started:.1f}s")
            return 0
        if not rows:
            print(f"No bull flags at {now.strftime('%-I:%M %p')} Eastern.")
        else:
            print_table(rows)
        log(f"Completed in {time.perf_counter() - started:.1f} seconds")
        return 0
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan for intraday bull flags and email new matches.")
    parser.add_argument(
        "--email",
        action="store_true",
        help="During regular hours, email new matches. Used by the weekday job.",
    )
    args = parser.parse_args()
    now = datetime.datetime.now(EASTERN)
    try:
        return run(now, args.email)
    except Exception as error:
        if args.email:
            email_log(f"Scan failed: {error}")
        log(str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
