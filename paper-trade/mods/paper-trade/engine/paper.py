#!/usr/bin/env python3
"""Paper trading on live free quotes. Korean stocks only. No broker, no real money.

Data lives in $PAPER_HOME (default ~/.paper-trade). Personal strategy limits are read from
$PAPER_HOME/strategy.json and order rules from $PAPER_HOME/rules.json; both are optional.

  paper.py search hynix
  paper.py quote 000660 NVDA.O
  paper.py buy 000660 3 [--limit 1300000] [--source user|ai] [--reason TEXT]
  paper.py sell 000660 1 [--limit ...]
  paper.py cancel ORDER_ID
  paper.py why ORDER_ID "the reason in plain words"
  paper.py tick
  paper.py status
  paper.py view 000660 [--tf day|week|month] [--count 200]   chart, order book, key facts
  paper.py board                                             indices and watchlist
  paper.py fin 000660                                        annual financials
  paper.py pulse                                             everything one AI decision reads
  paper.py plan [--check] [--draft] [--reach]                show or check the AI plan
  paper.py review final                                      make the checked draft the plan
  paper.py journal ACTION "reason"                           log an AI decision
  paper.py watch add|rm 000660
"""
import argparse
import fcntl
import html
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path(os.environ.get("PAPER_HOME") or Path.home() / ".paper-trade")
STATE = HOME / "state.json"
EQUITY = HOME / "equity.csv"
RULES = HOME / "rules.json"
WATCH = HOME / "watch.json"
DEFAULT_WATCH = ["000660", "005930", "035420"]
PLAN = HOME / "portfolio.json"  # the AI's short- and medium-term portfolio
JOURNAL = HOME / "journal.jsonl"  # one line per AI decision
DRAFT = HOME / "portfolio.draft.json"  # draft before checks
PROFILE = HOME / "investor_profile.md"  # written with the user during onboarding
# Limits on plans and AI buys. strategy.json overrides any of these by name.
DEFAULT_STRATEGY = {
    "min_shares": 3,  # fewest shares that still allow buying in parts and a half stop-loss
    "max_names": 6,  # most names across short and medium term
    "max_group_pct": 40,  # cap on names that depend on the same demand
    "add_min_pnl_pct": 3.0,  # past half the target, add only at this unrealized gain
    "dip_stop_pct": -2.0,  # no AI buys on days KOSPI falls this much
    "spike_pct": 10.0,  # no first buy the day after a jump this big
    "open_calm_hhmm": "0930",  # no AI buys before this time (opening gaps)
    "pulse_min_value_eok": 300,  # drop thinly traded names (100M KRW units) from the movers list
    "exclude": [],  # codes the user never wants bought
    # Weight limits per role. Empty means plan items need no role. Example:
    # {"leader": {"max_pct": 40}, "supplier": {"min_pct": 30, "fin": {"years": 2, "margin": 8, "debt": 150, "quick": 80}}}
    "roles": {},
}


def load_strategy(path):
    s = dict(DEFAULT_STRATEGY)
    if path.exists():
        s.update(read_settings(path))
    return s


def read_settings(path):
    try:
        return json.loads(path.read_text())
    except ValueError as e:
        raise SystemExit(f"{path} is not valid JSON: {e}")


STRATEGY = load_strategy(HOME / "strategy.json")
MIN_SHARES = STRATEGY["min_shares"]
MAX_NAMES = STRATEGY["max_names"]
MAX_GROUP_PCT = STRATEGY["max_group_pct"]
ADD_MIN_PNL_PCT = STRATEGY["add_min_pnl_pct"]
DIP_STOP_PCT = STRATEGY["dip_stop_pct"]
SPIKE_PCT = STRATEGY["spike_pct"]
OPEN_CALM_HHMM = STRATEGY["open_calm_hhmm"]
PULSE_MIN_VALUE_EOK = STRATEGY["pulse_min_value_eok"]
EXCLUDE = set(STRATEGY["exclude"])
ROLES = STRATEGY["roles"]
START_CASH = 10_000_000
KRW_ONLY = 1.0  # Korean stocks only for now; the ledger keeps currencies for a later expansion.
UA = "Mozilla/5.0"
DEFAULT_RULES = {
    "max_position_pct": 30.0,
    "stop_first_pct": -10.0,
    "stop_first_sell_ratio": 0.5,
    "stop_full_pct": -20.0,
    "fee_kr_pct": 0.015,
    "tax_kr_sell_pct": 0.20,
    "fee_us_pct": 0.07,
    "slippage_pct": 0.05,
}


def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for wait in (0.5, 1.5, None):  # ride out DNS failures right after sleep or a network switch
        try:
            return json.loads(urllib.request.urlopen(req, timeout=8).read())
        except urllib.error.HTTPError:
            raise
        except urllib.error.URLError:
            if wait is None:
                raise
            time.sleep(wait)


def num(s):
    return float(str(s).replace(",", ""))


def is_us(sym):
    return "." in sym


KST = timezone(timedelta(hours=9))


def kst_now():
    return datetime.now(KST)


def kr_regular_hours(t):
    """The regular KRX session. Naver reports OPEN during pre- and after-market trading too."""
    return t.weekday() < 5 and "0900" <= t.strftime("%H%M") < "1530"


