#!/usr/bin/env python3
"""Hold this Mac awake from 4:16 a.m. through 9:00 p.m. Eastern on weekdays.

The last dashboard refresh starts at 8:16 p.m. Eastern. The extra time lets that run finish.
"""

import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
WINDOW_START = 4 * 60
HOLD_UNTIL = 21 * 60


def main() -> int:
    now = datetime.now(EASTERN)
    if now.weekday() >= 5:
        print("Weekend; not holding the Mac awake", file=sys.stderr)
        return 0
    minute = now.hour * 60 + now.minute
    if minute < WINDOW_START or minute >= HOLD_UNTIL:
        print(f"{now.strftime('%-I:%M %p ET')} is outside the awake window", file=sys.stderr)
        return 0
    end = now.replace(hour=21, minute=0, second=0, microsecond=0)
    seconds = max(60, int((end - now).total_seconds()))
    print(f"Holding awake for {seconds // 60} minutes, until 9:00 PM ET", file=sys.stderr)
    return subprocess.run(["/usr/bin/caffeinate", "-is", "-t", str(seconds)]).returncode


if __name__ == "__main__":
    raise SystemExit(main())
