#!/usr/bin/env python3
"""Screen for stocks above $1B with two inside days in a row.

The most recent day also needs more than 50,000 option contracts traded, a close
above the 10-day average, a 6-month average daily range above 5%, and a
1-year return above zero. Option volume is that session's cleared total for the regular contracts. Each
inside day can break the prior day's high or low by at most 1%.
"""

import argparse
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

MIN_MARKET_CAP = 1_000_000_000
MIN_OPTIONS_VOLUME = 50_000
MIN_AVERAGE_RANGE = 5.0
RANGE_MONTHS = 6
MIN_RANGE_SESSIONS = 100
SMA_DAYS = 10
INSIDE_BUFFER = 0.01
DETAIL_WORKERS = 6
STOCK_TYPES = {"CS", "ADRC", "ADRP", "ADRR", "OS"}
KEY_FILE = Path.home() / ".config" / "two-day-screener" / "env"
EASTERN = ZoneInfo("America/New_York")
GROUPED_URL = "https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{day}"
DAILY_URL = "https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/{start}/{end}"
TICKER_URL = "https://api.polygon.io/v3/reference/tickers/{symbol}"
FINVIZ_QUOTE_URL = "https://finviz.com/quote.ashx"
OCC_URL = "https://marketdata.theocc.com/volume-query"

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
    "1 Year Return",
    "Options Volume",
    "Volume",
    "Avg Volume",
    "6-Month Range",
    "Country",
    "Finviz Market Cap",
    "Finviz Shares Float",
    "Finviz Short % of Float",
]
FORWARD_COLUMNS = ["Next Day %", "Next 3 Days %", "Next 5 Days %"]
FORWARD_SESSIONS = (1, 3, 5)


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


def polygon_symbol(symbol: str) -> str:
    return symbol.replace("-", ".")


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
        etf_index = header.index("ETF")
        for line in lines[1:]:
            parts = line.split("|")
            if len(parts) <= max(symbol_index, name_index, test_index, etf_index):
                continue
            if parts[test_index] == "Y" or parts[etf_index] == "Y":
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


def parse_bars(rows: list[dict]) -> dict[str, tuple[float, float, float, float]]:
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
    return bars


def load_sessions(api_key: str, as_of: date) -> list[tuple[date, dict[str, tuple[float, float, float, float]]]]:
    sessions = []
    cursor = as_of
    looked = 0
    while len(sessions) < SMA_DAYS and looked < 16:
        if cursor.weekday() < 5:
            looked += 1
            rows = fetch_grouped(api_key, cursor)
            if rows:
                sessions.append((cursor, parse_bars(rows)))
                log(f"{cursor.isoformat()}: {len(sessions[-1][1])} symbols")
            else:
                log(f"{cursor.isoformat()}: no bars")
        cursor -= timedelta(days=1)
    if len(sessions) < SMA_DAYS:
        raise RuntimeError("Need ten sessions to measure the 10-day average")
    return sessions


def load_forward_sessions(
    api_key: str, after: date, count: int = 5
) -> list[tuple[date, dict[str, tuple[float, float, float, float]]]]:
    """The next trading sessions after a past screen date. Empty for the latest session."""
    today = datetime.now(EASTERN).date()
    if after >= today:
        return []
    found = []
    cursor = after + timedelta(days=1)
    while cursor <= today and len(found) < count and (cursor - after).days <= 21:
        if cursor.weekday() < 5:
            rows = fetch_grouped(api_key, cursor)
            if rows:
                found.append((cursor, parse_bars(rows)))
                log(f"Forward session {cursor.isoformat()}: {len(found[-1][1])} symbols")
            else:
                log(f"{cursor.isoformat()}: no bars")
        cursor += timedelta(days=1)
    return found


def forward_changes(
    price: float,
    symbol: str,
    sessions: list[tuple[date, dict[str, tuple[float, float, float, float]]]],
) -> list[float | None]:
    """Close-to-close move from the screen day to the 1st, 3rd, and 5th later sessions."""
    closes: list[float | None] = []
    for _, bars in sessions:
        bar = bars.get(symbol)
        closes.append(bar[0] if bar else None)
    changes: list[float | None] = []
    for offset in FORWARD_SESSIONS:
        close = closes[offset - 1] if offset <= len(closes) else None
        if price > 0 and close:
            changes.append((close - price) / price * 100)
        else:
            changes.append(None)
    return changes