def fetch_quote(sym):
    kind = "worldstock/stock" if is_us(sym) else "domestic/stock"
    d = http_json(f"https://polling.finance.naver.com/api/realtime/{kind}/{sym}")["datas"][0]
    return {
        "sym": sym, "name": d["stockName"], "price": num(d["closePrice"]),
        "change_pct": num(d["fluctuationsRatio"]), "open": d.get("marketStatus") == "OPEN" and (is_us(sym) or kr_regular_hours(kst_now())),
        "tradable": d.get("tradableStatus", "tradable") == "tradable",
        "currency": "USD" if is_us(sym) else "KRW", "at": d.get("localTradedAt"),
    }


def fetch_kospi():
    d = http_json("https://polling.finance.naver.com/api/realtime/domestic/index/KOSPI")["datas"][0]
    return num(d["closePrice"])


def search(q):
    u = "https://m.stock.naver.com/front-api/search/autoComplete?target=stock&query=" + urllib.parse.quote(q)
    out = []
    for i in http_json(u)["result"]["items"]:
        if i["nationCode"] == "KOR":
            out.append({"sym": i["code"], "name": i["name"], "market": i["typeName"]})
    return out[:8]


def fetch_chart(sym, tf, count):
    days = {"day": 1.6, "week": 8, "month": 32}[tf] * count
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d0000")
    end = datetime.now().strftime("%Y%m%d2359")
    rows = http_json(f"https://api.stock.naver.com/chart/domestic/item/{sym}/{tf}?startDateTime={start}&endDateTime={end}")
    return [{"d": x["localDate"], "o": x["openPrice"], "h": x["highPrice"], "l": x["lowPrice"], "c": x["closePrice"],
             "v": x["accumulatedTradingVolume"]} for x in rows][-count:]


def fetch_book(sym):
    d = http_json(f"https://m.stock.naver.com/api/stock/{sym}/askingPrice")
    side = lambda rows: [{"price": num(x["price"]), "qty": num(x["count"]), "rate": x.get("rate", 0)} for x in rows]
    return {"asks": side(d.get("sellInfo") or []), "bids": side(d.get("buyInfos") or []),
            "total_ask": num(d.get("totalSell") or 0), "total_bid": num(d.get("totalBuy") or 0)}


def fetch_detail(sym):
    d = http_json(f"https://m.stock.naver.com/api/stock/{sym}/integration")
    flows = [{"date": x["bizdate"], "foreign": num(x["foreignerPureBuyQuant"]), "organ": num(x["organPureBuyQuant"]),
              "person": num(x["individualPureBuyQuant"])} for x in (d.get("dealTrendInfos") or [])[:5]]
    return {"infos": [{"key": x["key"], "value": x["value"]} for x in d.get("totalInfos") or []], "flows": flows}


def fetch_finance(sym):
    raw = http_json(f"https://m.stock.naver.com/api/stock/{sym}/finance/annual")
    d = raw["financeInfo"]
    about = " ".join(v for v in (raw.get("corporationSummary") or {}).values() if isinstance(v, str))
    years = [{"key": t["key"], "label": t["title"], "estimate": t.get("isConsensus") == "Y"} for t in d["trTitleList"]]
    rows = {r["title"]: [(r["columns"].get(y["key"]) or {}).get("value") for y in years] for r in d["rowList"]}
    return {"sym": sym, "about": about, "years": [y["label"] + (" est." if y["estimate"] else "") for y in years], "rows": rows}


def fetch_news(sym, size=8):
    groups = http_json(f"https://m.stock.naver.com/api/news/stock/{sym}?pageSize={size}&page=1")
    items = [i for g in groups for i in g.get("items", [])]
    return [{"date": i["datetime"][:8], "office": i.get("officeName", ""), "title": i.get("title", ""),
             "body": i.get("body", "")[:200]} for i in items][:size]


def chart_facts(candles, today=None):
    """Timing facts from closed daily candles; today's intraday candle is not a close."""
    c = [x for x in candles if x["d"] != today]
    if len(c) < 2:
        return None
    close = c[-1]["c"]
    ma = lambda n: round(sum(x["c"] for x in c[-n:]) / n, 2) if len(c) >= n else None
    vs = lambda m: m and round((close / m - 1) * 100, 2)
    prior = [x["v"] for x in c[-21:-1]]
    high = max(x["h"] for x in c)
    ma20, ma60 = ma(20), ma(60)
    return {"d": c[-1]["d"], "close": close, "ma20": ma20, "vs_ma20_pct": vs(ma20), "ma60": ma60, "vs_ma60_pct": vs(ma60),
            "high": high, "from_high_pct": round((close / high - 1) * 100, 2), "days": len(c),
            "last_day_pct": round((close / c[-2]["c"] - 1) * 100, 2),
            "vol_ratio": round(c[-1]["v"] / (sum(prior) / len(prior)), 2) if prior and sum(prior) else None}


def chart_now(sym):
    return chart_facts(fetch_chart(sym, "day", 120), datetime.now().strftime("%Y%m%d"))


def view(sym, tf, count):
    """Optional panels fail alone so a missing order book never blanks the chart."""
    out = {"sym": sym, "tf": tf, "quote": fetch_quote(sym), "candles": fetch_chart(sym, tf, count),
           "book": None, "infos": [], "flows": []}
    try:
        out["book"] = fetch_book(sym)
    except (OSError, ValueError, KeyError):
        pass
    try:
        out.update(fetch_detail(sym))
    except (OSError, ValueError, KeyError):
        pass
    return out


