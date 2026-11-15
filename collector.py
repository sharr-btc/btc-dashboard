"""BTC dashboard data collector.
Uses only the Python standard library (no pip install needed).
Writes data.json. Safe to run every hour."""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

OUT = "data.json"
UA = {"User-Agent": "btc-dashboard/1.0 (github actions)"}


def get(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise last


def supply_at(height):
    """Exact BTC issued up to and including block `height` (subsidy only)."""
    remaining = height + 1
    reward = 50 * 100_000_000
    total = 0
    while remaining > 0 and reward > 0:
        n = min(remaining, 210_000)
        total += n * reward
        remaining -= n
        reward //= 2
    return total / 1e8


def fetch_price():
    try:
        d = get("https://api.coingecko.com/api/v3/simple/price"
                "?ids=bitcoin&vs_currencies=usd&include_24hr_change=true")["bitcoin"]
        return {"usd": d["usd"], "change_24h_pct": d.get("usd_24h_change"),
                "source": "CoinGecko"}
    except Exception:  # noqa: BLE001
        d = get("https://mempool.space/api/v1/prices")
        return {"usd": d["USD"], "change_24h_pct": None, "source": "mempool.space"}


def fetch_fng():
    rows = get("https://api.alternative.me/fng/?limit=2")["data"]
    cur = rows[0]
    prev = rows[1] if len(rows) > 1 else None
    return {"value": int(cur["value"]), "label": cur["value_classification"],
            "previous": int(prev["value"]) if prev else None,
            "source": "Alternative.me"}


def fetch_network():
    height = int(get("https://mempool.space/api/blocks/tip/height"))
    adj = get("https://mempool.space/api/v1/difficulty-adjustment")
    hr = get("https://mempool.space/api/v1/mining/hashrate/3d")
    return {"height": height,
            "difficulty": hr.get("currentDifficulty"),
            "hashrate_ehs": hr["currentHashrate"] / 1e18,
            "last_retarget_pct": adj.get("previousRetarget"),
            "next_retarget_est_pct": adj.get("difficultyChange"),
            "blocks_to_retarget": adj.get("remainingBlocks"),
            "source": "mempool.space"}


def fetch_stablecoins():
    rows = get("https://stablecoins.llama.fi/stablecoincharts/all")

    def val(r):
        t = r.get("totalCirculatingUSD") or r.get("totalCirculating") or {}
        return float(t.get("peggedUSD", 0))

    now = val(rows[-1])
    a7, a30 = val(rows[-8]), val(rows[-31])
    return {"total_usd": now,
            "change_7d_usd": now - a7, "change_7d_pct": (now - a7) / a7 * 100,
            "change_30d_usd": now - a30, "change_30d_pct": (now - a30) / a30 * 100,
            "source": "DefiLlama"}


def fng_zone(v):
    if v <= 24:
        return "EXTREME FEAR"
    if v <= 44:
        return "FEAR"
    if v <= 55:
        return "NEUTRAL"
    if v <= 74:
        return "GREED"
    return "EXTREME GREED"


def rules(d):
    """Simple rule-based labels. NOT trading signals."""
    r = {}
    if "fng" in d:
        r["fng_zone"] = fng_zone(d["fng"]["value"])
    if "stablecoins" in d:
        c = d["stablecoins"]["change_30d_pct"]
        r["stablecoin_trend"] = ("CONTRACTING" if c < -1 else
                                 "EXPANDING" if c > 1 else "FLAT")
    if "network" in d and d["network"].get("last_retarget_pct") is not None:
        c = d["network"]["last_retarget_pct"]
        r["difficulty_trend"] = ("EASING" if c < -2 else
                                 "RISING" if c > 2 else "STABLE")
    return r


def main():
    old = {}
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding="utf-8") as f:
                old = json.load(f)
        except Exception:  # noqa: BLE001
            old = {}
    jobs = {"price": fetch_price, "fng": fetch_fng, "network": fetch_network,
            "stablecoins": fetch_stablecoins}
    new, errors = {}, {}
    for key, fn in jobs.items():
        try:
            new[key] = fn()
        except Exception as e:  # noqa: BLE001
            errors[key] = (type(e).__name__ + ": " + str(e))[:200]
            if key in old:
                new[key] = dict(old[key], stale=True)
    net = new.get("network")
    if net and net.get("height"):
        s = supply_at(net["height"])
        new["supply"] = {"circulating": s, "daily_issuance": 450.0,
                         "annual_inflation_pct": 450 * 365 / s * 100,
                         "note": "calculated from block height"}
    new["rules"] = rules(new)
    new["updated_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new["errors"] = errors
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(new, f, ensure_ascii=False, indent=2)
    print("updated", new["updated_utc"], "errors:", errors or "none")
    if len(errors) == len(jobs):
        sys.exit(1)


if __name__ == "__main__":
    main()
