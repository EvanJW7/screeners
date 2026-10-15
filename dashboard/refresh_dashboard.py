#!/usr/bin/env python3
"""Run the capitulation screen and rewrite its dashboard.

Capitulation runs on weekdays from 4:16 AM through 8:16 PM Eastern. The other
screens stay on the regular session, 9:30 AM through 4:15 PM. Pass --force to
run outside the capitulation window.
"""

import argparse
import fcntl
import html
import importlib.util
import json
import math
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCREENERS = Path("/Users/evanwright/Cursor/screeners")
sys.path.insert(0, str(SCREENERS / "volume-profile-screener"))

import screener as s

CANVAS_DIR = Path(
    "/Users/evanwright/.cursor/projects/Users-evanwright-Cursor-volume-profile-screener/canvases"
)
DASHBOARD = CANVAS_DIR / "capitulation-screen.canvas.tsx"
PAGE = Path(__file__).resolve().parent / "dashboard.html"
PRIOR_PATH = PAGE.with_name("dashboard-prior.json")
ZONE_HITS_PATH = PAGE.with_name("dashboard-zone-hits.json")
ETFIQ = Path(
    "/Users/evanwright/.cursor/projects/Users-evanwright-Cursor-screeners-volume-profile-screener"
    "/agent-tools/1feacf93-979a-4b8f-908b-b1186d73296b.txt"
)
SKIP_UNDERLYING = {"XRP", "DRAM", "EUV"}
SKIP_FUND = {"EO"}
CENTRAL = s.ZoneInfo("America/Chicago")
SESSION_OPEN = 4 * 60 + 16
SESSION_CLOSE = 20 * 60 + 16
REGULAR_OPEN = 9 * 60 + 30
REGULAR_CLOSE = 16 * 60 + 15
EXTENDED_CLOSE = 20 * 60


def fund_aum(raw: str) -> float:
    text = raw.strip().replace("$", "").replace(",", "")
    if not text or text in {"–", "—", "-"}:
        return 0.0
    match = re.fullmatch(r"([0-9]*\.?[0-9]+)\s*([mb])?", text, re.I)
    if not match:
        return 0.0
    value = float(match.group(1))
    unit = (match.group(2) or "").lower()
    if unit == "b":
        return value * 1_000_000_000
    if unit == "m":
        return value * 1_000_000
    return value


def leveraged_etfs() -> dict[str, str]:
    """Largest 2x long fund for each underlying, by net assets."""
    grouped: dict[str, list[tuple[float, str]]] = {}
    if not ETFIQ.exists():
        s.log(f"2x ETF list missing at {ETFIQ}")
        return {}
    for line in ETFIQ.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| ") or line.startswith("| ---") or line.startswith("| Ticker"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 15:
            continue
        ticker, assets, multiple, underlying = cells[0], cells[4], cells[13], cells[14]
        if multiple != "+2x" or ticker in SKIP_FUND or underlying in SKIP_UNDERLYING:
            continue
        if not re.fullmatch(r"[A-Z]{1,6}", underlying):
            continue
        grouped.setdefault(underlying, []).append((fund_aum(assets), ticker))
    mapped = {}
    for underlying, funds in grouped.items():
        funds.sort(key=lambda item: item[0], reverse=True)
        mapped[underlying] = funds[0][1]
    return mapped


def session_is_open(moment: datetime) -> bool:
    if moment.weekday() >= 5:
        return False
    minutes = moment.hour * 60 + moment.minute
    return SESSION_OPEN <= minutes <= SESSION_CLOSE


def regular_session(moment: datetime) -> bool:
    if moment.weekday() >= 5:
        return False
    minutes = moment.hour * 60 + moment.minute
    return REGULAR_OPEN <= minutes <= REGULAR_CLOSE


def quote_time(stamp: float) -> datetime:
    if stamp > 1e17:
        stamp /= 1e9
    elif stamp > 1e14:
        stamp /= 1e6
    elif stamp > 1e11:
        stamp /= 1e3
    return datetime.fromtimestamp(stamp, s.EASTERN)


def live_quotes(api_key: str, as_of: date) -> dict[str, dict]:
    """Latest 4:00 a.m. to 8:00 p.m. trade from the current minute bar."""
    url = "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers"
    quotes: dict[str, dict] = {}
    while url:
        response = s.requests.get(url, params={"apiKey": api_key}, timeout=90)
        if response.status_code == 429:
            s.time.sleep(12)
            continue
        response.raise_for_status()
        body = response.json()
        for row in body.get("tickers") or []:
            symbol = s.normalize_symbol(str(row.get("ticker") or ""))
            minute = row.get("min") or {}
            price = minute.get("c")
            stamp = minute.get("t")
            if not symbol or not price or stamp is None:
                continue
            when = quote_time(float(stamp))
            if when.date() != as_of:
                continue
            volume = minute.get("av")
            quotes[symbol] = {
                "price": float(price),
                "volume": float(volume or 0),
                "day": as_of,
            }
        url = body.get("next_url")
        if url and "apiKey=" not in url:
            url = f"{url}&apiKey={api_key}"
    return quotes


def apply_extended(hours: s.ExtendedHours, symbol: str, dated: list, as_of: date):
    """Replace daily highs and lows with the 4:00 a.m. to 8:00 p.m. range."""
    if not dated:
        return dated, None
    spans = hours.spans(symbol, dated[0][0], as_of, live_day=as_of)
    rebuilt = []
    for day, (close, volume, high, low, open_price) in dated:
        span = spans.get(day)
        if span is not None:
            high = max(high, span[0])
            low = min(low, span[1])
        rebuilt.append((day, (close, volume, high, low, open_price)))
    today = spans.get(as_of)
    return rebuilt, None if today is None else today[1]


def extend_chart_bars(
    hours: s.ExtendedHours,
    symbol: str,
    bars: list[tuple],
    as_of: date,
    price: float,
    volume: float,
) -> list[tuple]:
    if not bars:
        return bars
    start = date.fromisoformat(bars[0][0])
    spans = hours.spans(symbol, start, as_of, live_day=as_of)
    widened = []
    for day, open_price, high, low, close, bar_volume in bars:
        span = spans.get(date.fromisoformat(day))
        if span is not None:
            high = max(high, span[0])
            low = min(low, span[1])
        if day == as_of.isoformat():
            close = price
            high = max(high, price)
            low = min(low, price)
            if volume > 0:
                bar_volume = volume
        widened.append((day, open_price, high, low, close, bar_volume))
    if widened[-1][0] < as_of.isoformat():
        span = spans.get(as_of)
        high = max(span[0], price) if span else price
        low = min(span[1], price) if span else price
        widened.append((as_of.isoformat(), price, high, low, price, volume))
    return widened


def previous_other_sections() -> str:
    """Keep the regular-session screens when capitulation refreshes outside that window."""
    if not PAGE.exists():
        return ""
    text = PAGE.read_text(encoding="utf-8")
    main = text.find("<main>")
    heading = text.find("<h2>CAPITULATION", main if main >= 0 else 0)
    if main < 0 or heading < 0:
        return ""
    section = text.rfind("<section", main, heading)
    if section < 0:
        return ""
    return text[main + len("<main>"):section]


def short_name(raw: str) -> str:
    name = re.sub(r"\s+-\s+.*", "", raw)
    name = re.sub(
        r"\s+(Common Stock|Ordinary Shares|Class [A-Z] Common Stock|American Depositary Shares).*",
        "",
        name,
        flags=re.I,
    )
    return name.strip() or raw.strip()


def security_names() -> dict[str, str]:
    names: dict[str, str] = {}
    sources = [
        (s.KEY_FILE.parent / "nasdaqlisted.txt", "Symbol"),
        (s.KEY_FILE.parent / "otherlisted.txt", "ACT Symbol"),
    ]
    for path, symbol_column in sources:
        if not path.exists():
            continue
        lines = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("File Creation Time")
        ]
        if not lines:
            continue
        header = lines[0].split("|")
        if symbol_column not in header or "Security Name" not in header:
            continue
        symbol_index = header.index(symbol_column)
        name_index = header.index("Security Name")
        for line in lines[1:]:
            parts = line.split("|")
            if len(parts) <= max(symbol_index, name_index):
                continue
            symbol = s.normalize_symbol(parts[symbol_index])
            if symbol and symbol != "N/A":
                names[symbol] = short_name(parts[name_index])
    return names