def fetch_index(path, name):
    d = http_json(f"https://polling.finance.naver.com/api/realtime/{path}")["datas"][0]
    return {"name": name, "price": num(d["closePrice"]), "change_pct": num(d["fluctuationsRatio"])}


def watchlist():
    return json.loads(WATCH.read_text()) if WATCH.exists() else list(DEFAULT_WATCH)


def edit_watch(action, sym):
    syms = [x for x in watchlist() if x != sym]
    if action == "add":
        syms.append(sym)
    WATCH.write_text(json.dumps(syms))
    return syms


def board():
    market = []
    for path, name in (("domestic/index/KOSPI", "KOSPI"), ("domestic/index/KOSDAQ", "KOSDAQ"), ("domestic/index/KPI200", "KOSPI 200")):
        try:
            market.append(fetch_index(path, name))
        except (OSError, ValueError, KeyError, IndexError):
            pass
    return {"market": market, "watch": [fetch_quote(x) for x in watchlist()]}


def active_rows(rows):
    """Tradable common stocks only, reduced to what a decision needs."""
    out = []
    for x in rows:
        name = x["stockName"]
        if (x.get("stockEndType") != "stock" or x.get("tradableStatusCode", "ok") != "ok" or x["itemCode"] in EXCLUDE
                or name.endswith(("우", "우B", "우C")) or "스팩" in name):
            continue
        out.append({"sym": x["itemCode"], "name": name, "change_pct": num(x["fluctuationsRatio"]),
                    "value_eok": round(int(x["accumulatedTradingValueRaw"]) / 100_000_000)})
    return out


def scan_parts(sources):
    """A dead source must not hide the others, so each is fetched alone and named when it fails."""
    out, missing = {}, []
    for name, fetch in sources.items():
        try:
            out[name] = fetch()
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            out[name], missing = [], missing + [name]
    out["missing"] = missing
    return out


def stock_list(kind, market="", size=50):
    path = f"{kind}/{market}" if market else kind
    return active_rows(http_json(f"https://m.stock.naver.com/api/stocks/{path}?page=1&pageSize={size}")["stocks"])


def group_list(kind, size=12):
    rows = http_json(f"https://m.stock.naver.com/api/stocks/{kind}?page=1&pageSize={size}")["groups"]
    return [{"no": x["no"], "name": x["name"], "change_pct": num(x["changeRate"]), "rise": x["riseCount"], "fall": x["fallCount"]} for x in rows]


def market_news(size=15):
    rows = http_json(f"https://m.stock.naver.com/api/news/list?category=mainnews&pageSize={size}&page=1")
    return [{"at": x["dt"][:12], "title": html.unescape(x["tit"]), "body": html.unescape(x.get("subcontent", ""))[:120]} for x in rows]


def by_value(rows, count):
    return sorted(rows, key=lambda x: -x["value_eok"])[:count]


def market_scan():
    """The whole market's activity, so candidates never depend on a fixed list."""
    def most_traded():
        return by_value(stock_list("marketValue", "KOSPI", 100) + stock_list("marketValue", "KOSDAQ", 100), 20)

    def jumping():
        rows = stock_list("up", "KOSPI") + stock_list("up", "KOSDAQ")
        return by_value([x for x in rows if x["value_eok"] >= PULSE_MIN_VALUE_EOK], 15)

    return scan_parts({"news": market_news, "themes": lambda: group_list("theme"), "industries": lambda: group_list("industry"),
                       "most_traded": most_traded, "jumping": jumping, "searched": lambda: stock_list("searchTop", size=15)})


def theme_members(no):
    return by_value(stock_list(f"theme/{no}", size=40), 25)


def load_plan():
    return json.loads(PLAN.read_text()) if PLAN.exists() else None


def plan_errors(plan, max_pct):
    errors, seen = [], set()
    if not (plan.get("market") or {}).get("summary"):
        errors.append("market.summary is missing")
    total = plan.get("cash_pct", 0)
    for book in ("short", "medium"):
        for item in plan.get(book, []):
            sym = str(item.get("sym", ""))
            for key in ("name", "theme", "target_pct", "why", "exit"):
                if item.get(key) in (None, ""):
                    errors.append(f"{book} {sym}: {key} is missing")
            if ROLES and item.get("role") in (None, ""):
                errors.append(f"{book} {sym}: role is missing")
            elif ROLES and item["role"] not in ROLES:
                errors.append(f"{book} {sym}: role must be one of {', '.join(ROLES)}")
            if len(sym) != 6:
                errors.append(f"{book} {sym}: not a 6-character stock code")
            if sym in EXCLUDE:
                errors.append(f"{book} {sym}: excluded by the user")
            if sym in seen:
                errors.append(f"{sym}: listed in both short and medium term")
            seen.add(sym)
            target = item.get("target_pct") or 0
            if target > max_pct:
                errors.append(f"{book} {sym}: over the {max_pct}% position limit")
            total += target
    if total > 100.5:
        errors.append(f"target weights total {total}%, over 100%")
    if len(seen) > MAX_NAMES:
        errors.append(f"{len(seen)} names: at most {MAX_NAMES}")
    groups = {}
    for item in plan_items(plan):
        key = item.get("group") or item.get("theme")
        groups[key] = groups.get(key, 0) + (item.get("target_pct") or 0)
    errors += [f"group {k}: {v}% total, over {MAX_GROUP_PCT}%" for k, v in groups.items() if v > MAX_GROUP_PCT]
    for role, limit in ROLES.items():
        total_role = sum(i.get("target_pct") or 0 for i in plan_items(plan) if i.get("role") == role)
        if "max_pct" in limit and total_role > limit["max_pct"]:
            errors.append(f"{role} total {total_role}% is over {limit['max_pct']}%")
        if "min_pct" in limit and total_role < limit["min_pct"]:
            errors.append(f"{role} total {total_role}% is under {limit['min_pct']}%")
    return errors