def is_inside(high: float, low: float, prior_high: float, prior_low: float) -> bool:
    """True when this candle stays inside the prior candle, allowing a 1% break."""
    if prior_high <= 0 or prior_low <= 0:
        return False
    return high <= prior_high * (1 + INSIDE_BUFFER) and low >= prior_low * (1 - INSIDE_BUFFER)


def find_matches(
    universe: set[str],
    sessions: list[tuple[date, dict[str, tuple[float, float, float, float]]]],
) -> list[dict]:
    screen_day, screen = sessions[0]
    prior_day, prior = sessions[1]
    mother_day, mother = sessions[2]
    log(
        f"Looking for two inside days, {prior_day.isoformat()} inside {mother_day.isoformat()} "
        f"and {screen_day.isoformat()} inside {prior_day.isoformat()}, "
        f"with the close above the {SMA_DAYS}-day average, more than "
        f"{MIN_OPTIONS_VOLUME:,.0f} option contracts on {screen_day.isoformat()}, "
        f"and a {RANGE_MONTHS}-month average daily range above {MIN_AVERAGE_RANGE:.0f}%, "
        f"and a 1-year return above zero"
    )
    matches = []
    for symbol, (price, volume, high, low) in screen.items():
        if symbol not in universe or price <= 0:
            continue
        if symbol not in prior or symbol not in mother:
            continue
        prior_close, _, prior_high, prior_low = prior[symbol]
        _, _, mother_high, mother_low = mother[symbol]
        if prior_close <= 0:
            continue
        if not is_inside(prior_high, prior_low, mother_high, mother_low):
            continue
        if not is_inside(high, low, prior_high, prior_low):
            continue
        closes = []
        for _, bars in sessions[:SMA_DAYS]:
            bar = bars.get(symbol)
            if bar is None or bar[0] <= 0:
                closes = []
                break
            closes.append(bar[0])
        if len(closes) < SMA_DAYS or price <= sum(closes) / SMA_DAYS:
            continue
        matches.append(
            {
                "date": screen_day.isoformat(),
                "symbol": symbol,
                "price": price,
                "day_change": (price - prior_close) / prior_close * 100,
                "volume": volume,
            }
        )
    return matches


def market_cap(api_key: str, symbol: str, price: float) -> float | None:
    url = TICKER_URL.format(symbol=quote(polygon_symbol(symbol), safe=""))
    for attempt in range(4):
        response = requests.get(url, params={"apiKey": api_key}, timeout=30)
        if response.status_code == 429:
            time.sleep(15 * (attempt + 1))
            continue
        if response.status_code == 404:
            return None
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200:
            message = str(body.get("message") or body.get("error") or "")
            raise RuntimeError(message or f"Polygon returned HTTP {response.status_code} for {symbol}")
        result = body.get("results") or {}
        if result.get("type") not in STOCK_TYPES:
            return None
        cap = result.get("market_cap")
        if cap:
            return float(cap)
        shares = result.get("weighted_shares_outstanding") or result.get("share_class_shares_outstanding")
        if shares and price > 0:
            return float(shares) * price
        return None
    raise RuntimeError(f"Polygon rate limit while fetching {symbol}")


def keep_large_caps(api_key: str, matches: list[dict]) -> list[dict]:
    kept = []

    def lookup(match: dict) -> dict | None:
        cap = market_cap(api_key, match["symbol"], match["price"])
        if cap is None or cap < MIN_MARKET_CAP:
            return None
        match["market_cap"] = cap
        return match

    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        futures = [pool.submit(lookup, match) for match in matches]
        for future in as_completed(futures):
            row = future.result()
            if row is not None:
                kept.append(row)
    kept.sort(key=lambda row: row["market_cap"], reverse=True)
    return kept


def fmt_price(value: float) -> str:
    return f"{value:,.2f}"


