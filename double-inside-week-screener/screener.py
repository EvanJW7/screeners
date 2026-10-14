#!/usr/bin/env python3
"""Screen for a 50% run over one or two weeks, followed by two inside weeks.

The run is a close-to-close gain of at least 50% over the one week, or the
two weeks, immediately before the inside weeks. The latest week is inside
the prior week, and that week is inside the last week of the run. Each
inside week can break the prior week's high or low by at most 1%. The
latest week must be green, with its close above that week's open. Average
daily volume must be at least 500,000 and average daily volatility at least
4%. Volatility is each day's high-to-low range as a percent of that day's close.
"""

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

MIN_RUN = 50.0
MIN_AVERAGE_VOLUME = 500_000
MIN_AVERAGE_VOLATILITY = 4.0
MIN_VOLUME_SESSIONS = 10
INSIDE_BUFFER = 0.01
WEEKS_NEEDED = 5
SESSION_GOAL = 20
MAX_LOOKBACK = 40
KEY_FILE = Path.home() / ".config" / "two-day-screener" / "env"
EASTERN = ZoneInfo("America/New_York")
GROUPED_URL = "https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{day}"
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
    "Week Change",
    "Run",
    "Run Weeks",
    "Avg Volume",
    "Avg Volatility",
]
Bar = tuple[float, float, float, float, float]


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def load_api_key() -> str:
    key = os.environ.get("POLYGON_API_KEY", "").strip()
    if key:
        return key
    if KEY_FILE.exists():
        for line in KEY_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("POLYGON_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
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


def parse_bars(rows: list[dict]) -> dict[str, Bar]:
    bars: dict[str, Bar] = {}
    for row in rows:
        symbol = normalize_symbol(str(row.get("T") or ""))
        close, volume, high, low, open_price = (
            row.get("c"),
            row.get("v"),
            row.get("h"),
            row.get("l"),
            row.get("o"),
        )
        if not symbol or None in (close, volume, high, low, open_price):
            continue
        bars[symbol] = (float(close), float(volume), float(high), float(low), float(open_price))
    return bars


def load_span(api_key: str, start: date, end: date) -> list[tuple[date, dict[str, Bar]]]:
    """Sessions from start through end, newest first. Empty market days are skipped."""
    sessions: list[tuple[date, dict[str, Bar]]] = []
    cursor = end
    while cursor >= start:
        if cursor.weekday() < 5:
            rows = fetch_grouped(api_key, cursor)
            if rows:
                sessions.append((cursor, parse_bars(rows)))
                log(f"{cursor.isoformat()}: {len(sessions[-1][1])} symbols")
            else:
                log(f"{cursor.isoformat()}: no bars")
        cursor -= timedelta(days=1)
    return sessions


def load_sessions(api_key: str, as_of: date) -> list[tuple[date, dict[str, Bar]]]:
    """Recent sessions, newest first, covering four weeks and a volume sample."""
    sessions: list[tuple[date, dict[str, Bar]]] = []
    weeks: set[date] = set()
    cursor = as_of
    looked = 0
    while looked < MAX_LOOKBACK and (len(weeks) < WEEKS_NEEDED or len(sessions) < SESSION_GOAL):
        if cursor.weekday() < 5:
            looked += 1
            rows = fetch_grouped(api_key, cursor)
            if rows:
                sessions.append((cursor, parse_bars(rows)))
                weeks.add(cursor - timedelta(days=cursor.weekday()))
                log(f"{cursor.isoformat()}: {len(sessions[-1][1])} symbols")
            else:
                log(f"{cursor.isoformat()}: no bars")
        cursor -= timedelta(days=1)
    if len(weeks) < WEEKS_NEEDED:
        raise RuntimeError("Need five weeks to measure a run and two inside weeks")
    return sessions


def sessions_through(
    sessions: list[tuple[date, dict[str, Bar]]],
    screen_end: date,
) -> list[tuple[date, dict[str, Bar]]]:
    """Sessions ending on screen_end, enough for two inside weeks and the averages."""
    window: list[tuple[date, dict[str, Bar]]] = []
    weeks: set[date] = set()
    for day, bars in sessions:
        if day > screen_end:
            continue
        window.append((day, bars))
        weeks.add(day - timedelta(days=day.weekday()))
        if len(weeks) >= WEEKS_NEEDED and len(window) >= SESSION_GOAL:
            break
    return window


def recent_week_ends(
    sessions: list[tuple[date, dict[str, Bar]]],
    as_of: date,
    count: int,
) -> list[date]:
    """Last session of each recent week, newest first."""
    last_session: dict[date, date] = {}
    for day, _ in sessions:
        if day > as_of:
            continue
        monday = day - timedelta(days=day.weekday())
        if monday not in last_session or day > last_session[monday]:
            last_session[monday] = day
    mondays = sorted(last_session, reverse=True)[:count]
    return [last_session[monday] for monday in mondays]


def build_weeks(
    sessions: list[tuple[date, dict[str, Bar]]],
) -> list[tuple[date, date, dict[str, Bar]]]:
    """Weekly close, summed volume, high, low, and the first session's open. Newest week first."""
    buckets: dict[date, dict[str, list[float]]] = {}
    last_day: dict[date, date] = {}
    for day, bars in reversed(sessions):
        monday = day - timedelta(days=day.weekday())
        bucket = buckets.setdefault(monday, {})
        last_day[monday] = max(last_day.get(monday, day), day)
        for symbol, (close, volume, high, low, open_price) in bars.items():
            current = bucket.get(symbol)
            if current is None:
                bucket[symbol] = [close, volume, high, low, open_price]
                continue
            current[0] = close
            current[1] += volume
            current[2] = max(current[2], high)
            current[3] = min(current[3], low)
    weeks = []
    for monday in sorted(buckets, reverse=True):
        bars = {
            symbol: (values[0], values[1], values[2], values[3], values[4])
            for symbol, values in buckets[monday].items()
        }
        weeks.append((monday, last_day[monday], bars))
    return weeks


def average_volumes(sessions: list[tuple[date, dict[str, Bar]]]) -> dict[str, float]:
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for _, bars in sessions:
        for symbol, (_, volume, _, _, _) in bars.items():
            totals[symbol] = totals.get(symbol, 0.0) + volume
            counts[symbol] = counts.get(symbol, 0) + 1
    return {
        symbol: totals[symbol] / counts[symbol]
        for symbol in totals
        if counts[symbol] >= MIN_VOLUME_SESSIONS
    }


def average_volatilities(sessions: list[tuple[date, dict[str, Bar]]]) -> dict[str, float]:
    """Mean of each day's high-to-low range as a percent of that day's close."""
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for _, bars in sessions:
        for symbol, (close, _, high, low, _) in bars.items():
            if close <= 0 or high < low:
                continue
            totals[symbol] = totals.get(symbol, 0.0) + (high - low) / close * 100
            counts[symbol] = counts.get(symbol, 0) + 1
    return {
        symbol: totals[symbol] / counts[symbol]
        for symbol in totals
        if counts[symbol] >= MIN_VOLUME_SESSIONS
    }


def run_gain(close: float, base: float) -> float | None:
    """Close-to-close percent gain when it is at least the minimum run."""
    if close <= 0 or base <= 0:
        return None
    gain = (close - base) / base * 100
    if gain < MIN_RUN:
        return None
    return gain


def closes_hold(
    sessions: list[tuple[date, dict[str, Bar]]],
    symbol: str,
    start: date,
    end: date,
) -> bool:
    """False when one adjusted close is several times the prior close, as in a ticker change."""
    previous = None
    for day, bars in reversed(sessions):
        if day < start or day > end:
            continue
        bar = bars.get(symbol)
        if bar is None or bar[0] <= 0:
            continue
        close = bar[0]
        if previous is not None and close / previous > 4:
            return False
        previous = close
    return previous is not None


def is_inside(high: float, low: float, prior_high: float, prior_low: float) -> bool:
    """True when this week stays inside the prior week, allowing a 1% break."""
    if prior_high <= 0 or prior_low <= 0:
        return False
    return high <= prior_high * (1 + INSIDE_BUFFER) and low >= prior_low * (1 - INSIDE_BUFFER)


def find_matches(
    universe: set[str],
    weeks: list[tuple[date, date, dict[str, Bar]]],
    sessions: list[tuple[date, dict[str, Bar]]],
    averages: dict[str, float],
    volatilities: dict[str, float],
    as_of: date,
) -> list[dict]:
    if len(weeks) < WEEKS_NEEDED:
        log("Not enough weeks for a one- or two-week run followed by two inside weeks")
        return []
    _, screen_end, screen = weeks[0]
    prior_start, prior_end, prior = weeks[1]
    run_start, run_end, run_week = weeks[2]
    _, earlier_end, earlier = weeks[3]
    _, base_end, base = weeks[4]
    log(
        f"Looking for a {MIN_RUN:.0f}% run over {run_start.isoformat()}–{run_end.isoformat()}, "
        f"or over the two weeks ending {run_end.isoformat()}, "
        f"followed by inside weeks {prior_start.isoformat()}–{prior_end.isoformat()} "
        f"and {screen_end.isoformat()}. "
        f"Average daily volume at least {MIN_AVERAGE_VOLUME:,.0f}, "
        f"average daily volatility at least {MIN_AVERAGE_VOLATILITY:.0f}%, "
        "and a green current week"
    )
    matches = []
    for symbol, (price, _, high, low, week_open) in screen.items():
        if symbol not in universe or price <= 0 or symbol not in prior or symbol not in run_week:
            continue
        average = averages.get(symbol)
        volatility = volatilities.get(symbol)
        if average is None or average < MIN_AVERAGE_VOLUME:
            continue
        if volatility is None or volatility < MIN_AVERAGE_VOLATILITY:
            continue
        if week_open <= 0 or price <= week_open:
            continue
        prior_close, _, prior_high, prior_low, _ = prior[symbol]
        run_close, _, run_high, run_low, _ = run_week[symbol]
        if prior_close <= 0 or run_close <= 0:
            continue
        if not is_inside(prior_high, prior_low, run_high, run_low):
            continue
        if not is_inside(high, low, prior_high, prior_low):
            continue
        one_week = run_gain(run_close, earlier[symbol][0]) if symbol in earlier else None
        two_week = run_gain(run_close, base[symbol][0]) if symbol in base else None
        if one_week is not None:
            gain, run_weeks, series_start = one_week, 1, earlier_end
        elif two_week is not None:
            gain, run_weeks, series_start = two_week, 2, base_end
        else:
            continue
        if not closes_hold(sessions, symbol, series_start, screen_end):
            log(f"Skipped {symbol}: the run's price series is not continuous")
            continue
        matches.append(
            {
                "Date": as_of.isoformat(),
                "Symbol": symbol,
                "Price": f"{price:,.2f}",
                "Week Change": f"{(price - prior_close) / prior_close * 100:+.2f}%",
                "Run": f"{gain:+.2f}%",
                "Run Weeks": str(run_weeks),
                "Avg Volume": f"{int(average):,}",
                "Avg Volatility": f"{volatility:.2f}%",
                "sort_run": gain,
            }
        )
    matches.sort(key=lambda row: row["sort_run"], reverse=True)
    return matches


def print_table(rows: list[dict]) -> None:
    widths = {column: max(len(column), *(len(str(row[column])) for row in rows)) for column in COLUMNS}
    print("  ".join(column.center(widths[column]) for column in COLUMNS))
    print("  ".join("-" * widths[column] for column in COLUMNS))
    for row in rows:
        print("  ".join(str(row[column]).center(widths[column]) for column in COLUMNS))


def none_message(as_of: date) -> str:
    return (
        f"No stocks have a {MIN_RUN:.0f}% run over one or two weeks, two inside weeks, "
        f"average daily volume of at least {MIN_AVERAGE_VOLUME:,.0f}, "
        f"average daily volatility of at least {MIN_AVERAGE_VOLATILITY:.0f}%, "
        f"and a green current week on {as_of.isoformat()}."
    )


def show_matches(matches: list[dict], as_of: date) -> None:
    if not matches:
        print(none_message(as_of))
        return
    log(f"{len(matches)} matches")
    print_table(matches)


def run_weeks(as_of: date, weeks_back: int) -> int:
    """Screen each of the last weeks_back weeks through that week's last session."""
    started = time.perf_counter()
    log("Loading US-listed stocks...")
    universe = load_symbols()
    log(f"Universe has {len(universe)} symbols")
    api_key = load_api_key()
    current_monday = as_of - timedelta(days=as_of.weekday())
    oldest_monday = current_monday - timedelta(weeks=weeks_back - 1)
    history_start = oldest_monday - timedelta(days=60)
    log(f"Loading sessions from {history_start.isoformat()} through {as_of.isoformat()}...")
    sessions = load_span(api_key, history_start, as_of)
    ends = recent_week_ends(sessions, as_of, weeks_back)
    log(f"Screening {len(ends)} weeks")
    for screen_end in ends:
        print()
        print(f"Week ending {screen_end.isoformat()}")
        window = sessions_through(sessions, screen_end)
        weeks = build_weeks(window)
        if len(weeks) < WEEKS_NEEDED:
            print(none_message(screen_end))
            continue
        screen_monday, last_session, _ = weeks[0]
        if screen_end == ends[0] and (as_of.weekday() < 4 or last_session < as_of):
            log(
                f"The week of {screen_monday.isoformat()} runs through {last_session.isoformat()} "
                "and can still break the prior week's range."
            )
        matches = find_matches(
            universe,
            weeks,
            window,
            average_volumes(window),
            average_volatilities(window),
            screen_end,
        )
        show_matches(matches, screen_end)
    log(f"Completed in {time.perf_counter() - started:.2f} seconds")
    return 0


def run_screen(as_of: date) -> int:
    started = time.perf_counter()
    log("Loading US-listed stocks...")
    universe = load_symbols()
    log(f"Universe has {len(universe)} symbols")
    api_key = load_api_key()
    sessions = load_sessions(api_key, as_of)
    weeks = build_weeks(sessions)
    screen_monday, screen_end, _ = weeks[0]
    if as_of.weekday() < 4 or screen_end < as_of:
        log(
            f"The week of {screen_monday.isoformat()} runs through {screen_end.isoformat()} "
            "and can still break the prior week's range."
        )
    matches = find_matches(
        universe,
        weeks,
        sessions,
        average_volumes(sessions),
        average_volatilities(sessions),
        as_of,
    )
    show_matches(matches, as_of)
    log(f"Completed in {time.perf_counter() - started:.2f} seconds")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Screen for a 50% run over one or two weeks, followed by two inside weeks, "
            "with average daily volume of at least 500,000, average daily volatility of at least 4%, "
            "and a green current week."
        )
    )
    parser.add_argument("--date", help="Screen date, YYYY-MM-DD. Defaults to today.")
    parser.add_argument(
        "--weeks",
        type=int,
        default=1,
        help="How many recent weeks to screen, through each week's last session. Defaults to 1.",
    )
    args = parser.parse_args()
    if args.weeks < 1:
        parser.error("--weeks must be at least 1")
    as_of = date.fromisoformat(args.date) if args.date else datetime.now(EASTERN).date()
    if args.weeks == 1:
        return run_screen(as_of)
    return run_weeks(as_of, args.weeks)


if __name__ == "__main__":
    raise SystemExit(main())