def parts_fin_errors(sym, fin, bar, role="supplier"):
    """A role with a financial bar must clear it on reported (not estimated) years."""
    def nums(title):
        vals = [v for v, y in zip(fin["rows"].get(title, []), fin["years"]) if not y.endswith("est.")]
        try:
            return [float(str(v).replace(",", "")) for v in vals]
        except ValueError:
            return None
    op, margin, debt, quick = (nums(t) for t in ("영업이익", "영업이익률", "부채비율", "당좌비율"))
    if not (op and margin and debt and quick) or len(op) < bar["years"]:
        return [f"{sym}: cannot check the {role} financials"]
    errors = []
    if any(v <= 0 for v in op[-bar["years"]:]):
        errors.append(f"{sym}: operating loss in the last {bar['years']} years")
    if margin[-1] < bar["margin"]:
        errors.append(f"{sym}: operating margin {margin[-1]}% < {bar['margin']}%")
    if debt[-1] > bar["debt"]:
        errors.append(f"{sym}: debt ratio {debt[-1]}% > {bar['debt']}%")
    if quick[-1] < bar["quick"]:
        errors.append(f"{sym}: quick ratio {quick[-1]}% < {bar['quick']}%")
    return errors


def plan_items(plan):
    return [i for book in ("short", "medium") for i in plan.get(book, [])]


def feasibility_errors(plan, equity, quotes):
    """A target must be buyable with this account at today's price, in whole shares."""
    errors = []
    for item in plan_items(plan):
        q = quotes[item["sym"]]
        if not q["tradable"]:
            errors.append(f"{item['sym']}: not tradable")
        budget = equity * item["target_pct"] / 100
        if budget // q["price"] < MIN_SHARES:
            errors.append(f"{item['sym']}: a {budget:,.0f} KRW target cannot buy {MIN_SHARES} shares at {q['price']:,.0f} KRW")
    return errors


def buy_errors(item, pos, price, total, kospi_pct, hhmm, last_day_pct=None):
    """AI buys: none in the opening half hour or on a dip day; adds only on a real gain."""
    if hhmm < OPEN_CALM_HHMM:
        return [f"the AI does not buy before {OPEN_CALM_HHMM[:2]}:{OPEN_CALM_HHMM[2:]}"]
    if kospi_pct is not None and kospi_pct <= DIP_STOP_PCT:
        return [f"KOSPI {kospi_pct}%: no AI buys on days it falls {DIP_STOP_PCT}% or more"]
    if not pos:
        if last_day_pct is not None and last_day_pct >= SPIKE_PCT:
            return [f"up +{last_day_pct}% last session: no first buy the day after a +{SPIKE_PCT:.0f}% jump"]
        return []
    return hold_errors(item, (price / pos["avg"] - 1) * 100, pos["qty"] * price / total * 100)


def hold_errors(item, pnl, weight):
    """The buy guards that depend on the position itself, not on the clock or the index."""
    if pnl < 0:
        return [f"at a loss ({pnl:.1f}%): the AI does not add to losers"]
    if weight >= item["target_pct"] / 2 and pnl < ADD_MIN_PNL_PCT:
        return [f"past half the target, adds need +{ADD_MIN_PNL_PCT:.0f}% unrealized gain (now {pnl:.1f}%)"]
    return []


STOP_HORIZONS = (1, 3, 5)  # sessions after a stop-loss to compare closes with


def stop_review(trades, candles, partial_day=None):
    """What each stop-loss sale avoided: saved > 0 means the price kept falling after the stop."""
    rows, total = [], {str(n): {"n": 0, "saved": 0} for n in STOP_HORIZONS}
    for t in trades:
        if t["source"] != "stop" or t["side"] != "sell":
            continue
        day = t["at"][:10].replace("-", "")
        later = [c for c in candles.get(t["sym"], []) if c["d"] > day]
        closed = [c for c in later if c["d"] != partial_day]  # today's intraday candle is not a close
        saved = lambda close: round((t["price"] - close) * t["qty"])
        after = {}
        for n in STOP_HORIZONS:
            c = closed[n - 1] if len(closed) >= n else None
            after[str(n)] = c and {"close": c["c"], "pct": round((c["c"] / t["price"] - 1) * 100, 2), "saved": saved(c["c"])}
            if c:
                total[str(n)]["n"] += 1
                total[str(n)]["saved"] += saved(c["c"])
        rows.append({"id": t["id"], "at": t["at"], "sym": t["sym"], "name": t["name"], "qty": t["qty"], "price": t["price"],
                     "pnl": t["pnl"], "after": after, "latest": {"d": later[-1]["d"], "close": later[-1]["c"], "final": later[-1]["d"] != partial_day,
                               "saved": saved(later[-1]["c"])} if later else None})
    return {"trades": rows, "total": total}


def stop_review_now(trades):
    syms = {t["sym"] for t in trades if t["source"] == "stop"}
    partial = None if datetime.now().strftime("%H%M") >= "1530" else datetime.now().strftime("%Y%m%d")
    return stop_review(trades, {sym: fetch_chart(sym, "day", 30) for sym in syms}, partial)


