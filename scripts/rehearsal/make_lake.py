"""Synthetic Binance-format archives shaped like U1's exploration segment, built into a
local store, for rehearsing trials end to end before they run on real data.

90 perpetuals from 2019-11 to 2024-01, listed at staggered dates, some delisted in
2022, each with its own bid-ask spread and volume; funding every 8 hours (4 hours for
some contracts from mid 2023), persistent like the real thing, recorded a few
milliseconds after the hour. Made harsh the way real data can be: R59, one of the most
liquid contracts, falls 99 % over 9-12 May 2022 and is delisted after the 12th (like
LUNA); two exchange-wide maintenance hours leave every contract without its last bar
before midnight; single bars go missing here and there. No edge is planted.

Everything here is made up, so its numbers may be read and published. Usage, and how
to run trials against the store: scripts/rehearsal/README.md.
"""

import io
import math
import random
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from sqp.data import build, vision
from sqp.storage import LocalStore

lake = LocalStore(Path(sys.argv[1]))  # an empty directory
UM = "data/futures/um/monthly/klines/"
FR = "data/futures/um/monthly/fundingRate/"
source = vision.Source(
    site="https://site.test/",
    listing="https://list.test/b",
    symbol_sets={},
    datasets=[
        {"id": "um-klines-1h", "kind": "klines", "prefix": UM + "{symbol}/1h/"},
        {"id": "um-funding", "kind": "funding", "prefix": FR + "{symbol}/"},
    ],
)
rng = random.Random(11)
OUTAGES = {datetime(2022, 8, 15, 23, tzinfo=UTC), datetime(2023, 3, 24, 23, tzinfo=UTC)}
H = 3_600_000
months = [(2019, 11), (2019, 12)] + [(y, m) for y in range(2020, 2024) for m in range(1, 13)]
months.append((2024, 1))


def ms(y: int, m: int) -> int:
    return int(datetime(y, m, 1, tzinfo=UTC).timestamp() * 1000)


def zipped(name: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, text)
    return buf.getvalue()


for i in range(90):
    sym = f"R{i:02d}USDT"
    first_month, last_month = i // 3, 34 if i % 11 == 7 else len(months) - 1
    if i == 59:
        last_month = 30  # 2022-05
    half = 0.0001 * (1 + i % 20)
    size = 3e4 * (1 + i % 30)
    mid, x = 100.0, 0.0
    for k in range(first_month, last_month + 1):
        y, m = months[k]
        start = ms(y, m)
        end = ms(y + (m == 12), m % 12 + 1)
        bars, rates = [], ["calc_time,funding_interval_hours,last_funding_rate\n"]
        interval = 4 if (i % 13 == 0 and k >= 44) else 8
        for t in range(start, end, H):
            hour = (t // H) % 24
            when = datetime.fromtimestamp(t / 1000, tz=UTC)
            if i == 59 and when >= datetime(2022, 5, 13, tzinfo=UTC):
                break  # delisted
            crash = i == 59 and datetime(2022, 5, 9, tzinfo=UTC) <= when
            gap = when in OUTAGES or (hour == 23 and rng.random() < 0.004)
            if hour % interval == 0:
                x = 0.85 * x + rng.gauss(0, 0.00015)
                rates.append(f"{t + rng.randint(0, 30)},{interval},{0.0001 + x:.8f}\n")
            o = mid
            mid *= math.exp(rng.gauss(-0.064 if crash else 0, 0.03 if crash else 0.008))
            close = mid * (1 + (half if rng.random() < 0.5 else -half))
            high = max(o, mid) * (1 + half + abs(rng.gauss(0, 0.002)))
            low = min(o, mid) * (1 - half - abs(rng.gauss(0, 0.002)))
            quote = size * (1 + rng.random())
            vol = quote / mid
            if not gap:
                bars.append(f"{t},{o},{high},{low},{close},{vol},{t + H - 1},{quote},1,0,0,0\n")
        tag = f"{y}-{m:02d}"
        lake.put(
            f"{vision.RAW_PREFIX}{UM}{sym}/1h/{sym}-1h-{tag}.zip",
            zipped(f"{sym}-1h-{tag}.csv", "".join(bars)),
        )
        lake.put(
            f"{vision.RAW_PREFIX}{FR}{sym}/{sym}-fundingRate-{tag}.zip",
            zipped(f"{sym}-fundingRate-{tag}.csv", "".join(rates)),
        )
out = build.build(source, lambda: lake, room=10**12)
print({k: (v.parts, v.rows) for k, v in out.items()})
