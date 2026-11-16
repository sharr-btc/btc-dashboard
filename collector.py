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


def fred(series, limit=14):
    key = os.environ.get("FRED_API_KEY", "").strip()
    if not key:
        raise RuntimeError("FRED_API_KEY is not set")
    url = ("https://api.stlouisfed.org/fred/series/observations?series_id=%s"
           "&api_key=%s&file_type=json&sort_order=desc&limit=%d" % (series, key, limit))
    rows = get(url)["observations"]
    out = []
    for r in rows:
        try:
            out.append((r["date"], float(r["value"])))
        except ValueError:  # FRED uses "." for missing values
            continue
    return out


def pct(a, b):
    return (a / b - 1) * 100


def yoy(obs):
    d, v = obs[0]
    target = str(int(d[:4]) - 1) + d[4:]
    for d2, v2 in obs:
        if d2 == target:
            return pct(v, v2)
    return None


def fetch_macro():
    up, lo = fred("DFEDTARU", 3), fred("DFEDTARL", 3)
    y10 = fred("DGS10", 5)
    cpi, core = fred("CPIAUCSL", 14), fred("CPILFESL", 14)
    pay, un = fred("PAYEMS", 3), fred("UNRATE", 3)
    fed_hist = fred("DFEDTARU", 220)
    d0 = datetime.strptime(fed_hist[0][0], "%Y-%m-%d")
    fed_ref = None
    for d, v in fed_hist:
        if (d0 - datetime.strptime(d, "%Y-%m-%d")).days >= 180:
            fed_ref = v
            break
    return {"fed_change_6m_pp": (up[0][1] - fed_ref) if fed_ref is not None else None,
            "fed_upper": up[0][1], "fed_lower": lo[0][1], "fed_date": up[0][0],
            "us10y": y10[0][1], "us10y_date": y10[0][0],
            "cpi_mom_pct": pct(cpi[0][1], cpi[1][1]), "cpi_yoy_pct": yoy(cpi),
            "cpi_date": cpi[0][0],
            "core_mom_pct": pct(core[0][1], core[1][1]), "core_yoy_pct": yoy(core),
            "core_date": core[0][0],
            "nfp_change_k": pay[0][1] - pay[1][1], "nfp_date": pay[0][0],
            "unemployment_pct": un[0][1], "unemployment_date": un[0][0],
            "source": "FRED (St. Louis Fed)"}


def day(ts):
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d")


def series_price():
    d = get("https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
            "?vs_currency=usd&days=90&interval=daily")["prices"]
    out = {}
    for t, v in d:
        out[day(t / 1000)] = round(v)
    return sorted([k, v] for k, v in out.items())


def series_fng():
    rows = get("https://api.alternative.me/fng/?limit=90")["data"]
    out = {day(r["timestamp"]): int(r["value"]) for r in rows}
    return sorted([k, v] for k, v in out.items())


def series_hashrate():
    """Daily hash rate estimates are noisy, so return a 7-day moving average."""
    d = get("https://mempool.space/api/v1/mining/hashrate/3m")["hashrates"]
    daily = {}
    for r in d:
        daily[day(r["timestamp"])] = r["avgHashrate"] / 1e18
    days = sorted(daily)
    out = []
    for i in range(6, len(days)):
        window = [daily[k] for k in days[i - 6:i + 1]]
        out.append([days[i], round(sum(window) / 7)])
    return out


def series_stablecoins():
    rows = get("https://stablecoins.llama.fi/stablecoincharts/all")[-91:]
    out = {}
    for r in rows:
        t = r.get("totalCirculatingUSD") or r.get("totalCirculating") or {}
        out[day(r["date"])] = round(float(t.get("peggedUSD", 0)) / 1e9, 1)
    return sorted([k, v] for k, v in out.items())


def series_y10():
    obs = fred("DGS10", 130)
    return sorted([d, v] for d, v in obs)


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