def macro():
    """Rates, FX and the US close the morning plan must state."""
    def last(category, code):
        d = http_json(f"https://m.stock.naver.com/front-api/marketIndex/prices?category={category}&reutersCode={code}&page=1&pageSize=10")["result"][0]
        return {"value": num(d["closePrice"]), "change_pct": num(d["fluctuationsRatio"]), "at": d["localTradedAt"]}

    def index(code):
        d = http_json(f"https://api.stock.naver.com/index/{code}/basic")
        return {"value": num(d["closePrice"]), "change_pct": num(d["fluctuationsRatio"]), "at": d["localTradedAt"]}

    return scan_parts({"usdkrw": lambda: last("exchange", "FX_USDKRW"), "kr3y": lambda: last("bond", "KR3YT%3DRR"),
                       "us10y": lambda: last("bond", "US10YT%3DRR"), "nasdaq": lambda: index(".IXIC"), "sox": lambda: index(".SOX")})


def plan_allows(plan, sym):
    return plan is None or any(i["sym"] == sym for i in plan_items(plan))


def plan_check(plan, r):
    errors = plan_errors(plan, r["max_position_pct"])
    if errors:
        return errors
    with Ledger() as s:
        total = status_json(s, r)["equity"]
    errors = [e for i in plan_items(plan) if (bar := ROLES.get(i.get("role"), {}).get("fin"))
              for e in parts_fin_errors(i["sym"], fetch_finance(i["sym"]), bar, i["role"])]
    return errors + feasibility_errors(plan, total, {i["sym"]: fetch_quote(i["sym"]) for i in plan_items(plan)})


def finalize_unreviewed(draft):
    """The checked draft becomes final, marked so nobody reads it as reviewed by anyone else."""
    keep = lambda items: [{**i, "review": {"signal": "unreviewed", "reason": "no outside review"}} for i in items]
    return {**draft, "short": keep(draft.get("short", [])), "medium": keep(draft.get("medium", [])), "rejected": [], "as_of": now()}


def plan_view(plan, rows):
    weight = {x["sym"]: x for x in rows}
    planned = {i["sym"] for book in ("short", "medium") for i in plan.get(book, [])}
    view = {k: plan.get(k) for k in ("as_of", "market", "cash_pct", "rejected")}
    def reach(i):
        row = weight.get(i["sym"])
        block = hold_errors(i, row["pnl_pct"], row["weight"]) if row else []
        w = row["weight"] if row else 0
        return {**i, "weight": w, "reach_pct": w if block else max(w, i["target_pct"]), "buy_block": block[0] if block else None}

    for book in ("short", "medium"):
        view[book] = [reach(i) for i in plan.get(book, [])]
    # Cash the engine rules let the account reach today, not the target. Holdings outside the plan count as cash since they will be sold.
    view["cash_reach_pct"] = round(100 - sum(i["reach_pct"] for b in ("short", "medium") for i in view[b]), 1)
    view["unplanned"] = [{"sym": x["sym"], "name": x["name"], "weight": x["weight"]} for x in rows if x["sym"] not in planned]
    return view


def journal_add(path, action, text):
    entry = {"at": now(), "action": action, "text": text}
    with open(path, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def journal_tail(path, count):
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text().splitlines()[-count:]][::-1]


def pulse(s, r):
    """Everything one 30-minute decision reads, in one file."""
    snap = status_json(s, r)
    plan = load_plan()
    wanted = [i["sym"] for book in ("short", "medium") for i in (plan or {}).get(book, [])]
    held = [x["sym"] for x in snap["positions"]]

    def headlines(sym):
        return [{"date": n["date"], "title": n["title"]} for n in fetch_news(sym, 3)]

    return {"as_of": now(), "market_open": fetch_quote("005930")["open"],
            "account": {k: snap[k] for k in ("equity", "cash", "available", "return_pct", "positions", "orders", "rules")},
            "recent_trades": [{k: x[k] for k in ("at", "sym", "name", "side", "qty", "price", "pnl")} for x in snap["trades"][:5]],
            "plan": snap["plan"], "planned_quotes": [fetch_quote(x) for x in wanted if x not in held],
            "held_news": scan_parts({x: lambda x=x: headlines(x) for x in held}),
            "indices": board()["market"], "macro": macro(), "scan": market_scan(), "recent_decisions": journal_tail(JOURNAL, 4)}


def rules():
    r = dict(DEFAULT_RULES)
    if RULES.exists():
        r.update(read_settings(RULES))
    return r


def new_state():
    return {"cash": START_CASH, "start_cash": START_CASH, "created": now(), "next_id": 1,
            "positions": {}, "orders": [], "trades": []}


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Ledger:
    """Exclusive lock across the mod, scheduled AI runs and CLI writers."""

    def __enter__(self):
        HOME.mkdir(parents=True, exist_ok=True)
        self.f = open(HOME / ".lock", "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        self.s = json.loads(STATE.read_text()) if STATE.exists() else new_state()
        return self.s

    def __exit__(self, kind, *_):
        if kind is None:
            tmp = STATE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.s, ensure_ascii=False, indent=1))
            tmp.replace(STATE)
        self.f.close()


def krw(amount, cur, fx):
    return amount if cur == "KRW" else amount * fx