def fetch_dated(api_key: str, symbol: str, start: date, end: date) -> list[tuple]:
    bars = []
    for attempt in range(4):
        response = s.requests.get(
            s.DAILY_URL.format(symbol=symbol, start=start.isoformat(), end=end.isoformat()),
            params={"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": api_key},
            timeout=60,
        )
        if response.status_code == 429:
            s.time.sleep(15 * (attempt + 1))
            continue
        body = response.json()
        if response.status_code != 200:
            message = str(body.get("message") or body.get("error") or response.status_code)
            raise RuntimeError(f"{symbol}: {message}")
        for row in body.get("results") or []:
            close, volume, high, low, open_price, stamp = (
                row.get("c"),
                row.get("v"),
                row.get("h"),
                row.get("l"),
                row.get("o"),
                row.get("t"),
            )
            if None in (close, volume, high, low, open_price, stamp):
                continue
            session = datetime.fromtimestamp(stamp / 1000, timezone.utc).date()
            bars.append(
                (
                    session.isoformat(),
                    float(open_price),
                    float(high),
                    float(low),
                    float(close),
                    float(volume),
                )
            )
        return bars
    raise RuntimeError(f"Polygon rate limit while fetching {symbol}")


def profile_bins(bars: list[s.Bar]) -> tuple[float, float, list[float]] | None:
    if len(bars) < s.MIN_VOLUME_SESSIONS or not s.series_holds(bars):
        return None
    low = min(bar[3] for bar in bars)
    high = max(bar[2] for bar in bars)
    if low <= 0 or high <= low:
        return None
    width = (high - low) / s.BIN_COUNT
    volumes = [0.0] * s.BIN_COUNT
    for _, volume, bar_high, bar_low, _ in bars:
        if volume <= 0 or bar_high < bar_low:
            continue
        if bar_high == bar_low:
            index = min(s.BIN_COUNT - 1, max(0, int((bar_low - low) / width)))
            volumes[index] += volume
            continue
        start = min(s.BIN_COUNT - 1, max(0, int((bar_low - low) / width)))
        stop = min(s.BIN_COUNT - 1, max(0, int((bar_high - low) / width)))
        span = bar_high - bar_low
        for index in range(start, stop + 1):
            overlap = min(bar_high, low + (index + 1) * width) - max(bar_low, low + index * width)
            if overlap > 0:
                volumes[index] += volume * overlap / span
    return low, width, volumes


def labeled_zones(low: float, width: float, volumes: list[float], price: float) -> list[tuple[float, float]]:
    average = sum(volumes) / len(volumes)
    cutoff = average * s.THIN_OF_AVERAGE
    spans: list[tuple[int, int]] = []
    start = None
    for index, volume in enumerate(volumes):
        thin = volume < cutoff
        if thin and start is None:
            start = index
        elif not thin and start is not None:
            spans.append((start, index))
            start = None
    if start is not None:
        spans.append((start, len(volumes)))
    price_index = min(s.BIN_COUNT - 1, max(0, int((price - low) / width))) if width else 0
    zones = []
    for begin, end in spans:
        if end - begin >= 2 or begin <= price_index < end:
            zones.append((low + begin * width, low + end * width))
    return zones


def price_ticks(low: float, high: float) -> list[float]:
    span = high - low
    if span <= 0:
        return [round(low, 2)]
    raw = span / 4
    magnitude = 10 ** math.floor(math.log10(raw))
    chosen = 10 * magnitude
    for step in (1, 2, 2.5, 5, 10):
        if step * magnitude >= raw * 0.75:
            chosen = step * magnitude
            break
    start = math.ceil((low + span * 0.02) / chosen) * chosen
    ticks = []
    value = start
    while value < high and len(ticks) < 6:
        ticks.append(round(value, 4))
        value += chosen
    return ticks or [round((low + high) / 2, 2)]


def js_num(value: float) -> str:
    if abs(value) >= 100:
        return f"{value:.2f}"
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text if text else "0"


