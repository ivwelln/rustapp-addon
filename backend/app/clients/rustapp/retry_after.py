from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import isfinite


def parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None

    value = value.strip()
    try:
        if value.isascii() and value.isdecimal():
            seconds = float(value)
        else:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            seconds = (retry_at - datetime.now(UTC)).total_seconds()
    except (ValueError, OverflowError):
        return None

    if not isfinite(seconds):
        return None

    return max(0.0, seconds)