def equity(s, quotes, fx):
    rows, total = [], s["cash"]
    for sym, p in s["positions"].items():
        q = quotes.get(sym)
        px = q["price"] if q else p["avg"]
        val = krw(px * p["qty"], p["currency"], fx)
        total += val
        rows.append({"sym": sym, "name": p["name"], "qty": p["qty"], "avg": p["avg"], "price": px,
                     "currency": p["currency"], "value": round(val), "pnl_pct": round((px / p["avg"] - 1) * 100, 2),
                     "day_pct": q["change_pct"] if q else None, "open": bool(q and q["open"]), "cut": p.get("cut", 0)})
    for r in rows:
        r["weight"] = round(r["value"] / total * 100, 1)
    return total, sorted(rows, key=lambda r: -r["value"])


def reserved(s, fx_price):
    """KRW already promised to open buy orders."""
    total = 0.0
    for o in s["orders"]:
        if o["status"] == "open" and o["side"] == "buy":
            total += fx_price(o) * o["qty"]
    return total


def submit(s, sym, side, qty, limit, source, reason, quote, fx, r):
    if qty <= 0:
        raise SystemExit("quantity must be at least 1")
    if not quote["tradable"]:
        raise SystemExit(f"{quote['name']}: not tradable")
    px = limit or quote["price"]
    cost = krw(px * qty, quote["currency"], fx) * (1 + (r["fee_us_pct"] if is_us(sym) else r["fee_kr_pct"]) / 100)
    if side == "buy":
        promised = reserved(s, lambda o: krw(o["limit"] or o["ref"], o["currency"], fx))
        if cost + promised > s["cash"]:
            raise SystemExit(f"not enough cash: need {cost:,.0f} KRW, have {s['cash'] - promised:,.0f} KRW")
        if source == "ai":
            total, rows = equity(s, {sym: quote}, fx)
            held = next((x["value"] for x in rows if x["sym"] == sym), 0)
            if (held + cost) / total * 100 > r["max_position_pct"]:
                raise SystemExit(f"over the {r['max_position_pct']}% position limit")
    else:
        pending = sum(o["qty"] for o in s["orders"] if o["status"] == "open" and o["side"] == "sell" and o["sym"] == sym)
        if s["positions"].get(sym, {}).get("qty", 0) - pending < qty:
            raise SystemExit("not enough shares held")
    o = {"id": s["next_id"], "sym": sym, "name": quote["name"], "side": side, "qty": qty, "limit": limit,
         "ref": quote["price"], "currency": quote["currency"], "status": "open", "created": now(),
         "source": source, "reason": reason}
    s["next_id"] += 1
    s["orders"].append(o)
    return o


def fill_if_possible(s, o, q, fx, r):
    if not q["open"]:
        return False
    px = q["price"]
    if o["limit"] and ((o["side"] == "buy" and px > o["limit"]) or (o["side"] == "sell" and px < o["limit"])):
        return False
    if not o["limit"]:
        slip = r["slippage_pct"] / 100
        px = px * (1 + slip) if o["side"] == "buy" else px * (1 - slip)
    value = krw(px * o["qty"], o["currency"], fx)
    fee = value * (r["fee_us_pct"] if is_us(o["sym"]) else r["fee_kr_pct"]) / 100
    tax = value * r["tax_kr_sell_pct"] / 100 if o["side"] == "sell" and not is_us(o["sym"]) else 0
    p = s["positions"].get(o["sym"])
    pnl = None
    if o["side"] == "buy":
        if value + fee > s["cash"]:
            o["status"], o["note"] = "rejected", "not enough cash at fill"
            return False
        s["cash"] -= value + fee
        if p:
            p["avg"] = (p["avg"] * p["qty"] + px * o["qty"]) / (p["qty"] + o["qty"])
            p["qty"] += o["qty"]
        else:
            s["positions"][o["sym"]] = {"name": o["name"], "qty": o["qty"], "avg": px,
                                         "currency": o["currency"], "opened": now(), "cut": 0}
    else:
        if not p or p["qty"] < o["qty"]:
            o["status"], o["note"] = "rejected", "not enough shares at fill"
            return False
        s["cash"] += value - fee - tax
        pnl = krw((px - p["avg"]) * o["qty"], o["currency"], fx) - fee - tax
        p["qty"] -= o["qty"]
        if p["qty"] == 0:
            del s["positions"][o["sym"]]
    o["status"], o["filled_at"], o["fill_price"] = "filled", now(), round(px, 4)
    s["trades"].append({"id": o["id"], "at": o["filled_at"], "sym": o["sym"], "name": o["name"], "side": o["side"],
                        "qty": o["qty"], "price": round(px, 4), "currency": o["currency"], "fee": round(fee + tax),
                        "pnl": None if pnl is None else round(pnl), "source": o["source"], "reason": o["reason"],
                        "why": o.get("why", "")})
    return True


def apply_stops(s, quotes, fx, r):
    """Stop-loss rule: cut part at the first loss limit, the rest at the second."""
    made = []
    for sym, p in list(s["positions"].items()):
        q = quotes.get(sym)
        if not q or not q["open"]:
            continue
        if any(o["status"] == "open" and o["sym"] == sym and o["side"] == "sell" for o in s["orders"]):
            continue
        change = (q["price"] / p["avg"] - 1) * 100
        if change <= r["stop_full_pct"]:
            qty, why = p["qty"], f"hit {r['stop_full_pct']}%: full stop-loss"
        elif change <= r["stop_first_pct"] and p.get("cut", 0) == 0:
            qty, why = max(1, int(p["qty"] * r["stop_first_sell_ratio"])), f"hit {r['stop_first_pct']}%: partial stop-loss"
            p["cut"] = 1
        else:
            continue
        made.append(submit(s, sym, "sell", qty, None, "stop", why, q, fx, r))
    return made