def component_name(symbol: str) -> str:
    parts = [part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", symbol) if part]
    stem = "".join(parts) or "Symbol"
    if stem[0].isdigit():
        stem = f"Ticker{stem}"
    return f"{stem}VolumeProfile"


def chart_path(symbol: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", symbol.lower()).strip("-")
    return CANVAS_DIR / f"{slug}-volume-profile.canvas.tsx"


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def render_chart(
    symbol: str,
    company: str,
    price_bars: list[tuple],
    profile_end: date,
    screen_day: date,
    in_progress: bool,
    years: int = 2,
) -> str:
    profile_rows = [bar for bar in price_bars if bar[0] <= profile_end.isoformat()]
    profile_input = [(bar[4], bar[5], bar[2], bar[3], bar[1]) for bar in profile_rows]
    built = profile_bins(profile_input)
    if built is None or not price_bars:
        raise RuntimeError(f"{symbol}: no profile to chart")
    low, width, volumes = built
    high = low + width * s.BIN_COUNT
    last_close = price_bars[-1][4]
    zones = labeled_zones(low, width, volumes, last_close)
    zone = s.blue_zone(profile_input, last_close)
    versus = zone[1] if zone else 0
    zone_label = zone[0] if zone else ""
    span = high - low
    price_min = 0 if low < span * 0.2 else low - span * 0.04
    price_max = high + span * 0.04
    ticks = [tick for tick in price_ticks(price_min, price_max) if price_min < tick < price_max]
    count = len(price_bars)
    tick_count = min(8, count)
    date_indexes = sorted({round(i * (count - 1) / (tick_count - 1)) for i in range(tick_count)}) if count > 1 else [0]
    date_ticks = [price_bars[index][0] for index in date_indexes]
    title = company if company and company.upper() != symbol else symbol
    screen_label = screen_day.strftime("%B %-d, %Y")
    session_note = (
        f"the delayed {screen_label} session, still in progress"
        if in_progress
        else f"the {screen_label} session"
    )
    profile_label = profile_end.strftime("%B %-d, %Y")
    bars_js = ",".join(
        f'["{bar[0]}",{js_num(bar[1])},{js_num(bar[2])},{js_num(bar[3])},{js_num(bar[4])},{js_num(bar[5])}]'
        for bar in price_bars
    )
    profile_js = ",".join(
        f"[{js_num(low + index * width)},{js_num(volume)}]" for index, volume in enumerate(volumes) if volume > 0
    )
    zones_js = ",".join(f"[{js_num(zone_low)},{js_num(zone_high)}]" for zone_low, zone_high in zones)
    ticks_js = ",".join(js_num(tick) for tick in ticks)
    dates_js = ",".join(f'"{day}"' for day in date_ticks)
    name = component_name(symbol)
    last_price = f"${last_close:.2f}"
    versus_text = f"{versus:.0f}%"
    return f'''import {{ H1, Row, Stack, Stat, Text, useHostTheme, useState }} from "cursor/canvas";

type Bar = [string, number, number, number, number, number];
type Bin = [number, number];

const BARS: Bar[] = [{bars_js}];
const PROFILE: Bin[] = [{profile_js}];
const BIN = {js_num(width)};
const ZONES: [number, number][] = [{zones_js}];
const PRICE_MIN = {js_num(price_min)};
const PRICE_MAX = {js_num(price_max)};
const TICKS = [{ticks_js}];
const DATE_TICKS = [{dates_js}];

const WIDTH = 920;
const HEIGHT = 520;
const LEFT = 58;
const TOP = 18;
const PRICE_W = 640;
const GAP = 18;
const PROFILE_W = 168;
const VOL_H = 72;
const BOTTOM = 32;

export default function {name}() {{
  const theme = useHostTheme();
  const [hover, setHover] = useState<number | null>(null);
  const plotBottom = HEIGHT - BOTTOM - VOL_H - 10;
  const plotH = plotBottom - TOP;
  const n = BARS.length;
  const maxVol = Math.max(...BARS.map((bar) => bar[5]));
  const maxProfile = Math.max(...PROFILE.map((bin) => bin[1]));
  const averageBar = PROFILE.reduce((sum, bin) => sum + bin[1], 0) / 100;
  const yOf = (price: number) => TOP + ((PRICE_MAX - price) / (PRICE_MAX - PRICE_MIN)) * plotH;
  const xOf = (index: number) => LEFT + (index / (n - 1)) * PRICE_W;
  const closePath = BARS.map((bar, index) => `${{index === 0 ? "M" : "L"}}${{xOf(index).toFixed(1)}},${{yOf(bar[4]).toFixed(1)}}`).join("");
  const band = `${{BARS.map((bar, index) => `${{index === 0 ? "M" : "L"}}${{xOf(index).toFixed(1)}},${{yOf(bar[2]).toFixed(1)}}`).join("")}} ${{[...BARS].reverse().map((bar, index) => `L${{xOf(n - 1 - index).toFixed(1)}},${{yOf(bar[3]).toFixed(1)}}`).join("")}} Z`;
  const dateIndex = (day: string) => {{
    let best = 0;
    let bestGap = Infinity;
    BARS.forEach((bar, index) => {{
      const gap = Math.abs(Date.parse(bar[0]) - Date.parse(day));
      if (gap < bestGap) {{
        bestGap = gap;
        best = index;
      }}
    }});
    return best;
  }};
  const onMove = (event: {{ currentTarget: SVGSVGElement; clientX: number; clientY: number }}) => {{
    const rect = event.currentTarget.getBoundingClientRect();
    const scale = WIDTH / rect.width;
    const x = (event.clientX - rect.left) * scale;
    if (x < LEFT || x > LEFT + PRICE_W) {{
      setHover(null);
      return;
    }}
    const index = Math.round(((x - LEFT) / PRICE_W) * (n - 1));
    setHover(Math.max(0, Math.min(n - 1, index)));
  }};
  const hovered = hover === null ? null : BARS[hover];
  const profileX = LEFT + PRICE_W + GAP;
  return (
    <Stack gap={{16}}>
      <Stack gap={{6}}>
        <H1>{symbol} blue zone</H1>
        <Text tone="secondary">
          {title} from {{BARS[0][0]}} through {session_note}. Highs and lows run from 4:00 a.m. to 8:00 p.m. Eastern. The volume profile is the {"three" if years == 3 else "two"} years through {profile_label}, before this week's decline. A blue zone is a bar shorter than 20% of the average bar. The close passes when it sits in one, or within 4 bars of one, on the two-year or three-year profile.
        </Text>
      </Stack>
      <Row gap={{24}} align="end">
        <Stat value="{last_price}" label="Last price" />
        <Stat value="{versus_text}" label="That bar versus the average" />
        <Stat value="{zone_label}" label="Blue zone at the last price" tone="warning" />
      </Row>
      <svg viewBox={{`0 0 ${{WIDTH}} ${{HEIGHT}}`}} width="100%" role="img" aria-label="{symbol} daily price with its volume profile" onMouseMove={{onMove}} onMouseLeave={{() => setHover(null)}} style={{{{ display: "block", background: theme.bg.editor }}}}>
        <text x={{LEFT}} y={{14}} fill={{theme.text.secondary}} fontSize={{11}}>Price (USD)</text>
        {{TICKS.map((price) => (
          <g key={{price}}>
            <line x1={{LEFT}} x2={{LEFT + PRICE_W}} y1={{yOf(price)}} y2={{yOf(price)}} stroke={{theme.stroke.tertiary}} strokeWidth={{1}} />
            <text x={{LEFT - 8}} y={{yOf(price) + 4}} textAnchor="end" fill={{theme.text.tertiary}} fontSize={{11}}>{{price < 10 ? price.toFixed(2) : price.toFixed(0)}}</text>
          </g>
        ))}}
        <path d={{band}} fill={{theme.fill.tertiary}} />
        <path d={{closePath}} fill="none" stroke={{theme.accent.primary}} strokeWidth={{1.6}} />
        {{PROFILE.map(([price, volume]) => {{
          const thin = volume < averageBar * {s.THIN_OF_AVERAGE};
          const top = yOf(price + BIN);
          const barHeight = Math.max(1, yOf(price) - top - 0.2);
          const barWidth = (volume / maxProfile) * PROFILE_W;
          return <rect key={{price}} x={{profileX}} y={{top}} width={{barWidth}} height={{barHeight}} fill={{thin ? theme.chart.sequence[1] : theme.fill.primary}} />;
        }})}}
        {{ZONES.map(([zoneLow, zoneHigh]) => (
          <text key={{zoneLow}} x={{profileX + 8}} y={{(yOf(zoneLow) + yOf(zoneHigh)) / 2}} fill={{theme.text.primary}} fontSize={{11}}>{{`$${{zoneLow.toFixed(2)}}–$${{zoneHigh.toFixed(2)}}`}}</text>
        ))}}
        <text x={{profileX}} y={{14}} fill={{theme.text.secondary}} fontSize={{11}}>Volume at price</text>
        {{BARS.map((bar, index) => {{
          const height = (bar[5] / maxVol) * (VOL_H - 14);
          const slot = PRICE_W / n;
          return <rect key={{bar[0]}} x={{xOf(index) - slot / 2}} y={{HEIGHT - BOTTOM - height}} width={{Math.max(0.6, slot * 0.7)}} height={{height}} fill={{hover === index ? theme.accent.primary : theme.fill.tertiary}} />;
        }})}}
        <text x={{LEFT}} y={{HEIGHT - BOTTOM - VOL_H + 12}} fill={{theme.text.tertiary}} fontSize={{11}}>Daily volume (shares)</text>
        {{DATE_TICKS.map((day) => {{
          const index = dateIndex(day);
          const label = index === n - 1 ? "{screen_day.strftime("%b %-d")}" : day.slice(0, 7);
          return <text key={{day}} x={{xOf(index)}} y={{HEIGHT - 8}} textAnchor="middle" fill={{theme.text.tertiary}} fontSize={{11}}>{{label}}</text>;
        }})}}
        {{hover !== null && hovered ? (
          <g>
            <line x1={{xOf(hover)}} x2={{xOf(hover)}} y1={{TOP}} y2={{plotBottom}} stroke={{theme.text.tertiary}} strokeWidth={{1}} />
            <rect x={{Math.min(xOf(hover) + 8, LEFT + PRICE_W - 148)}} y={{TOP + 8}} width={{140}} height={{78}} fill={{theme.bg.elevated}} stroke={{theme.stroke.primary}} />
            <text x={{Math.min(xOf(hover) + 16, LEFT + PRICE_W - 140)}} y={{TOP + 26}} fill={{theme.text.primary}} fontSize={{12}}>{{hovered[0]}}</text>
            <text x={{Math.min(xOf(hover) + 16, LEFT + PRICE_W - 140)}} y={{TOP + 44}} fill={{theme.text.secondary}} fontSize={{11}}>{{`O ${{hovered[1].toFixed(2)}}  C ${{hovered[4].toFixed(2)}}`}}</text>
            <text x={{Math.min(xOf(hover) + 16, LEFT + PRICE_W - 140)}} y={{TOP + 60}} fill={{theme.text.secondary}} fontSize={{11}}>{{`H ${{hovered[2].toFixed(2)}}  L ${{hovered[3].toFixed(2)}}`}}</text>
            <text x={{Math.min(xOf(hover) + 16, LEFT + PRICE_W - 140)}} y={{TOP + 76}} fill={{theme.text.tertiary}} fontSize={{11}}>{{`${{(hovered[5] / 1e6).toFixed(1)}}M shares`}}</text>
          </g>
        ) : null}}
      </svg>
      <Row gap={{16}} align="center">
        <Text size="small" tone="secondary">Close</Text>
        <Text size="small" tone="tertiary">4:00 a.m.–8:00 p.m. high and low</Text>
        <Text size="small" tone="tertiary">Blue bars are under 20% of the average bar</Text>
      </Row>
      <Text size="small" tone="tertiary">
        Source: Polygon adjusted daily volume, with highs and lows from 4:00 a.m. to 8:00 p.m. Eastern. The profile stops on {profile_label}, so this week's selling is not part of the average. {screen_label}{" is still in progress." if in_progress else "."}
      </Text>
    </Stack>
  );
}}
'''


def week_text(change: float) -> str:
    return f"−{abs(change):.1f}%" if change < 0 else f"+{change:.1f}%"


def day_text(change: float | None) -> str:
    if change is None:
        return "—"
    return f"−{abs(change):.2f}%" if change < 0 else f"+{change:.2f}%"


def tone(change: float | None) -> str:
    if change is None or change == 0:
        return ""
    return "down" if change < 0 else "up"


def format_cap(cap: float) -> str:
    return f"${cap / 1_000_000_000:.2f}B"


def versus_text(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}%"


def zone_cell(label: str | None) -> str:
    if not label:
        return '<td class="num">—</td>'
    return f'<td><span class="zone">{html.escape(label)}</span></td>'


def versus_cell(value: float | None) -> str:
    if value is None:
        return '<td class="num">—</td>'
    return f'<td class="num cyan">{value:.1f}%</td>'


def zone_bounds(label: str | None) -> tuple[float, float] | None:
    if not label:
        return None
    parts = re.split(r"[–-]", label.replace("$", "").replace(",", ""))
    if len(parts) != 2:
        return None
    try:
        low, high = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    return low, high


def price_in_zone(price: float, label: str | None) -> bool:
    bounds = zone_bounds(label)
    if bounds is None:
        return False
    low, high = bounds
    return low <= price <= high


def range_overlaps_zone(high: float, low: float, label: str | None) -> bool:
    bounds = zone_bounds(label)
    if bounds is None:
        return False
    zone_low, zone_high = bounds
    return low <= zone_high and high >= zone_low


def print_in_zone(high: float, low: float, close: float, zone_two: str | None, zone_three: str | None) -> float | None:
    """A price from this bar that traded inside either zone."""
    if price_in_zone(close, zone_two) or price_in_zone(close, zone_three):
        return close
    if price_in_zone(low, zone_two) or price_in_zone(low, zone_three):
        return low
    if price_in_zone(high, zone_two) or price_in_zone(high, zone_three):
        return high
    if range_overlaps_zone(high, low, zone_two) or range_overlaps_zone(high, low, zone_three):
        return low
    return None


def first_zone_print(
    api_key: str,
    symbol: str,
    day: date,
    zone_two: str | None,
    zone_three: str | None,
) -> tuple[datetime, float] | None:
    """First 4:00 a.m. to 8:00 p.m. print that traded inside either zone."""
    start = datetime.combine(day, datetime.min.time(), s.EASTERN).replace(hour=4)
    end = start.replace(hour=20)
    url = (
        "https://api.polygon.io/v2/aggs/ticker/"
        f"{symbol}/range/1/minute/{int(start.timestamp() * 1000)}/{int(end.timestamp() * 1000)}"
    )
    params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": api_key}
    while url:
        response = s.requests.get(url, params=params, timeout=90)
        if response.status_code == 429:
            s.time.sleep(10)
            continue
        try:
            body = response.json()
        except ValueError:
            return None
        if response.status_code != 200:
            s.log(f"{symbol}: zone scan failed ({response.status_code})")
            return None
        for row in body.get("results") or []:
            stamp, high, low, close = row.get("t"), row.get("h"), row.get("l"), row.get("c")
            if None in (stamp, high, low, close):
                continue
            when = datetime.fromtimestamp(float(stamp) / 1000, s.EASTERN)
            minute = when.hour * 60 + when.minute
            if minute < 4 * 60 or minute >= 20 * 60:
                continue
            touch = print_in_zone(float(high), float(low), float(close), zone_two, zone_three)
            if touch is not None:
                return when.astimezone(CENTRAL), touch
        url = body.get("next_url")
        params = None
        if url and "apiKey=" not in url:
            url = f"{url}&apiKey={api_key}"
    return None


def change_at(live_price: float, live_change: float | None, touch_price: float) -> float | None:
    if live_change is None or live_price <= 0 or touch_price <= 0:
        return live_change
    base = live_price / (1 + live_change / 100)
    if base <= 0:
        return live_change
    return (touch_price / base - 1) * 100


def trading_in_zone(price: float, zone_two: str | None, zone_three: str | None) -> bool:
    """The live price sits inside the two-year zone or the three-year zone."""
    return price_in_zone(price, zone_two) or price_in_zone(price, zone_three)


def load_zone_hits(screen_day: date) -> list[dict]:
    try:
        data = json.loads(ZONE_HITS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict) or data.get("date") != screen_day.isoformat():
        return []
    hits = data.get("hits")
    return [hit for hit in hits if isinstance(hit, dict) and hit.get("symbol")] if isinstance(hits, list) else []


def save_zone_hits(screen_day: date, hits: list[dict]) -> None:
    payload = {"date": screen_day.isoformat(), "hits": hits}
    write_atomic(ZONE_HITS_PATH, json.dumps(payload, indent=2) + "\n")


def record_zone_hits(
    screen_day: date,
    matches: list[tuple],
    etfs: dict[str, str],
    updated: datetime,
    api_key: str,
) -> list[dict]:
    """Keep the first print that traded inside a zone today, even if price has left it."""
    hits = load_zone_hits(screen_day)
    seen = {hit["symbol"] for hit in hits}
    for (
        symbol, price, change, _average_volume, _average_range,
        timeframe, zone_two, versus_two, zone_three, versus_three,
        _above, _session_volume, _market_cap, day_change, _short_float,
    ) in matches:
        if symbol in seen:
            continue
        found = first_zone_print(api_key, symbol, screen_day, zone_two, zone_three)
        if found is None:
            if not trading_in_zone(price, zone_two, zone_three):
                continue
            touch_at, touch_price = updated, price
        else:
            touch_at, touch_price = found
        stamp = touch_at.strftime("%-I:%M %p CT")
        hits.append({
            "symbol": symbol,
            "etf": etfs.get(symbol, "—"),
            "price": touch_price,
            "day": change_at(price, day_change, touch_price),
            "week": change_at(price, change, touch_price),
            "timeframe": timeframe,
            "zone_two": zone_two,
            "versus_two": versus_two,
            "zone_three": zone_three,
            "versus_three": versus_three,
            "hit": stamp,
        })
        seen.add(symbol)
        s.log(f"{symbol}: recorded inside a blue zone at {stamp}")
    def hit_time(hit: dict) -> datetime:
        try:
            return datetime.strptime(str(hit.get("hit") or "").replace(" CT", ""), "%I:%M %p")
        except ValueError:
            return datetime.max

    hits.sort(key=hit_time)
    save_zone_hits(screen_day, hits)
    return hits


def year_average_volumes(api_key: str, symbols: list[str], screen_day: date) -> dict[str, float]:
    """Mean daily share volume over the 365 days ending on the screen day."""
    start = screen_day - timedelta(days=365)

    def one(symbol: str) -> tuple[str, float | None]:
        try:
            dated = s.fetch_dated_daily(api_key, symbol, start, screen_day)
        except Exception as error:
            s.log(f"{symbol}: year volume failed ({error})")
            return symbol, None
        volumes = [bar[1] for _day, bar in dated]
        if len(volumes) < s.MIN_VOLUME_SESSIONS:
            return symbol, None
        return symbol, sum(volumes) / len(volumes)

    if not symbols:
        return {}
    with s_pool(6) as pool:
        rows = list(pool.map(one, symbols))
    return {symbol: value for symbol, value in rows if value is not None}


def render_dashboard(
    updated: datetime,
    screen_day: date,
    base_day: date,
    matches: list[tuple],
    liquid: int,
    declined: int,
    in_progress: bool,
    links: dict[str, Path],
    year_volumes: dict[str, float] | None = None,
    etfs: dict[str, str] | None = None,
) -> str:
    updated_label = updated.strftime("%-I:%M %p CT").replace("AM", "AM").replace("PM", "PM")
    session_label = screen_day.strftime("%B %-d, %Y")
    base_label = base_day.strftime("%B %-d")
    progress = " The session is still in progress." if in_progress else ""
    averages = year_volumes or {}
    funds = etfs or {}
    rows = []
    for (
        symbol, price, change, _average_volume, average_range,
        timeframe, zone_two, versus_two, zone_three, versus_three,
        _above, session_volume, market_cap, day_change, short_float,
    ) in matches:
        link = links.get(symbol)
        symbol_cell = f'<Text>[{symbol}]({link})</Text>' if link else f'"{symbol}"'
        rows.append(
            "  [\n"
            f"    {symbol_cell},\n"
            f'    "{funds.get(symbol, "—")}",\n'
            f'    "{day_text(day_change)}",\n'
            f'    "{week_text(change)}",\n'
            f'    "{s.format_volume(averages[symbol]) if symbol in averages else "—"}",\n'
            f'    "{s.format_volume(session_volume)}",\n'
            f'    "{format_cap(market_cap)}",\n'
            f'    "{short_float}",\n'
            f'    "{average_range:.1f}%",\n'
            f'    "{timeframe}",\n'
            f'    "{price:.2f}",\n'
            f'    "{zone_two or "—"}",\n'
            f'    "{versus_text(versus_two)}",\n'
            f'    "{zone_three or "—"}",\n'
            f'    "{versus_text(versus_three)}",\n'
            "  ],"
        )
    row_block = "\n".join(rows)
    imports = 'import { H1, Row, Stack, Stat, Table, Text } from "cursor/canvas";'
    table = ""
    if rows:
        table = f'''      <Table
        headers={{["Symbol", "2x ETF", "Day", "Week", "Avg volume", "Volume", "Market cap", "Short float", "Avg range", "Timeframe", "Price", "2-year zone", "2-year vs avg", "3-year zone", "3-year vs avg"]}}
        rows={{ROWS}}
        columnAlign={{["left", "left", "right", "right", "right", "right", "right", "right", "right", "left", "right", "left", "right", "left", "right"]}}
        striped
      />'''
    rows_const = f"const ROWS = [\n{row_block}\n];\n\n" if rows else ""
    if not rows:
        imports = 'import { H1, Row, Stack, Stat, Text } from "cursor/canvas";'
    return f'''{imports}

{rows_const}export default function CapitulationScreen() {{
  return (
    <Stack gap={{16}}>
      <Stack gap={{6}}>
        <H1>Capitulation screen</H1>
        <Text tone="secondary">
          Updated {updated_label}. {session_label} versus the {base_label} close.{progress} Refreshes every 15 minutes from 4:16 AM to 8:16 PM ET on weekdays. The symbol opens that name's volume-profile chart.
        </Text>
      </Stack>
      <Row gap={{24}} align="end" wrap>
        <Stat value="{liquid}" label="Average 5M shares and a 5% range" />
        <Stat value="{declined}" label="Down at least 20% over five sessions" />
        <Stat value="{len(matches)}" label="In a blue zone, above the lows" tone="warning" />
      </Row>
      {table}
      <Text size="small" tone="tertiary">
        Source: Polygon adjusted daily volume. Profile highs and lows run from 4:00 a.m. to 8:00 p.m. Eastern. Leveraged ETFs are excluded from the screen. The 2x ETF column is the largest 2x long fund for that stock. Avg volume is the mean daily share volume over the last year. Volume is today's shares. The screen still requires a 5 million share average over the last 20 sessions. Range is the mean of the last year. The week is the latest close versus the close five sessions earlier. Market cap has to be above $2 billion. Short float is from Finviz. The profile is the prior two years or three years through {base_label}, {screen_day.year}. A blue zone is a bar shorter than 20% of the average bar. The close has to sit in one, or within 4 bars of one, on either profile. Names within 15% of the two-year low are left out.
      </Text>
    </Stack>
  );
}}
'''


def pct_class(text: str) -> str:
    cleaned = text.replace("−", "-").replace("%", "").replace(",", "").replace("+", "").strip()
    try:
        value = float(cleaned)
    except ValueError:
        return ""
    if value < 0:
        return "down"
    if value > 0:
        return "up"
    return ""


SCREEN_HEADERS = ["Ticker", "Last", "Day", "Volume", "Float", "Short float"]


def quote_row(ticker: str, last: str, day: str, volume: str, shares: str, short: str) -> list[tuple[str, str]]:
    return [
        (ticker, "symbol"),
        (last, "num"),
        (day, f"num {pct_class(day)}"),
        (volume, "num"),
        (shares or "—", "num"),
        (short or "—", "num"),
    ]


def load_prior() -> dict[str, list[str]]:
    try:
        data = json.loads(PRIOR_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: list(value) for key, value in data.items() if isinstance(value, list)}


def save_prior(sections: dict[str, list[str] | None]) -> None:
    prior = load_prior()
    for key, symbols in sections.items():
        if symbols is not None:
            prior[key] = symbols
    write_atomic(PRIOR_PATH, json.dumps(prior, indent=2) + "\n")


def fresh_names(key: str, symbols: list[str] | None, prior: dict[str, list[str]]) -> set[str]:
    if symbols is None or key not in prior:
        return set()
    return set(symbols) - set(prior[key])


def section_table(
    headers: list[str],
    rows: list[list[tuple[str, str]]] | None,
    noun: str,
    fresh: set[str] | None = None,
) -> str:
    fresh = fresh or set()
    if rows is None:
        body = f'<tr><td colspan="{len(headers)}" class="empty">This screen did not finish.</td></tr>'
    elif not rows:
        body = f'<tr><td colspan="{len(headers)}" class="empty">No names this run.</td></tr>'
    else:
        lines = []
        for row in rows:
            ticker = row[0][0]
            arrived = ticker in fresh
            cells = []
            for index, (text, css) in enumerate(row):
                shown = html.escape(text)
                if index == 0 and arrived:
                    shown += ' <span class="new">New</span>'
                cells.append(f'<td class="{css}">{shown}</td>')
            attr = ' class="fresh"' if arrived else ""
            lines.append(f"<tr{attr}>" + "".join(cells) + "</tr>")
        body = "\n".join(lines)
    heads = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    count = "—" if rows is None else str(len(rows))
    return f'''<section class="panel">
    <h2>{html.escape(noun)} <span>{count}</span></h2>
    <table>
      <thead><tr>{heads}</tr></thead>
      <tbody>
        {body}
      </tbody>
    </table>
    </section>'''


def render_zone_log(hits: list[dict]) -> str:
    body = []
    for hit in hits:
        week = hit.get("week")
        body.append(
            "<tr>"
            f'<td class="symbol">{html.escape(str(hit.get("symbol") or ""))}</td>'
            f'<td class="symbol">{html.escape(str(hit.get("etf") or "—"))}</td>'
            f'<td>{html.escape(str(hit.get("hit") or "—"))}</td>'
            f'<td class="num">{float(hit["price"]):.2f}</td>'
            f'<td class="num {tone(hit.get("day"))}">{day_text(hit.get("day"))}</td>'
            f'<td class="num {tone(week)}">{week_text(week) if week is not None else "—"}</td>'
            f'<td>{html.escape(str(hit.get("timeframe") or "—"))}</td>'
            f'{zone_cell(hit.get("zone_two"))}'
            f'{versus_cell(hit.get("versus_two"))}'
            f'{zone_cell(hit.get("zone_three"))}'
            f'{versus_cell(hit.get("versus_three"))}'
            "</tr>"
        )
    rows = "\n".join(body) if body else '<tr><td colspan="11" class="empty">No name has traded inside a zone today.</td></tr>'
    return f'''<section class="panel">
    <h2>HIT THE ZONE <span>{len(hits)}</span></h2>
    <table class="zone-log">
      <thead>
        <tr>
          <th>Symbol</th><th>2x ETF</th><th>Hit</th><th>Last</th><th>Day</th><th>Week</th><th>Timeframe</th><th>2-year zone</th><th>2-year vs avg</th><th>3-year zone</th><th>3-year vs avg</th>
        </tr>
      </thead>
      <tbody>
        {rows}
      </tbody>
    </table>
    <p class="note">The first time each name's price traded inside its two-year or three-year zone today. It stays here after the price leaves.</p>
    </section>'''


def render_page(
    updated: datetime,
    screen_day: date,
    base_day: date,
    matches: list[tuple],
    liquid: int,
    declined: int,
    in_progress: bool,
    extra: str = "",
    fresh: set[str] | None = None,
    year_volumes: dict[str, float] | None = None,
    etfs: dict[str, str] | None = None,
    zone_hits: list[dict] | None = None,
) -> str:
    updated_label = updated.strftime("%-I:%M %p CT")
    session_label = screen_day.strftime("%b %-d, %Y")
    base_label = base_day.strftime("%b %-d")
    status = "SESSION OPEN" if in_progress else "SESSION CLOSED"
    status_class = "live" if in_progress else "closed"
    arrived = fresh or set()
    averages = year_volumes or {}
    funds = etfs or {}
    zone_log = render_zone_log(zone_hits or [])
    body = []
    for (
        symbol, price, change, _average_volume, average_range,
        timeframe, zone_two, versus_two, zone_three, versus_three,
        _above, session_volume, market_cap, day_change, short_float,
    ) in matches:
        is_new = symbol in arrived
        new_mark = ' <span class="new">New</span>' if is_new else ""
        classes = []
        if is_new:
            classes.append("fresh")
        if trading_in_zone(price, zone_two, zone_three):
            classes.append("in-zone")
        row_class = " ".join(classes)
        fund = funds.get(symbol, "—")
        body.append(
            f'<tr class="{row_class}">'
            f'<td class="symbol">{html.escape(symbol)}{new_mark}</td>'
            f'<td class="symbol">{html.escape(fund)}</td>'
            f'<td class="num {tone(day_change)}">{day_text(day_change)}</td>'
            f'<td class="num {tone(change)}">{week_text(change)}</td>'
            f'<td class="num">{s.format_volume(averages[symbol]) if symbol in averages else "—"}</td>'
            f'<td class="num">{s.format_volume(session_volume)}</td>'
            f'<td class="num">{format_cap(market_cap)}</td>'
            f'<td class="num">{html.escape(short_float)}</td>'
            f'<td class="num">{average_range:.1f}%</td>'
            f'<td>{html.escape(timeframe)}</td>'
            f'<td class="num">{price:.2f}</td>'
            f'{zone_cell(zone_two)}'
            f'{versus_cell(versus_two)}'
            f'{zone_cell(zone_three)}'
            f'{versus_cell(versus_three)}'
            "</tr>"
        )
    rows = "\n".join(body) if body else '<tr><td colspan="15" class="empty">No names passed this run.</td></tr>'
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Main Dashboard</title>
  <style>
    :root {{
      --bg: #0c0f12;
      --panel: #141a20;
      --line: #24303a;
      --text: #e8eef2;
      --muted: #8b98a5;
      --up: #00b85c;
      --down: #e23b3b;
      --cyan: #4cc3ff;
      --gold: #f5c16c;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background:
        radial-gradient(980px 280px at 0% 0%, rgba(76, 195, 255, 0.10), transparent 58%),
        radial-gradient(720px 240px at 100% 0%, rgba(0, 184, 92, 0.06), transparent 50%),
        var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    header {{
      position: sticky;
      top: 0;
      z-index: 3;
      display: flex;
      align-items: center;
      gap: 14px;
      padding: 14px 22px;
      background: rgba(12, 15, 18, 0.88);
      border-bottom: 1px solid var(--line);
      box-shadow: inset 0 -1px 0 rgba(76, 195, 255, 0.45);
      backdrop-filter: blur(18px);
    }}
    header h1 {{
      margin: 0;
      font-size: 15px;
      letter-spacing: 0.18em;
      font-weight: 700;
      text-transform: uppercase;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 7px;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.08em;
      padding: 5px 9px;
      border-radius: 999px;
    }}
    .badge.live {{ color: #04140a; background: var(--up); }}
    .badge.closed {{ color: #1a1208; background: var(--gold); }}
    .pip {{
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: currentColor;
    }}
    .badge.live .pip {{ animation: pulse 1.6s ease-out infinite; }}
    @keyframes pulse {{
      0% {{ box-shadow: 0 0 0 0 rgba(4, 20, 10, 0.55); }}
      70% {{ box-shadow: 0 0 0 6px rgba(4, 20, 10, 0); }}
      100% {{ box-shadow: 0 0 0 0 rgba(4, 20, 10, 0); }}
    }}
    .clock {{ margin-left: auto; color: #ffffff; font-variant-numeric: tabular-nums; font-size: 13px; font-weight: 600; }}
    main {{ padding: 18px 22px 36px; display: flex; flex-direction: column; gap: 16px; }}
    .panel {{
      background:
        linear-gradient(180deg, rgba(76, 195, 255, 0.06), transparent 48px),
        var(--panel);
      border: 1px solid var(--line);
      border-radius: 12px;
      overflow: hidden;
      box-shadow: 0 16px 40px rgba(0, 0, 0, 0.28);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: transparent;
    }}
    th {{
      text-align: right;
      padding: 9px 14px 11px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      border-bottom: 1px solid var(--line);
      background: rgba(16, 22, 28, 0.72);
    }}
    td {{
      padding: 11px 14px;
      border-bottom: 1px solid #1c262e;
      font-variant-numeric: tabular-nums;
      font-size: 15px;
    }}
    tbody tr:last-child td {{ border-bottom: 0; }}
    tbody tr:hover td {{ background: #1a232c; }}
    th:first-child, td:first-child {{ text-align: left; }}
    table.blotter td {{ color: #ffffff; }}
    table.blotter td.up {{ color: var(--up); }}
    table.blotter td.down {{ color: var(--down); }}
    table.blotter .cyan, table.blotter .zone {{ color: var(--cyan); }}
    table.blotter th:nth-child(2), table.blotter td:nth-child(2),
    table.blotter th:nth-child(10), table.blotter td:nth-child(10),
    table.blotter th:nth-child(12), table.blotter td:nth-child(12),
    table.blotter th:nth-child(14), table.blotter td:nth-child(14) {{ text-align: left; }}
    tr.fresh td {{ background: #2c2618; }}
    tr.fresh:hover td {{ background: #3a3222; }}
    table.blotter tr.in-zone td {{ background: #123848; }}
    table.blotter tr.in-zone:hover td {{ background: #18506a; }}
    table.blotter tr.in-zone td:first-child {{ box-shadow: inset 3px 0 0 var(--cyan); }}
    table.zone-log th:nth-child(2), table.zone-log td:nth-child(2),
    table.zone-log th:nth-child(3), table.zone-log td:nth-child(3),
    table.zone-log th:nth-child(7), table.zone-log td:nth-child(7),
    table.zone-log th:nth-child(8), table.zone-log td:nth-child(8),
    table.zone-log th:nth-child(10), table.zone-log td:nth-child(10) {{ text-align: left; }}
    .symbol {{
      font-family: ui-monospace, "SF Mono", Menlo, monospace;
      font-weight: 700;
      letter-spacing: 0.06em;
    }}
    .new {{
      color: #1a1208;
      background: var(--gold);
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      margin-left: 8px;
      padding: 2px 6px;
      border-radius: 999px;
      vertical-align: 1px;
    }}
    .num {{ text-align: right; font-family: ui-monospace, "SF Mono", Menlo, monospace; }}
    .up {{ color: var(--up); }}
    .down {{ color: var(--down); }}
    .cyan {{ color: var(--cyan); }}
    .zone {{
      display: inline-block;
      color: var(--cyan);
      background: #102734;
      border: 1px solid #1d4d66;
      border-radius: 999px;
      padding: 3px 9px;
      font-family: ui-monospace, "SF Mono", Menlo, monospace;
      font-size: 13px;
    }}
    .empty {{ color: var(--muted); text-align: left; }}
    h2 {{
      display: flex;
      align-items: center;
      gap: 10px;
      margin: 0;
      padding: 13px 16px 11px;
      font-size: 12px;
      letter-spacing: 0.16em;
      font-weight: 700;
    }}
    h2 span {{
      margin-left: auto;
      color: var(--cyan);
      min-width: 1.7em;
      padding: 2px 8px;
      text-align: center;
      font-variant-numeric: tabular-nums;
      letter-spacing: 0;
      background: rgba(76, 195, 255, 0.12);
      border: 1px solid rgba(76, 195, 255, 0.38);
      border-radius: 999px;
    }}
    .hide {{
      margin-left: 10px;
      border: 0;
      padding: 0 4px;
      background: transparent;
      color: var(--muted);
      font-size: 16px;
      line-height: 1;
      cursor: pointer;
    }}
    .hide:hover {{ color: var(--text); }}
    tr.hidden {{ display: none; }}
    .show-hidden {{
      margin-left: 8px;
      border: 1px solid var(--line);
      background: transparent;
      color: var(--muted);
      border-radius: 999px;
      padding: 3px 8px;
      font-size: 11px;
      letter-spacing: 0.04em;
      cursor: pointer;
    }}
    .show-hidden:hover {{ color: var(--text); border-color: var(--muted); }}
    .note {{
      color: var(--muted);
      font-size: 12px;
      margin: 2px 4px 0;
      max-width: 920px;
      line-height: 1.5;
    }}
  </style>
</head>
<body>
  <header>
    <h1>Main Dashboard</h1>
    <span class="badge {status_class}"><i class="pip"></i>{status}</span>
    <span class="clock">Last refresh: {updated_label}</span>
  </header>
  <main>
    {extra}
    <section class="panel">
    <h2>CAPITULATION <span>{len(matches)}</span></h2>
    <table class="blotter">
      <thead>
        <tr>
          <th>Symbol</th><th>2x ETF</th><th>Day</th><th>Week</th><th>Avg volume</th><th>Volume</th><th>Market cap</th><th>Short float</th><th>Avg range</th><th>Timeframe</th><th>Last</th><th>2-year zone</th><th>2-year vs avg</th><th>3-year zone</th><th>3-year vs avg</th>
        </tr>
      </thead>
      <tbody>
        {rows}
      </tbody>
    </table>
    </section>
    {zone_log}
  </main>
  <script>
    const hiddenKey = "dashboard-hidden-rows";
    function readHidden() {{
      try {{
        const data = JSON.parse(localStorage.getItem(hiddenKey) || "{{}}");
        return data && typeof data === "object" ? data : {{}};
      }} catch (error) {{
        return {{}};
      }}
    }}
    function writeHidden(data) {{
      localStorage.setItem(hiddenKey, JSON.stringify(data));
    }}
    function screenName(section) {{
      const copy = section.querySelector("h2").cloneNode(true);
      copy.querySelectorAll("span, button").forEach((node) => node.remove());
      return copy.textContent.trim();
    }}
    function tickerOf(row) {{
      const cell = row.querySelector(".symbol");
      if (!cell) return "";
      const copy = cell.cloneNode(true);
      copy.querySelectorAll(".new, .hide").forEach((node) => node.remove());
      return copy.textContent.trim();
    }}
    function paint() {{
      const hidden = readHidden();
      document.querySelectorAll("section.panel").forEach((section) => {{
        const name = screenName(section);
        const gone = new Set(hidden[name] || []);
        let count = 0;
        section.querySelectorAll("tbody tr").forEach((row) => {{
          const ticker = tickerOf(row);
          if (!ticker) return;
          let button = row.querySelector(".hide");
          if (!button) {{
            button = document.createElement("button");
            button.type = "button";
            button.className = "hide";
            button.textContent = "×";
            row.querySelector(".symbol").appendChild(button);
          }}
          button.setAttribute("aria-label", "Hide " + ticker);
          const isHidden = gone.has(ticker);
          row.classList.toggle("hidden", isHidden);
          if (isHidden) count += 1;
        }});
        let restore = section.querySelector(".show-hidden");
        if (!restore) {{
          restore = document.createElement("button");
          restore.type = "button";
          restore.className = "show-hidden";
          section.querySelector("h2").appendChild(restore);
        }}
        restore.hidden = count === 0;
        restore.textContent = count === 1 ? "Show 1 hidden" : "Show " + count + " hidden";
      }});
    }}
    document.addEventListener("click", (event) => {{
      const hide = event.target.closest(".hide");
      if (hide) {{
        const section = hide.closest("section.panel");
        const name = screenName(section);
        const ticker = tickerOf(hide.closest("tr"));
        const hidden = readHidden();
        const list = new Set(hidden[name] || []);
        list.add(ticker);
        hidden[name] = Array.from(list);
        writeHidden(hidden);
        paint();
        return;
      }}
      const restore = event.target.closest(".show-hidden");
      if (!restore) return;
      const hidden = readHidden();
      delete hidden[screenName(restore.closest("section.panel"))];
      writeHidden(hidden);
      paint();
    }});
    paint();
    setInterval(() => location.reload(), 60000);
  </script>
</body>
</html>
'''


def load_screener(key: str, folder: str):
    path = SCREENERS / folder / "screener.py"
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def inside_day_rows(as_of: date) -> list[list[tuple[str, str]]]:
    mod = load_screener("inside_day_screener", "inside-day-screener")
    api_key = mod.load_api_key()
    universe = mod.load_symbols()
    sessions = mod.load_sessions(api_key, as_of)
    matches = mod.find_matches(universe, sessions, None)
    screen_day = sessions[0][0]
    two_day_dates = {sessions[0][0], sessions[1][0]}
    three_day_dates = two_day_dates | {sessions[2][0]}
    kept = []
    with s_pool(mod.DETAIL_WORKERS) as pool:
        jobs = {
            pool.submit(mod.volume_profile, api_key, match["symbol"], screen_day, two_day_dates, three_day_dates): match
            for match in matches
        }
        for future in jobs:
            match = jobs[future]
            try:
                average, two_day_top, three_day_top = future.result()
            except Exception:
                continue
            match["average_volume"] = average
            if match["two_day_hit"] and not two_day_top:
                match["two_day_hit"] = False
            if match["three_day_hit"] and not three_day_top:
                match["three_day_hit"] = False
            if match["two_day_hit"] or match["three_day_hit"]:
                kept.append(match)
    details = []
    with s_pool(mod.DETAIL_WORKERS) as pool:
        for row in pool.map(mod.fetch_row, kept):
            if not mod.over_size_limit(row):
                details.append(row)
    details.sort(key=lambda row: mod.sort_percent(row["% Change"]), reverse=True)
    rendered = []
    for row in details:
        rendered.append(quote_row(
            row["Symbol"],
            row["Price"].replace(",", ""),
            row["% Change"],
            row["Volume"],
            row["Finviz Shares Float"],
            row["Finviz Short % of Float"],
        ))
    return rendered


def double_inside_rows(as_of: date) -> list[list[tuple[str, str]]]:
    mod = load_screener("double_inside_day_screener", "double-inside-day-screener")
    api_key = mod.load_api_key()
    universe = mod.load_symbols()
    sessions = mod.load_sessions(api_key, as_of)
    matches = mod.find_matches(universe, sessions, None)
    screen_day = sessions[0][0]
    with s_pool(mod.DETAIL_WORKERS) as pool:
        jobs = {
            pool.submit(mod.average_volume, api_key, match["symbol"], screen_day): match
            for match in matches
        }
        for future in jobs:
            match = jobs[future]
            try:
                match["average_volume"] = future.result()
            except Exception:
                match["average_volume"] = None
    with s_pool(mod.DETAIL_WORKERS) as pool:
        details = list(pool.map(mod.fetch_row, matches))
    details.sort(key=lambda row: mod.sort_percent(row["Up Day %"]), reverse=True)
    rendered = []
    for row in details:
        rendered.append(quote_row(
            row["Symbol"],
            row["Price"].replace(",", ""),
            row["% Change"],
            row["Volume"],
            row["Finviz Shares Float"],
            row["Finviz Short % of Float"],
        ))
    return rendered


def nine_sma_rows(as_of: date) -> list[list[tuple[str, str]]]:
    mod = load_screener("nine_sma_screener", "9sma-screener")
    universe = mod.load_symbols()
    api_key = mod.load_api_key()
    history = mod.load_history(api_key, as_of)
    screen_day = history[-1][0]
    by_symbol: dict[str, list] = {}
    for _, bars in history:
        for symbol, bar in bars.items():
            if symbol in universe:
                by_symbol.setdefault(symbol, []).append(bar)
    session_count = len(history)
    matches = []
    for symbol, bars in by_symbol.items():
        if len(bars) != session_count:
            continue
        match = mod.screen_symbol(bars)
        if match is None:
            continue
        matches.append((symbol, match))
    confirmed = []
    for symbol, match in matches:
        if mod.is_six_month_high(api_key, symbol, screen_day, match["run_high"]):
            confirmed.append((symbol, match))
    confirmed.sort(key=lambda item: item[1]["run_change"], reverse=True)
    pending = [
        {
            "Symbol": symbol,
            "Price": mod.fmt_price(match["price"]),
            "Day": mod.fmt_percent(match["day_change"]),
            "Volume": mod.fmt_volume(match["volume"]),
        }
        for symbol, match in confirmed
    ]
    with s_pool(6) as pool:
        detailed = list(pool.map(mod.add_finviz, pending))
    return [
        quote_row(
            row["Symbol"],
            row["Price"],
            row["Day"],
            row["Volume"],
            row["Finviz Shares Float"],
            row["Finviz Short % of Float"],
        )
        for row in detailed
    ]


def attach_short_float(matches: list[tuple]) -> list[tuple]:
    if not matches:
        return matches
    mod = load_screener("nine_sma_screener", "9sma-screener")

    def one(match: tuple) -> tuple:
        try:
            _shares, short = mod.finviz_float(match[0])
        except Exception:
            short = "—"
        return (*match, short or "—")

    with s_pool(6) as pool:
        return list(pool.map(one, matches))


def s_pool(workers: int):
    from concurrent.futures import ThreadPoolExecutor
    return ThreadPoolExecutor(max_workers=workers)


def safe_screen(label: str, fn, as_of: date):
    try:
        rows = fn(as_of)
        s.log(f"{label}: {len(rows)} names")
        return rows
    except Exception as error:
        s.log(f"{label} failed: {error}")
        return None


PARABOLIC_HEADERS = ["Ticker", "Last", "Day", "Volume", "Float", "Short float", "5 day %"]


def parabolic_rows(as_of: date) -> list[list[tuple[str, str]]]:
    mod = load_screener("parabolic_shorts", "parabolic-shorts")
    universe = mod.load_symbols()
    sessions = mod.load_sessions(mod.load_api_key(), as_of)
    matches = mod.find_matches(universe, sessions)
    previous = sessions[1][1]
    pending = []
    for symbol, close, _prior, change, volume in matches:
        earlier = previous.get(symbol)
        if earlier is not None and earlier[0] > 0:
            day = f"{(close / earlier[0] - 1.0) * 100:+.2f}%"
        else:
            day = "—"
        pending.append({
            "Symbol": symbol,
            "Price": f"{close:.2f}",
            "Day": day,
            "Volume": f"{round(volume):,}",
            "Five": f"{change:+.1f}%",
        })
    nine = load_screener("nine_sma_finviz", "9sma-screener")
    with s_pool(6) as pool:
        detailed = list(pool.map(nine.add_finviz, pending))
    rows = []
    for row in detailed:
        rows.append([
            (row["Symbol"], "symbol"),
            (row["Price"], "num"),
            (row["Day"], f"num {pct_class(row['Day'])}"),
            (row["Volume"], "num"),
            (row.get("Finviz Shares Float") or "—", "num"),
            (row.get("Finviz Short % of Float") or "—", "num"),
            (row["Five"], f"num {pct_class(row['Five'])}"),
        ])
    return rows


def other_sections(as_of: date) -> tuple[str, dict[str, list[str] | None]]:
    inside = safe_screen("Inside day", inside_day_rows, as_of)
    double = safe_screen("Double inside day", double_inside_rows, as_of)
    sma = safe_screen("9 SMA", nine_sma_rows, as_of)
    parabolic = safe_screen("Parabolic shorts", parabolic_rows, as_of)
    sections = {
        "inside": None if inside is None else [row[0][0] for row in inside],
        "double": None if double is None else [row[0][0] for row in double],
        "sma": None if sma is None else [row[0][0] for row in sma],
        "parabolic": None if parabolic is None else [row[0][0] for row in parabolic],
    }
    prior = load_prior()
    page = "\n".join([
        section_table(SCREEN_HEADERS, inside, "INSIDE DAY", fresh_names("inside", sections["inside"], prior)),
        section_table(SCREEN_HEADERS, double, "DOUBLE INSIDE DAY", fresh_names("double", sections["double"], prior)),
        section_table(SCREEN_HEADERS, sma, "9 SMA", fresh_names("sma", sections["sma"], prior)),
        section_table(PARABOLIC_HEADERS, parabolic, "PARABOLIC SHORTS", fresh_names("parabolic", sections["parabolic"], prior)),
    ])
    return page, sections


def refresh(force: bool) -> None:
    now_eastern = datetime.now(s.EASTERN)
    if not force and not session_is_open(now_eastern):
        s.log("Outside the weekday session; the dashboard stays as it is")
        return
    as_of = now_eastern.date()
    api_key = s.load_api_key()
    quotes = live_quotes(api_key, as_of)
    s.log(f"Live trades today: {len(quotes)}")
    if len(quotes) < 100:
        s.log("Live prices were too thin, so the screen uses the latest daily close")
        quotes = None
    hours = s.ExtendedHours(api_key)

    def adjust_bars(symbol: str, dated: list):
        try:
            return apply_extended(hours, symbol, dated, as_of)
        except Exception as error:
            s.log(f"{symbol}: extended hours failed ({error})")
            return dated, None

    last_error = None
    for attempt in range(2):
        try:
            screen_day, base_day, matches, liquid, declined = s.run_screen(as_of, quotes, adjust_bars)
            break
        except Exception as error:
            last_error = error
            s.log(f"Capitulation attempt {attempt + 1} failed: {error}")
            s.time.sleep(8)
    else:
        raise last_error
    in_progress = screen_day == as_of and now_eastern.hour * 60 + now_eastern.minute < EXTENDED_CLOSE
    etfs = leveraged_etfs()
    names = security_names()
    links: dict[str, Path] = {}
    for match in matches:
        symbol = match[0]
        years = 3 if match[5] == "3 year" else 2
        try:
            bars = fetch_dated(api_key, symbol, base_day - timedelta(days=365 * years), screen_day)
            bars = extend_chart_bars(hours, symbol, bars, screen_day, match[1], match[11])
            chart = render_chart(symbol, names.get(symbol, symbol), bars, base_day, screen_day, in_progress, years)
            path = chart_path(symbol)
            write_atomic(path, chart)
            links[symbol] = path
        except Exception as error:
            s.log(f"{symbol}: chart skipped ({error})")
    hours.save()
    matches = attach_short_float(matches)
    matches.sort(key=lambda match: match[13] if match[13] is not None else float("inf"))
    year_volumes = year_average_volumes(api_key, [match[0] for match in matches], screen_day)
    if regular_session(now_eastern):
        extra, sections = other_sections(as_of)
    else:
        extra = previous_other_sections()
        prior_sections = load_prior()
        sections = {key: prior_sections.get(key) for key in ("inside", "double", "sma", "parabolic")}
    prior = load_prior()
    sections["capitulation"] = [match[0] for match in matches]
    updated = datetime.now(CENTRAL)
    zone_hits = record_zone_hits(screen_day, matches, etfs, updated, api_key)
    dashboard = render_dashboard(updated, screen_day, base_day, matches, liquid, declined, in_progress, links, year_volumes, etfs)
    write_atomic(DASHBOARD, dashboard)
    page = render_page(
        updated,
        screen_day,
        base_day,
        matches,
        liquid,
        declined,
        in_progress,
        extra,
        fresh_names("capitulation", sections["capitulation"], prior),
        year_volumes,
        etfs,
        zone_hits,
    )
    write_atomic(PAGE, page)
    save_prior(sections)
    s.log(f"Dashboard updated with {len(matches)} names at {updated.strftime('%-I:%M %p CT')}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh the capitulation dashboard.")
    parser.add_argument("--force", action="store_true", help="Run outside the weekday session window.")
    args = parser.parse_args()
    lock_path = Path("/tmp/capitulation-screener.lock")
    lock_file = lock_path.open("w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        s.log("A refresh is already running")
        return
    try:
        refresh(args.force)
    except Exception as error:
        s.log(f"Dashboard refresh failed: {error}")
        sys.exit(1)
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)


if __name__ == "__main__":
    main()
