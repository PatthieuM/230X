"""Download Dukascopy EUR/USD tick data (bid/ask, millisecond stamps) for September 2019.

One LZMA-compressed .bi5 file per UTC hour. Dukascopy months are zero-indexed
in the URL (September -> 08). Files are stored untouched under
data/fx/dukascopy/EURUSD/ so the notebook can decode them offline.

Only the hours the analysis uses are fetched: 12:00-21:00 UTC on weekdays,
which covers the 13:30-20:00 UTC session shared with the equities. The
Dukascopy server is slow and often answers 503, hence the retries.

Usage: python src/download_fx.py
"""
from __future__ import annotations

import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

SYMBOL = "EURUSD"
START, END = date(2019, 9, 1), date(2019, 10, 1)  # END exclusive
OUT = Path(__file__).resolve().parents[1] / "data" / "fx" / "dukascopy" / SYMBOL
URL = "https://datafeed.dukascopy.com/datafeed/{sym}/{y:04d}/{m:02d}/{d:02d}/{h:02d}h_ticks.bi5"
HOURS = range(12, 21)
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.dukascopy.com/"}


def fetch(url: str, retries: int = 8) -> bytes | None:
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return b""
            err = f"HTTP {e.code}"
        except Exception as e:  # timeouts, resets
            err = repr(e)
        time.sleep(min(60, 3 * 2**attempt))
    print(f"FAILED {url}: {err}", file=sys.stderr)
    return None


def get(job: tuple[date, int]) -> bool:
    d, h = job
    path = OUT / f"{d:%Y%m%d}_{h:02d}h_ticks.bi5"
    if path.exists() and path.stat().st_size > 0:
        return True
    data = fetch(URL.format(sym=SYMBOL, y=d.year, m=d.month - 1, d=d.day, h=h))
    if not data:
        return False
    path.write_bytes(data)
    print(path.name, len(data), flush=True)
    return True


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    days = [START + timedelta(n) for n in range((END - START).days)]
    jobs = [(d, h) for d in days if d.weekday() < 5 for h in HOURS]
    with ThreadPoolExecutor(4) as pool:
        ok = list(pool.map(get, jobs))
    print(f"done, {len(jobs) - sum(ok)} of {len(jobs)} hours missing")
    return 0 if all(ok) else 1


if __name__ == "__main__":
    sys.exit(main())