def write_equity(total, s, kospi):
    if not EQUITY.exists():
        EQUITY.write_text("at,equity,cash,kospi\n")
    with EQUITY.open("a") as f:
        f.write(f"{now()},{total:.0f},{s['cash']:.0f},{kospi}\n")


def tick(s, r, quote_fn, fx, kospi):
    syms = {o["sym"] for o in s["orders"] if o["status"] == "open"} | set(s["positions"])
    quotes = {x: quote_fn(x) for x in syms}
    apply_stops(s, quotes, fx, r)
    filled = [o for o in list(s["orders"]) if o["status"] == "open" and fill_if_possible(s, o, quotes[o["sym"]], fx, r)]
    total, _ = equity(s, quotes, fx)
    write_equity(total, s, kospi)
    return filled, total


def status_json(s, r):
    syms = set(s["positions"]) | {o["sym"] for o in s["orders"] if o["status"] == "open"}
    quotes = {x: fetch_quote(x) for x in syms}
    fx = KRW_ONLY
    total, rows = equity(s, quotes, fx)
    curve = []
    if EQUITY.exists():
        curve = [float(l.split(",")[1]) for l in EQUITY.read_text().splitlines()[1:]][-60:]
    return {
        "as_of": now(), "equity": round(total), "cash": round(s["cash"]), "start": s["start_cash"],
        "available": round(s["cash"] - reserved(s, lambda o: krw(o["limit"] or o["ref"], o["currency"], fx))),
        "return_pct": round((total / s["start_cash"] - 1) * 100, 2), "positions": rows,
        "orders": [o for o in s["orders"] if o["status"] == "open"], "trades": s["trades"][-10:][::-1],
        "curve": curve,
        "rules": {k: r[k] for k in ("max_position_pct", "stop_first_pct", "stop_full_pct")},
        "plan": plan_view(plan, rows) if (plan := load_plan()) else None, "journal": journal_tail(JOURNAL, 13),
        "profile": PROFILE.read_text() if PROFILE.exists() else "", "running": ai_running(HOME / "ai-running"),
    }


