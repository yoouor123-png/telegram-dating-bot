"""Daily free-like allowance uses Israel's calendar day, including DST."""
from zoneinfo import ZoneInfo

ISRAEL = ZoneInfo("Asia/Jerusalem")


def count_for_today(last_reset, stored_count, now):
    if last_reset is None or last_reset.astimezone(ISRAEL).date() < now.astimezone(ISRAEL).date():
        return 0
    return stored_count