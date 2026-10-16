#!/usr/bin/env python3
"""Install the dashboard refresh so it follows Eastern time without a login.

Launchd schedules in the Mac's local clock. The generated hours are shifted so
they land on 4:16 a.m. through 8:16 p.m. Eastern. A weekday wake and an awake
hold cover sleep and logout. Re-run after changing the Mac's time zone.
"""

import os
import plistlib
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
LABEL = "com.evanwright.capitulation-screener"
AWAKE_LABEL = "com.evanwright.capitulation-screener-awake"
PYTHON = "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
DASHBOARD = Path("/Users/evanwright/Cursor/screeners/dashboard")
REFRESH = DASHBOARD / "refresh_dashboard.py"
KEEP_AWAKE = DASHBOARD / "keep_awake.py"
LOG = Path("/Users/evanwright/Library/Logs/capitulation-screener.log")
HOME = Path("/Users/evanwright")
RETIRED = HOME / "Library/Application Support/capitulation-screener"
DAEMONS = Path("/Library/LaunchDaemons")
LAUNCH = Path("/Users/evanwright/Cursor/screeners/dashboard/launchd")
USER = "evanwright"
GROUP = "staff"


def local_shift() -> timedelta:
    """How far the Mac clock sits from Eastern right now."""
    now = datetime.now(EASTERN)
    return now.astimezone().utcoffset() - now.utcoffset()


def eastern_slots() -> list[tuple[int, int]]:
    minute = 4 * 60 + 16
    end = 20 * 60 + 16
    slots = []
    while minute <= end:
        slots.append(divmod(minute, 60))
        minute += 15
    return slots


def local_slots(shift: timedelta) -> list[tuple[int, int, int]]:
    """Weekday, local hour, local minute for each Eastern refresh."""
    slots = []
    for weekday in range(1, 6):
        for hour, minute in eastern_slots():
            moment = datetime(2026, 1, 4 + weekday, hour, minute, tzinfo=EASTERN)
            local = moment + shift
            slots.append((local.isoweekday(), local.hour, local.minute))
    return slots


def interval(slots: list[tuple[int, int, int]]) -> list[dict]:
    return [
        {"Weekday": weekday, "Hour": hour, "Minute": minute}
        for weekday, hour, minute in slots
    ]


def daemon_plist(label: str, arguments: list[str], slots: list[tuple[int, int, int]], run_at_load: bool) -> dict:
    plist = {
        "Label": label,
        "UserName": USER,
        "GroupName": GROUP,
        "ProgramArguments": arguments,
        "WorkingDirectory": str(DASHBOARD),
        "EnvironmentVariables": {
            "HOME": str(HOME),
            "USER": USER,
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/Library/Frameworks/Python.framework/Versions/3.14/bin",
        },
        "StartCalendarInterval": interval(slots),
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
    }
    if run_at_load:
        plist["RunAtLoad"] = True
    return plist


def wake_local(shift: timedelta) -> str:
    """Two minutes before the first refresh, in the Mac's clock, HH:MM:SS."""
    first = datetime(2026, 1, 6, 4, 16, tzinfo=EASTERN) + shift - timedelta(minutes=2)
    return first.strftime("%H:%M:%S")


def awake_slot(shift: timedelta) -> list[tuple[int, int, int]]:
    """One minute before the first refresh, so the awake hold is already up."""
    moment = datetime(2026, 1, 6, 4, 16, tzinfo=EASTERN) + shift - timedelta(minutes=1)
    return [(weekday, moment.hour, moment.minute) for weekday in range(1, 6)]


def write_plists(shift: timedelta) -> tuple[Path, Path]:
    LAUNCH.mkdir(parents=True, exist_ok=True)
    refresh_path = LAUNCH / f"{LABEL}.plist"
    awake_path = LAUNCH / f"{AWAKE_LABEL}.plist"
    refresh_path.write_bytes(plistlib.dumps(daemon_plist(
        LABEL,
        [PYTHON, str(REFRESH)],
        local_slots(shift),
        False,
    ), sort_keys=False))
    awake_path.write_bytes(plistlib.dumps(daemon_plist(
        AWAKE_LABEL,
        [PYTHON, str(KEEP_AWAKE)],
        awake_slot(shift),
        True,
    ), sort_keys=False))
    return refresh_path, awake_path


def retire_user_agent() -> None:
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], check=False)
    agent = HOME / "Library/LaunchAgents" / f"{LABEL}.plist"
    if agent.exists():
        RETIRED.mkdir(parents=True, exist_ok=True)
        target = RETIRED / agent.name
        if target.exists():
            target.unlink()
        shutil.move(agent, target)
        print(f"Retired login agent to {target}")


def install(refresh_path: Path, awake_path: Path, wake: str) -> None:
    if os.geteuid() != 0:
        raise SystemExit("Install needs root. Run with sudo.")
    for path in (refresh_path, awake_path):
        dest = DAEMONS / path.name
        shutil.copy(path, dest)
        os.chown(dest, 0, 0)
        os.chmod(dest, 0o644)
        subprocess.run(["launchctl", "bootout", f"system/{path.stem}"], check=False)
        subprocess.run(["launchctl", "bootstrap", "system", str(dest)], check=True)
        subprocess.run(["launchctl", "enable", f"system/{path.stem}"], check=True)
        print(f"Installed {dest}")
    subprocess.run(["pmset", "repeat", "wakeorpoweron", "MTWRF", wake], check=True)
    print(f"Weekday wake at {wake} local")


def main() -> int:
    shift = local_shift()
    refresh_path, awake_path = write_plists(shift)
    wake = wake_local(shift)
    print(f"Local clock is {int(shift.total_seconds() // 3600)} hours from Eastern")
    print(f"Wake at {wake} local")
    if os.geteuid() != 0:
        retire_user_agent()
    if "--install" in sys.argv:
        install(refresh_path, awake_path, wake)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