def ai_running(path):
    """The AI job run.sh started, or None once its process is gone (a crash leaves a stale file)."""
    if not path.exists():
        return None
    try:
        pid, job, started = path.read_text().split()[:3]
        os.kill(int(pid), 0)
    except (ValueError, ProcessLookupError, PermissionError):
        return None
    return {"job": job, "since": datetime.fromtimestamp(int(started)).astimezone().isoformat(timespec="seconds")}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("search").add_argument("q")
    sub.add_parser("quote").add_argument("syms", nargs="+")
    for side in ("buy", "sell"):
        o = sub.add_parser(side)
        o.add_argument("sym")
        o.add_argument("qty", type=int)
        o.add_argument("--limit", type=float)
        o.add_argument("--source", default="user", choices=["user", "ai"])
        o.add_argument("--reason", default="")
        o.add_argument("--why", default="", help="the reason in plain words, shown to the user")
    sub.add_parser("cancel").add_argument("id", type=int)
    y = sub.add_parser("why")
    y.add_argument("id", type=int)
    y.add_argument("text")
    sub.add_parser("tick")
    sub.add_parser("status")
    v = sub.add_parser("view")
    v.add_argument("sym")
    v.add_argument("--tf", default="day", choices=["day", "week", "month"])
    v.add_argument("--count", type=int, default=200)
    sub.add_parser("board")
    sub.add_parser("fin").add_argument("sym")
    sub.add_parser("news").add_argument("sym")
    sub.add_parser("scan")
    sub.add_parser("pulse")
    sub.add_parser("stops")
    sub.add_parser("chart").add_argument("syms", nargs="+")
    sub.add_parser("macro")
    sub.add_parser("theme").add_argument("no", type=int)
    plan_cmd = sub.add_parser("plan")
    plan_cmd.add_argument("--check", action="store_true")
    plan_cmd.add_argument("--draft", action="store_true")
    plan_cmd.add_argument("--reach", action="store_true")
    review_cmd = sub.add_parser("review")
    review_cmd.add_argument("step", choices=["final"])
    j = sub.add_parser("journal")
    j.add_argument("action")
    j.add_argument("text")
    w = sub.add_parser("watch")
    w.add_argument("action", choices=["add", "rm"])
    w.add_argument("sym")
    a = p.parse_args()
    if a.cmd in ("buy", "sell") and os.environ.get("PAPER_SOURCE") == "ai":
        a.source = "ai"  # set by run.sh for AI runs, so AI buy limits apply even without --source ai
    HOME.mkdir(parents=True, exist_ok=True)
    r = rules()
    if a.cmd == "search":
        print(json.dumps(search(a.q), ensure_ascii=False))
    elif a.cmd == "chart":
        print(json.dumps({x: chart_now(x) for x in a.syms}, ensure_ascii=False))
    elif a.cmd == "quote":
        print(json.dumps([fetch_quote(x) for x in a.syms], ensure_ascii=False))
    elif a.cmd in ("buy", "sell"):
        if is_us(a.sym):
            raise SystemExit("only Korean stocks can be ordered")
        if a.cmd == "buy" and a.source == "ai" and not plan_allows(load_plan(), a.sym):
            raise SystemExit(f"{a.sym}: the AI can only buy names in the plan")
        q, fx = fetch_quote(a.sym), KRW_ONLY
        with Ledger() as s:
            if a.cmd == "buy" and a.source == "ai" and (plan := load_plan()):
                item = next(i for i in plan_items(plan) if i["sym"] == a.sym)
                kospi = fetch_index("domestic/index/KOSPI", "KOSPI")["change_pct"]
                pos = s["positions"].get(a.sym)
                last_day = None if pos else (chart_now(a.sym) or {}).get("last_day_pct")
                if errs := buy_errors(item, pos, q["price"], equity(s, {a.sym: q}, fx)[0], kospi,
                                      datetime.now().strftime("%H%M"), last_day):
                    raise SystemExit(f"{a.sym}: " + "; ".join(errs))
            o = submit(s, a.sym, a.cmd, a.qty, a.limit, a.source, a.reason, q, fx, r)
            o["why"] = a.why
            filled = fill_if_possible(s, o, q, fx, r)
        print(json.dumps({"order": o, "filled": filled, "market_open": q["open"]}, ensure_ascii=False))
    elif a.cmd == "cancel":
        with Ledger() as s:
            o = next((x for x in s["orders"] if x["id"] == a.id and x["status"] == "open"), None)
            if not o:
                raise SystemExit("not an open order")
            o["status"] = "canceled"
        print(json.dumps(o, ensure_ascii=False))
    elif a.cmd == "why":
        with Ledger() as s:
            found = [x for x in s["orders"] + s["trades"] if x["id"] == a.id]
            if not found:
                raise SystemExit("no such order")
            for x in found:
                x["why"] = a.text
        print(json.dumps({"id": a.id, "why": a.text}, ensure_ascii=False))
    elif a.cmd == "tick":
        if not fetch_quote("005930")["open"]:
            print(json.dumps({"filled": 0, "skipped": "market closed"}, ensure_ascii=False))
            return
        fx, kospi = KRW_ONLY, fetch_kospi()
        with Ledger() as s:
            filled, total = tick(s, r, fetch_quote, fx, kospi)
        print(json.dumps({"filled": len(filled), "equity": round(total)}))
    elif a.cmd == "status":
        with Ledger() as s:
            print(json.dumps(status_json(s, r), ensure_ascii=False))
    elif a.cmd == "view":
        if is_us(a.sym):
            raise SystemExit("only Korean stocks are supported")
        print(json.dumps(view(a.sym, a.tf, a.count), ensure_ascii=False))
    elif a.cmd == "news":
        print(json.dumps(fetch_news(a.sym), ensure_ascii=False))
    elif a.cmd == "scan":
        print(json.dumps(market_scan(), ensure_ascii=False))
    elif a.cmd == "macro":
        print(json.dumps(macro(), ensure_ascii=False))
    elif a.cmd == "stops":
        with Ledger() as s:
            trades = list(s["trades"])  # don't hold the ledger lock while fetching quotes
        print(json.dumps(stop_review_now(trades), ensure_ascii=False, indent=1))
    elif a.cmd == "pulse":
        with Ledger() as s:
            frozen = json.loads(json.dumps(s))  # don't hold the ledger lock while fetching quotes
        print(json.dumps(pulse(frozen, r), ensure_ascii=False, indent=1))  # multi-line so the Read tool can page through it
    elif a.cmd == "theme":
        print(json.dumps(theme_members(a.no), ensure_ascii=False))
    elif a.cmd == "plan":
        path = DRAFT if a.draft else PLAN
        if not path.exists():
            raise SystemExit(f"{path.name} not found")
        plan = json.loads(path.read_text())
        if a.reach:
            with Ledger() as s:
                reach = plan_view(plan, status_json(s, r)["positions"])
            keys = ("sym", "name", "target_pct", "weight", "reach_pct", "buy_block")
            print(json.dumps({"cash_pct": plan.get("cash_pct"), "cash_reach_pct": reach["cash_reach_pct"],
                              "items": [{k: i[k] for k in keys} for i in plan_items(reach)]}, ensure_ascii=False))
        elif not a.check:
            print(json.dumps(plan, ensure_ascii=False))
        else:
            errors = plan_check(plan, r)
            if errors:
                raise SystemExit("\n".join(errors))
            print(json.dumps({"ok": True}))
    elif a.cmd == "review":
        if not DRAFT.exists():
            raise SystemExit("no draft")
        final = finalize_unreviewed(json.loads(DRAFT.read_text()))
        errors = plan_check(final, r)
        if errors:
            raise SystemExit("\n".join(errors))
        PLAN.write_text(json.dumps(final, ensure_ascii=False, indent=1))
        print(json.dumps({"short": len(final["short"]), "medium": len(final["medium"]), "cash_pct": final["cash_pct"]}))
    elif a.cmd == "journal":
        print(json.dumps(journal_add(JOURNAL, a.action, a.text), ensure_ascii=False))
    elif a.cmd == "fin":
        print(json.dumps(fetch_finance(a.sym), ensure_ascii=False))
    elif a.cmd == "board":
        print(json.dumps(board(), ensure_ascii=False))
    elif a.cmd == "watch":
        if is_us(a.sym):
            raise SystemExit("only Korean stocks can be watched")
        print(json.dumps(edit_watch(a.action, a.sym), ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except urllib.error.URLError as e:
        sys.exit(f"quote request failed: {e}")