def fmt_volume(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{int(value):,}"


def fmt_change(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:+.2f}%"


def fetch_daily_volumes(api_key: str, symbol: str, start: date, end: date) -> list[tuple[date, float]]:
    url = DAILY_URL.format(symbol=quote(polygon_symbol(symbol), safe=""), start=start.isoformat(), end=end.isoformat())
    params: dict | None = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": api_key}
    volumes: list[tuple[date, float]] = []
    while url:
        response = requests.get(url, params=params, timeout=60)
        if response.status_code == 429:
            time.sleep(15)
            continue
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code == 404:
            return []
        if response.status_code != 200:
            message = str(body.get("message") or body.get("error") or "")
            raise RuntimeError(message or f"Polygon returned HTTP {response.status_code} for {symbol}")
        for row in body.get("results") or []:
            volume = row.get("v")
            timestamp = row.get("t")
            if volume is None or timestamp is None:
                continue
            bar_day = datetime.fromtimestamp(timestamp / 1000, tz=ZoneInfo("UTC")).date()
            volumes.append((bar_day, float(volume)))
        url = body.get("next_url")
        params = {"apiKey": api_key} if url else None
    return volumes


def months_before(day: date, months: int) -> date:
    month = day.month - months
    year = day.year
    while month <= 0:
        month += 12
        year -= 1
    for day_num in range(day.day, 0, -1):
        try:
            return date(year, month, day_num)
        except ValueError:
            continue
    return date(year, month, 28)


def fetch_daily_bars(
    api_key: str, symbol: str, start: date, end: date
) -> list[tuple[date, float, float, float]]:
    """Daily high, low, and close, oldest first."""
    url = DAILY_URL.format(symbol=quote(polygon_symbol(symbol), safe=""), start=start.isoformat(), end=end.isoformat())
    params: dict | None = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": api_key}
    bars: list[tuple[date, float, float, float]] = []
    while url:
        response = requests.get(url, params=params, timeout=60)
        if response.status_code == 429:
            time.sleep(15)
            continue
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code == 404:
            return []
        if response.status_code != 200:
            message = str(body.get("message") or body.get("error") or "")
            raise RuntimeError(message or f"Polygon returned HTTP {response.status_code} for {symbol}")
        for row in body.get("results") or []:
            high, low, close, timestamp = row.get("h"), row.get("l"), row.get("c"), row.get("t")
            if None in (high, low, close, timestamp):
                continue
            bar_day = datetime.fromtimestamp(timestamp / 1000, tz=ZoneInfo("UTC")).date()
            bars.append((bar_day, float(high), float(low), float(close)))
        url = body.get("next_url")
        params = {"apiKey": api_key} if url else None
    return bars


def average_range(api_key: str, symbol: str, screen_day: date) -> float | None:
    """Mean true range as a percent of the close over the six months through the screen day."""
    start = months_before(screen_day, RANGE_MONTHS)
    bars = fetch_daily_bars(api_key, symbol, start - timedelta(days=7), screen_day)
    ranges: list[float] = []
    previous_close: float | None = None
    for bar_day, high, low, close in bars:
        if close <= 0:
            previous_close = close if close > 0 else previous_close
            continue
        if start <= bar_day <= screen_day:
            span = high - low
            if previous_close is not None:
                span = max(span, abs(high - previous_close), abs(low - previous_close))
            ranges.append(span / close * 100)
        previous_close = close
    if len(ranges) < MIN_RANGE_SESSIONS:
        return None
    return sum(ranges) / len(ranges)


def one_year_return(api_key: str, symbol: str, screen_day: date, price: float) -> float | None:
    """Percent change from the close one year before the screen day to that day's close."""
    if price <= 0:
        return None
    anniversary = months_before(screen_day, 12)
    bars = fetch_daily_bars(api_key, symbol, anniversary - timedelta(days=10), anniversary)
    prior = [bar for bar in bars if bar[0] <= anniversary and bar[3] > 0]
    if not prior:
        return None
    bar_day, _, _, close = prior[-1]
    if (anniversary - bar_day).days > 7:
        return None
    return (price - close) / close * 100


def keep_volatile(api_key: str, matches: list[dict], screen_day: date) -> list[dict]:
    kept = []

    def lookup(match: dict) -> dict | None:
        try:
            value = average_range(api_key, match["symbol"], screen_day)
        except Exception:
            return None
        if value is None or value <= MIN_AVERAGE_RANGE:
            return None
        match["average_range"] = value
        return match

    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        futures = [pool.submit(lookup, match) for match in matches]
        for future in as_completed(futures):
            row = future.result()
            if row is not None:
                kept.append(row)
    for match in sorted(kept, key=lambda row: row["average_range"], reverse=True):
        log(f"{match['symbol']} 6-month average range {match['average_range']:.2f}%")
    return kept


def keep_positive_year(api_key: str, matches: list[dict], screen_day: date) -> list[dict]:
    kept = []

    def lookup(match: dict) -> dict | None:
        try:
            value = one_year_return(api_key, match["symbol"], screen_day, match["price"])
        except Exception:
            return None
        if value is None or value <= 0:
            return None
        match["year_return"] = value
        return match

    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        futures = [pool.submit(lookup, match) for match in matches]
        for future in as_completed(futures):
            row = future.result()
            if row is not None:
                kept.append(row)
    return kept


def average_volume(api_key: str, symbol: str, screen_day: date) -> float | None:
    """Mean daily volume over the year before the screen day."""
    start = screen_day - timedelta(days=365)
    bars = fetch_daily_volumes(api_key, symbol, start, screen_day)
    prior = [volume for day, volume in bars if start <= day < screen_day]
    if not prior:
        return None
    return sum(prior) / len(prior)


def finviz_quote(session: requests.Session, ticker: str) -> tuple[dict[str, str], str]:
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            response = session.get(FINVIZ_QUOTE_URL, params={"t": ticker}, headers=HEADERS, timeout=20)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            cells = soup.select("td.snapshot-td2")
            if not cells:
                time.sleep(1.5 * (attempt + 1))
                continue
            snapshot: dict[str, str] = {}
            for index in range(0, len(cells) - 1, 2):
                label = cells[index].get_text(" ", strip=True)
                value = cells[index + 1].get_text(" ", strip=True)
                if label:
                    snapshot[label] = value if value and value != "-" else "—"
            country_link = soup.select_one("a.quote-header_category[href*='geo_']")
            country = country_link.get_text(" ", strip=True) if country_link else ""
            if country == "United States":
                country = "USA"
            return snapshot, country
        except requests.RequestException as error:
            last_error = error
            time.sleep(1.5 * (attempt + 1))
    if last_error:
        raise last_error
    return {}, ""


def fetch_option_volumes(day: date) -> dict[str, int]:
    """Cleared contract volume by underlying for the regular option chain.

    OCC lists both sides of each trade, so the stored count is half the sum.
    Adjusted roots such as 2SNDK are left out; those are not in the standard
    chain total.
    """
    response = requests.get(
        OCC_URL,
        params={
            "reportDate": day.strftime("%Y%m%d"),
            "format": "csv",
            "volumeQueryType": "O",
            "symbolType": "ALL",
            "reportType": "D",
            "accountType": "ALL",
            "productKind": "OSTK",
            "porc": "BOTH",
        },
        headers=HEADERS,
        timeout=120,
    )
    if response.status_code != 200:
        raise RuntimeError(f"OCC returned HTTP {response.status_code} for {day.isoformat()}")
    wanted = day.strftime("%m/%d/%Y")
    totals: Counter[str] = Counter()
    for line in response.text.splitlines():
        line = line.strip().rstrip(",")
        if not line or line.startswith("quantity"):
            continue
        parts = line.split(",")
        if len(parts) < 7 or parts[6] != wanted:
            continue
        underlying = parts[1].strip().upper()
        root = parts[2].strip().upper()
        if not underlying or root[:1].isdigit():
            continue
        totals[underlying] += int(parts[0])
    volumes: dict[str, int] = {}
    for symbol, quantity in totals.items():
        volume = quantity // 2
        for key in {symbol, symbol.replace(".", "-"), symbol.replace("/", "-")}:
            volumes[key] = volume
    return volumes


def detail_row(api_key: str, match: dict) -> dict:
    screen_day = date.fromisoformat(match["date"])
    try:
        average = average_volume(api_key, match["symbol"], screen_day)
    except Exception:
        average = None
    year_return = match.get("year_return")
    session = requests.Session()
    try:
        snapshot, country = finviz_quote(session, match["symbol"])
    except Exception:
        snapshot, country = {}, ""
    row = {
        "Date": match["date"],
        "Symbol": match["symbol"],
        "Price": fmt_price(match["price"]),
        "% Change": fmt_change(match["day_change"]),
        "1 Year Return": fmt_change(year_return),
        "Volume": fmt_volume(match["volume"]),
        "Avg Volume": fmt_volume(average),
        "Options Volume": fmt_volume(match.get("options_volume")),
        "6-Month Range": (
            f"{match['average_range']:.2f}%" if match.get("average_range") is not None else "—"
        ),
        "Country": country or "—",
        "Finviz Market Cap": snapshot.get("Market Cap") or "—",
        "Finviz Shares Float": snapshot.get("Shs Float") or "—",
        "Finviz Short % of Float": snapshot.get("Short Float") or "—",
        "sort_options": match.get("options_volume") or 0,
    }
    if "next_changes" in match:
        for column, change in zip(FORWARD_COLUMNS, match["next_changes"]):
            row[column] = fmt_change(change)
    return row


def keep_active_options(matches: list[dict], day: date) -> list[dict]:
    volumes = fetch_option_volumes(day)
    kept = []
    for match in matches:
        volume = volumes.get(match["symbol"])
        if volume is None or volume <= MIN_OPTIONS_VOLUME:
            continue
        match["options_volume"] = volume
        kept.append(match)
    return kept


def table_columns(include_forward: bool) -> list[str]:
    columns = list(COLUMNS)
    if include_forward:
        at = columns.index("1 Year Return") + 1
        for offset, name in enumerate(FORWARD_COLUMNS):
            columns.insert(at + offset, name)
    return columns


def print_table(rows: list[dict], columns: list[str]) -> None:
    widths = {column: max(len(column), *(len(str(row[column])) for row in rows)) for column in columns}
    print("  ".join(column.center(widths[column]) for column in columns))
    print("  ".join("-" * widths[column] for column in columns))
    for row in rows:
        print("  ".join(str(row[column]).center(widths[column]) for column in columns))


def run_screen(as_of: date) -> int:
    started = time.perf_counter()
    log("Loading US-listed stocks...")
    universe = load_symbols()
    log(f"Universe has {len(universe)} symbols")
    api_key = load_api_key()
    sessions = load_sessions(api_key, as_of)
    matches = find_matches(universe, sessions)
    log(f"{len(matches)} names have two inside days")
    if matches:
        log("Checking market cap...")
        matches = keep_large_caps(api_key, matches)
    screen_day = sessions[0][0]
    if matches:
        log(f"{len(matches)} are above $1B. Checking option volume...")
        matches = keep_active_options(matches, screen_day)
    if matches:
        log(f"{len(matches)} have enough option volume. Checking the 6-month average range...")
        matches = keep_volatile(api_key, matches, screen_day)
    if matches:
        log(f"{len(matches)} have a wide enough range. Checking the 1-year return...")
        matches = keep_positive_year(api_key, matches, screen_day)
    forward = load_forward_sessions(api_key, screen_day) if matches else []
    for match in matches:
        if forward:
            match["next_changes"] = forward_changes(match["price"], match["symbol"], forward)
    if not matches:
        print(
            f"No stocks above $1B with more than {MIN_OPTIONS_VOLUME:,.0f} option contracts, "
            f"a close above the {SMA_DAYS}-day average, and a {RANGE_MONTHS}-month average "
            f"daily range above {MIN_AVERAGE_RANGE:.0f}%, and a 1-year return above zero "
            f"have two inside days on {screen_day.isoformat()}."
        )
    else:
        log(f"Fetching volume and Finviz details for {len(matches)} matches...")
        rows = []
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(detail_row, api_key, match) for match in matches]
            for future in as_completed(futures):
                rows.append(future.result())
        rows.sort(key=lambda row: row["sort_options"], reverse=True)
        print_table(rows, table_columns(bool(forward)))
    now = datetime.now(EASTERN)
    if screen_day == now.date() and (now.hour, now.minute) < (16, 0):
        log("The session is still open, so these are the latest prints.")
    log(f"Completed in {time.perf_counter() - started:.2f} seconds")
    return 0


def mark_handled(today: str) -> None:
    """Record that the combined daily email included this screen."""
    stamp = Path.home() / ".config" / "large-cap-inside-screener" / "last-email-date"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(today + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Screen for stocks above $1B with two inside days, more than 50,000 option contracts, a close above the 10-day average, a 6-month average daily range above 5%, and a 1-year return above zero."
    )
    parser.add_argument("--date", help="Screen date, YYYY-MM-DD. Defaults to today.")
    args = parser.parse_args()
    as_of = date.fromisoformat(args.date) if args.date else datetime.now(EASTERN).date()
    return run_screen(as_of)


if __name__ == "__main__":
    raise SystemExit(main())