def macro_rule(m):
    """Rule-based macro score from -3 (bearish for BTC) to +3. NOT a trading signal."""
    parts = []
    ch = m.get("fed_change_6m_pp")
    if ch is None:
        parts.append({"name": "Fed 6 လ ပြောင်းလဲမှု", "value": "-", "points": 0,
                      "reason": "ဒေတာ မရ"})
    elif ch > 0.1:
        parts.append({"name": "Fed 6 လ ပြောင်းလဲမှု", "value": "%+.2f pp" % ch, "points": -1,
                      "reason": "အတိုးနှုန်း တိုးနေ (Tightening)"})
    elif ch < -0.1:
        parts.append({"name": "Fed 6 လ ပြောင်းလဲမှု", "value": "%+.2f pp" % ch, "points": 1,
                      "reason": "အတိုးနှုန်း လျှော့နေ (Easing)"})
    else:
        parts.append({"name": "Fed 6 လ ပြောင်းလဲမှု", "value": "%+.2f pp" % ch, "points": 0,
                      "reason": "မပြောင်း (Hold)"})
    y = m["us10y"]
    parts.append({"name": "US 10Y Yield", "value": "%.2f%%" % y,
                  "points": -1 if y >= 5 else 1 if y < 4 else 0,
                  "reason": "5% နှင့်အထက်" if y >= 5 else "4% အောက်" if y < 4 else "4-5% အတွင်း"})
    c = m["cpi_mom_pct"]
    parts.append({"name": "CPI လချင်း", "value": "%+.2f%%" % c,
                  "points": -1 if c >= 0.3 else 1 if c <= 0.1 else 0,
                  "reason": "0.3% နှင့်အထက် (အပူလွန်)" if c >= 0.3 else
                            "0.1% နှင့်အောက် (အေးသွား)" if c <= 0.1 else "0.1-0.3% အတွင်း"})
    score = sum(p["points"] for p in parts)
    label = "BEARISH" if score <= -2 else "BULLISH" if score >= 2 else "MIXED"
    return {"score": score, "label": label, "parts": parts,
            "note": "Rule-based ဖြစ်ပြီး Trading Signal မဟုတ်ပါ"}


def rules(d):
    """Simple rule-based labels. NOT trading signals."""
    r = {}
    if "fng" in d:
        r["fng_zone"] = fng_zone(d["fng"]["value"])
    if "stablecoins" in d:
        c = d["stablecoins"]["change_30d_pct"]
        r["stablecoin_trend"] = ("CONTRACTING" if c < -1 else
                                 "EXPANDING" if c > 1 else "FLAT")
    if "macro" in d:
        y = d["macro"]["us10y"]
        r["us10y_zone"] = "ABOVE 5%" if y >= 5 else "4-5%" if y >= 4 else "BELOW 4%"
        try:
            r["macro"] = macro_rule(d["macro"])
        except Exception:  # noqa: BLE001
            pass
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
            "stablecoins": fetch_stablecoins,
            "macro": fetch_macro}
    new, errors = {}, {}
    for key, fn in jobs.items():
        try:
            new[key] = fn()
        except Exception as e:  # noqa: BLE001
            msg = type(e).__name__ + ": " + str(e)
            secret = os.environ.get("FRED_API_KEY", "").strip()
            if secret:
                msg = msg.replace(secret, "***")
            errors[key] = msg[:200]
            if key in old:
                new[key] = dict(old[key], stale=True)
    new["series"] = {}
    series_jobs = {"price": series_price, "fng": series_fng, "hashrate": series_hashrate,
                   "stablecoins": series_stablecoins, "y10": series_y10}
    for key, fn in series_jobs.items():
        try:
            new["series"][key] = fn()
        except Exception as e:  # noqa: BLE001
            msg = type(e).__name__ + ": " + str(e)
            secret = os.environ.get("FRED_API_KEY", "").strip()
            if secret:
                msg = msg.replace(secret, "***")
            errors["series_" + key] = msg[:200]
            if key in (old.get("series") or {}):
                new["series"][key] = old["series"][key]
    net = new.get("network")
    if net and net.get("height"):
        s = supply_at(net["height"])
        new["supply"] = {"circulating": s, "daily_issuance": 450.0,
                         "annual_inflation_pct": 450 * 365 / s * 100,
                         "note": "calculated from block height"}
    if os.path.exists("manual.json"):
        try:
            with open("manual.json", encoding="utf-8") as f:
                new["manual"] = json.load(f)
        except Exception as e:  # noqa: BLE001
            errors["manual"] = "manual.json: " + str(e)[:150]
    new["rules"] = rules(new)
    new["updated_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new["errors"] = errors
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(new, f, ensure_ascii=False, indent=2)
    print("updated", new["updated_utc"], "errors:", errors or "none")
    if sum(1 for k in jobs if k in errors) == len(jobs):
        sys.exit(1)


if __name__ == "__main__":
    main()
