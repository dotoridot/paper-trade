import json
import socket
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest import mock

import os

os.environ["PAPER_HOME"] = tempfile.mkdtemp(prefix="paper-test-")
import paper  # noqa: E402

R = dict(paper.DEFAULT_RULES)
FX = 1300.0


def q(sym="000660", price=1000.0, open_=True, cur="KRW"):
    return {"sym": sym, "name": "T", "price": price, "change_pct": 0, "open": open_,
            "tradable": True, "currency": cur, "at": ""}


class Engine(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, paper, "START_CASH", paper.START_CASH)
        paper.START_CASH = 100_000_000
        self.s = paper.new_state()

    def buy(self, qty=100, quote=None, limit=None, source="user"):
        quote = quote or q()
        o = paper.submit(self.s, quote["sym"], "buy", qty, limit, source, "", quote, FX, R)
        return o, paper.fill_if_possible(self.s, o, quote, FX, R)

    def test_market_buy_applies_slippage_and_fee(self):
        o, ok = self.buy()
        self.assertTrue(ok)
        self.assertAlmostEqual(o["fill_price"], 1000.5)
        self.assertAlmostEqual(self.s["cash"], paper.START_CASH - 100050 * 1.00015)

    def test_closed_market_stays_open_then_fills_at_tick(self):
        o, ok = self.buy(quote=q(open_=False))
        self.assertFalse(ok)
        self.assertEqual(o["status"], "open")
        self.assertEqual(self.s["cash"], paper.START_CASH)
        with tempfile.TemporaryDirectory() as d:
            paper.EQUITY = paper.Path(d) / "e.csv"
            filled, _ = paper.tick(self.s, R, lambda _: q(price=990), FX, 1)
        self.assertEqual([x["id"] for x in filled], [o["id"]])
        self.assertEqual(self.s["positions"]["000660"]["qty"], 100)

    def test_limit_buy_waits_for_price(self):
        o, ok = self.buy(limit=900)
        self.assertFalse(ok)
        self.assertTrue(paper.fill_if_possible(self.s, o, q(price=899), FX, R))
        self.assertEqual(o["fill_price"], 899)

    def test_sell_charges_tax_and_reports_pnl(self):
        self.buy()
        cash = self.s["cash"]
        o = paper.submit(self.s, "000660", "sell", 100, None, "user", "", q(price=1100), FX, R)
        self.assertTrue(paper.fill_if_possible(self.s, o, q(price=1100), FX, R))
        value = 1100 * 0.9995 * 100
        self.assertAlmostEqual(self.s["cash"], cash + value * (1 - 0.00015 - 0.002))
        self.assertNotIn("000660", self.s["positions"])
        self.assertGreater(self.s["trades"][-1]["pnl"], 0)

    def test_fill_carries_the_plain_reason_to_the_trade(self):
        o = paper.submit(self.s, "000660", "buy", 1, None, "ai", "playbook 1", q(), FX, R)
        o["why"] = "plain reason"
        paper.fill_if_possible(self.s, o, q(), FX, R)
        self.assertEqual(self.s["trades"][-1]["why"], "plain reason")

    def test_cannot_oversell_or_overspend(self):
        with self.assertRaises(SystemExit):
            paper.submit(self.s, "000660", "sell", 1, None, "user", "", q(), FX, R)
        with self.assertRaises(SystemExit):
            self.buy(qty=10**6)

    def test_open_buys_reserve_cash(self):
        self.buy(qty=60000, quote=q(open_=False))
        with self.assertRaises(SystemExit):
            self.buy(qty=60000, quote=q(open_=False))

    def test_ai_position_limit_only_binds_ai(self):
        with self.assertRaises(SystemExit):
            self.buy(qty=31000, source="ai")
        self.assertTrue(self.buy(qty=31000, source="user")[1])

    def test_us_trade_converts_at_fx(self):
        us = q("NVDA.O", 200, cur="USD")
        self.buy(qty=10, quote=us)
        total, rows = paper.equity(self.s, {"NVDA.O": us}, FX)
        self.assertEqual(rows[0]["value"], round(2000 * FX))
        self.assertLess(total, paper.START_CASH)

    def test_stops_cut_half_then_all_once(self):
        self.buy()
        with tempfile.TemporaryDirectory() as d:
            paper.EQUITY = paper.Path(d) / "e.csv"
            paper.tick(self.s, R, lambda _: q(price=890), FX, 1)
            self.assertEqual(self.s["positions"]["000660"]["qty"], 50)
            paper.tick(self.s, R, lambda _: q(price=885), FX, 1)
            self.assertEqual(self.s["positions"]["000660"]["qty"], 50)
            paper.tick(self.s, R, lambda _: q(price=790), FX, 1)
            self.assertNotIn("000660", self.s["positions"])

    def test_stops_wait_for_open_market(self):
        self.buy()
        with tempfile.TemporaryDirectory() as d:
            paper.EQUITY = paper.Path(d) / "e.csv"
            paper.tick(self.s, R, lambda _: q(price=500, open_=False), FX, 1)
        self.assertEqual(self.s["positions"]["000660"]["qty"], 100)

    def test_ledger_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            paper.HOME, paper.STATE = paper.Path(d), paper.Path(d) / "state.json"
            with paper.Ledger() as s:
                s["cash"] = 5
            self.assertEqual(json.loads(paper.STATE.read_text())["cash"], 5)
            with self.assertRaises(ValueError):
                with paper.Ledger() as s:
                    s["cash"] = 9
                    raise ValueError
            self.assertEqual(json.loads(paper.STATE.read_text())["cash"], 5)


class Market(unittest.TestCase):
    def setUp(self):
        self.real = paper.http_json

    def tearDown(self):
        paper.http_json = self.real

    def test_chart_keeps_the_latest_candles_in_order(self):
        rows = [{"localDate": str(d), "openPrice": 1, "highPrice": 2, "lowPrice": 1, "closePrice": 2,
                 "accumulatedTradingVolume": 5} for d in range(1, 6)]
        urls = []
        paper.http_json = lambda u: urls.append(u) or rows
        out = paper.fetch_chart("000660", "week", 2)
        self.assertEqual([c["d"] for c in out], ["4", "5"])
        self.assertIn("/chart/domestic/item/000660/week?", urls[0])

    def test_search_keeps_only_korean_stocks(self):
        paper.http_json = lambda u: {"result": {"items": [
            {"nationCode": "USA", "code": "NVDA", "reutersCode": "NVDA.O", "name": "엔비디아", "typeName": "나스닥"},
            {"nationCode": "KOR", "code": "000660", "reutersCode": "000660", "name": "SK하이닉스", "typeName": "코스피"}]}}
        self.assertEqual([h["sym"] for h in paper.search("x")], ["000660"])

    def test_finance_marks_estimates(self):
        paper.http_json = lambda u: {"financeInfo": {
            "trTitleList": [{"key": "2025", "title": "2025.12.", "isConsensus": "N"}, {"key": "2026", "title": "2026.12.", "isConsensus": "Y"}],
            "rowList": [{"title": "부채비율", "columns": {"2025": {"value": "8.48"}}}]}}
        f = paper.fetch_finance("123456")
        self.assertEqual(f["years"], ["2025.12.", "2026.12. est."])
        self.assertEqual(f["rows"]["부채비율"], ["8.48", None])

    def test_news_flattens_groups_and_limits_size(self):
        paper.http_json = lambda u: [{"items": [{"datetime": "202610071919", "officeName": "A", "title": "T", "body": "B"}]} for _ in range(3)]
        rows = paper.fetch_news("123456", 2)
        self.assertEqual(rows[0], {"date": "20261007", "office": "A", "title": "T", "body": "B"})
        self.assertEqual(len(rows), 2)

    def test_book_parses_comma_numbers(self):
        paper.http_json = lambda u: {"totalSell": "3,860", "totalBuy": "11,560",
                                     "sellInfo": [{"price": "1,395,000", "count": "539", "rate": 78}], "buyInfos": []}
        b = paper.fetch_book("000660")
        self.assertEqual(b["asks"][0], {"price": 1395000, "qty": 539, "rate": 78})
        self.assertEqual(b["total_bid"], 11560)

    def test_view_survives_missing_side_panels(self):
        def fake(u):
            if "askingPrice" in u or "integration" in u:
                raise OSError("down")
            if "chart" in u:
                return []
            return {"datas": [{"stockName": "T", "closePrice": "1", "fluctuationsRatio": "0"}]}
        paper.http_json = fake
        v = paper.view("000660", "day", 5)
        self.assertIsNone(v["book"])
        self.assertEqual(v["infos"], [])

    def test_watch_add_and_remove_without_duplicates(self):
        with tempfile.TemporaryDirectory() as d:
            paper.WATCH = paper.Path(d) / "w.json"
            self.assertEqual(paper.edit_watch("add", "035720")[-1], "035720")
            self.assertEqual(paper.edit_watch("add", "035720").count("035720"), 1)
            self.assertNotIn("000660", paper.edit_watch("rm", "000660"))


class FlakyNetworkTest(unittest.TestCase):
    """DNS failures right after the Mac wakes or switches networks are retried."""

    class Body:
        def read(self):
            return b'{"ok": 1}'

    def run_with(self, responses):
        calls = []

        def fake(req, timeout=None):
            calls.append(req.full_url)
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with mock.patch("urllib.request.urlopen", fake), mock.patch("time.sleep"):
            try:
                return paper.http_json("http://x/"), len(calls)
            except urllib.error.URLError as e:
                return e, len(calls)

    def test_dns_failure_is_retried(self):
        dns = urllib.error.URLError(socket.gaierror(8, "nodename nor servname provided"))
        got, calls = self.run_with([dns, dns, self.Body()])
        self.assertEqual(got, {"ok": 1})
        self.assertEqual(calls, 3)

    def test_persistent_failure_still_raises(self):
        dns = urllib.error.URLError(socket.gaierror(8, "x"))
        got, calls = self.run_with([dns, dns, dns, dns])
        self.assertIsInstance(got, urllib.error.URLError)
        self.assertEqual(calls, 3)

    def test_http_error_is_not_retried(self):
        err = urllib.error.HTTPError("http://x/", 404, "nf", None, None)
        got, calls = self.run_with([err, self.Body()])
        self.assertIsInstance(got, urllib.error.HTTPError)
        self.assertEqual(calls, 1)


class MarketScan(unittest.TestCase):
    def row(self, sym, name="가", kind="stock", value=100_000_000_000, rate="1.00", status="ok"):
        return {"itemCode": sym, "stockName": name, "stockEndType": kind, "fluctuationsRatio": rate,
                "accumulatedTradingValueRaw": str(value), "tradableStatusCode": status}

    def test_only_tradable_common_stocks_remain(self):
        saved, paper.EXCLUDE = paper.EXCLUDE, {"123456"}  # the exclude list from strategy.json
        self.addCleanup(setattr, paper, "EXCLUDE", saved)
        rows = [self.row("000001"), self.row("000002", "가우"), self.row("000003", kind="etf"), self.row("000004", "가스팩1호"),
                self.row("000005", status="stop"), self.row("123456", "excluded")]
        self.assertEqual([x["sym"] for x in paper.active_rows(rows)], ["000001"])

    def test_row_is_reduced_to_what_a_decision_needs(self):
        got = paper.active_rows([self.row("000001", rate="-2.50", value=250_000_000_000)])[0]
        self.assertEqual(got, {"sym": "000001", "name": "가", "change_pct": -2.5, "value_eok": 2500})

    def test_failed_source_is_named_and_the_rest_survive(self):
        def broken():
            raise OSError("down")
        got = paper.scan_parts({"ok": lambda: [1], "news": broken})
        self.assertEqual(got["ok"], [1])
        self.assertEqual(got["news"], [])
        self.assertEqual(got["missing"], ["news"])


ROLES = {"leader": {"max_pct": 40}, "supplier": {"min_pct": 30, "fin": {"years": 2, "margin": 8.0, "debt": 150.0, "quick": 80.0}}}


class Plan(unittest.TestCase):
    """The checks below use a sample strategy.json with two roles and one excluded symbol."""

    def setUp(self):
        self.saved = paper.ROLES, paper.EXCLUDE
        paper.ROLES, paper.EXCLUDE = ROLES, {"123456"}

    def tearDown(self):
        paper.ROLES, paper.EXCLUDE = self.saved

    def plan(self, **over):
        item = {"sym": "000660", "name": "SK하이닉스", "theme": "AI memory", "role": "leader", "target_pct": 15, "why": "reason", "exit": "condition"}
        part = lambda sym, theme, pct: {**item, "sym": sym, "name": sym, "theme": theme, "role": "supplier", "target_pct": pct}
        return {"as_of": "2026-11-02", "market": {"summary": "summary", "leading": ["chips"], "future": ["nuclear"]},
                "short": [item], "medium": [part("000101", "nuclear", 20), part("000102", "shipbuilding", 20), part("000103", "plants", 10)],
                "cash_pct": 35, **over}

    def test_valid_plan_has_no_errors(self):
        self.assertEqual(paper.plan_errors(self.plan(), 20), [])

    def test_total_over_100_is_rejected(self):
        self.assertTrue(any("over 100%" in e for e in paper.plan_errors(self.plan(cash_pct=70), 20)))

    def test_position_over_limit_is_rejected(self):
        p = self.plan()
        p["short"][0]["target_pct"] = 25
        self.assertTrue(any("position limit" in e for e in paper.plan_errors(p, 20)))

    def test_excluded_symbol_is_rejected(self):
        p = self.plan()
        p["short"][0]["sym"] = "123456"
        self.assertTrue(any("excluded" in e for e in paper.plan_errors(p, 20)))

    def test_one_industry_group_is_capped_at_40(self):
        p = self.plan(cash_pct=5)
        p["short"][0].update(target_pct=25, group="chips")
        p["medium"][0].update(target_pct=25, group="chips")
        self.assertTrue(any("group" in e for e in paper.plan_errors(p, 30)))
        p["medium"][0]["group"] = "construction"
        self.assertFalse(any("group" in e for e in paper.plan_errors(p, 30)))

    def test_role_totals_follow_strategy_limits(self):
        self.assertEqual(paper.plan_errors(self.plan(), 30), [])
        p = self.plan(cash_pct=30)
        p["short"][0]["target_pct"] = 41
        p["medium"][1]["target_pct"] = 5
        p["medium"][2]["target_pct"] = 4
        got = paper.plan_errors(p, 50)
        self.assertTrue(any("leader total 41% is over 40%" in e for e in got))
        self.assertTrue(any("supplier total 29% is under 30%" in e for e in got))

    def test_every_item_states_a_known_role(self):
        p = self.plan()
        del p["short"][0]["role"]
        p["medium"][0]["role"] = "other"
        got = paper.plan_errors(p, 30)
        self.assertTrue(any("000660: role is missing" in e for e in got))
        self.assertTrue(any("000101: role must be one of" in e for e in got))

    def test_without_roles_any_role_is_accepted(self):
        paper.ROLES = {}
        p = self.plan()
        del p["short"][0]["role"]
        p["medium"][0]["role"] = "other"
        self.assertEqual(paper.plan_errors(p, 30), [])

    def test_strategy_file_overrides_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            path = paper.Path(d) / "strategy.json"
            self.assertEqual(paper.load_strategy(path)["roles"], {})
            path.write_text(json.dumps({"max_names": 4, "exclude": ["005930"]}))
            got = paper.load_strategy(path)
            self.assertEqual((got["max_names"], got["exclude"], got["min_shares"]), (4, ["005930"], 3))
            path.write_text("{broken")
            with self.assertRaises(SystemExit) as e:
                paper.load_strategy(path)
            self.assertIn("not valid JSON", str(e.exception))

    def test_parts_maker_must_clear_the_financial_bar(self):
        bar = ROLES["supplier"]["fin"]
        fin = lambda op, margin, debt, quick: {"years": ["2023.12.", "2024.12.", "2025.12.", "2026.12. est."],
            "rows": {"영업이익": op, "영업이익률": margin, "부채비율": debt, "당좌비율": quick}}
        steady = fin(["120", "135", "150", "-"], ["12.0", "12.5", "13.1", "-"], ["60", "58", "55", "-"], ["140", "150", "160", "-"])
        self.assertEqual(paper.parts_fin_errors("X", steady, bar), [])
        weak = fin(["-10", "-5", "1,589", "9,999"], ["27", "26", "7.5", "50"], ["80", "90", "160", "-"], ["150", "120", "70", "-"])
        got = " ".join(paper.parts_fin_errors("X", weak, bar))
        for word in ("operating loss", "operating margin 7.5%", "debt ratio 160.0%", "quick ratio 70.0%"):
            self.assertIn(word, got)
        self.assertEqual(paper.parts_fin_errors("X", fin(["1", "2"], ["-"], ["-"], ["-"]), bar), ["X: cannot check the supplier financials"])

    def test_add_buy_guards(self):
        item, pos = {"target_pct": 20}, {"avg": 100.0, "qty": 15}
        self.assertEqual(paper.buy_errors(item, None, 100, 10000, 0.5, "1000"), [])
        self.assertIn("at a loss", paper.buy_errors(item, pos, 99, 10000, 0.5, "1000")[0])
        self.assertIn("+3%", paper.buy_errors(item, pos, 101, 10000, 0.5, "1000")[0])
        self.assertEqual(paper.buy_errors(item, pos, 104, 10000, 0.5, "1000"), [])
        self.assertEqual(paper.buy_errors(item, {"avg": 100.0, "qty": 5}, 101, 10000, 0.5, "1000"), [])

    def test_no_ai_buy_at_all_on_a_dip_day(self):
        # Names bought on a sharp index drop tend to fall further into the close.
        item, pos = {"target_pct": 20}, {"avg": 100.0, "qty": 15}
        self.assertIn("KOSPI", paper.buy_errors(item, None, 100, 10000, -2.0, "1000")[0])
        self.assertIn("KOSPI", paper.buy_errors(item, pos, 110, 10000, -2.5, "1000")[0])
        self.assertEqual(paper.buy_errors(item, None, 100, 10000, -1.9, "1000"), [])

    def test_no_first_buy_the_day_after_a_spike(self):
        # A first buy the day after a jump tends to chase the top.
        item, pos = {"target_pct": 20}, {"avg": 100.0, "qty": 5}
        self.assertIn("jump", paper.buy_errors(item, None, 100, 10000, 0.5, "1000", 14.2)[0])
        self.assertEqual(paper.buy_errors(item, None, 100, 10000, 0.5, "1000", 9.9), [])
        self.assertEqual(paper.buy_errors(item, pos, 104, 10000, 0.5, "1000", 14.2), [])

    def test_chart_facts_use_closed_days_only(self):
        candles = [{"d": f"2026{i:04d}", "c": 100.0, "h": 100.0, "v": 10} for i in range(101, 160)]
        candles += [{"d": "20260160", "c": 120.0, "h": 150.0, "v": 30}, {"d": "20260161", "c": 50.0, "h": 50.0, "v": 1}]
        got = paper.chart_facts(candles, today="20260161")
        self.assertEqual((got["close"], got["last_day_pct"], got["vol_ratio"]), (120.0, 20.0, 3.0))
        self.assertEqual((got["ma20"], got["vs_ma20_pct"]), (101.0, 18.81))
        self.assertEqual((got["high"], got["from_high_pct"]), (150.0, -20.0))
        self.assertIsNone(paper.chart_facts(candles[:10])["ma60"])

    def test_no_ai_buy_in_the_first_half_hour(self):
        # Chasing an opening gap tends to end in a stop-loss the next day.
        item = {"target_pct": 20}
        self.assertIn("09:30", paper.buy_errors(item, None, 100, 10000, 2.7, "0905")[0])
        self.assertEqual(paper.buy_errors(item, None, 100, 10000, 2.7, "0930"), [])

    def test_aggressive_limit_allows_30_and_refuses_31(self):
        limit = paper.DEFAULT_RULES["max_position_pct"]
        ok, over = self.plan(), self.plan()
        ok["short"][0]["target_pct"], over["short"][0]["target_pct"] = 30, 31
        ok["cash_pct"] = over["cash_pct"] = 0
        self.assertFalse(any("position limit" in e for e in paper.plan_errors(ok, limit)))
        self.assertTrue(any("position limit" in e for e in paper.plan_errors(over, limit)))

    def test_same_symbol_cannot_sit_in_both_books(self):
        p = self.plan()
        p["medium"][0]["sym"] = "000660"
        self.assertTrue(any("both short and medium" in e for e in paper.plan_errors(p, 20)))

    def test_missing_exit_condition_is_rejected(self):
        p = self.plan()
        del p["medium"][0]["exit"]
        self.assertTrue(any("exit" in e for e in paper.plan_errors(p, 20)))

    def test_view_adds_current_weight_and_unplanned_holdings(self):
        rows = [{"sym": "000660", "name": "SK하이닉스", "weight": 9.5, "pnl_pct": 1.0}, {"sym": "068270", "name": "셀트리온", "weight": 4.0, "pnl_pct": 1.0}]
        got = paper.plan_view(self.plan(), rows)
        self.assertEqual(got["short"][0]["weight"], 9.5)
        self.assertEqual(got["medium"][0]["weight"], 0)
        self.assertEqual(got["unplanned"], [{"sym": "068270", "name": "셀트리온", "weight": 4.0}])

    def test_view_shows_what_the_buy_guards_let_today_reach(self):
        # Losers and names short of the add-on gain cannot reach their targets, so real cash stays above plan.
        rows = [{"sym": "000660", "name": "a", "weight": 9.5, "pnl_pct": -2.0},
                {"sym": "000101", "name": "b", "weight": 12.0, "pnl_pct": 1.0},
                {"sym": "000102", "name": "c", "weight": 3.0, "pnl_pct": 1.0}]
        got = paper.plan_view(self.plan(), rows)
        loss, short_gain, below_half = got["short"][0], got["medium"][0], got["medium"][1]
        self.assertEqual((loss["reach_pct"], "at a loss" in loss["buy_block"]), (9.5, True))
        self.assertEqual((short_gain["reach_pct"], "+3%" in short_gain["buy_block"]), (12.0, True))
        self.assertEqual((below_half["reach_pct"], below_half["buy_block"]), (20, None))
        self.assertEqual((got["medium"][2]["reach_pct"], got["medium"][2]["buy_block"]), (10, None))
        self.assertEqual(got["cash_reach_pct"], 48.5)


class Feasible(unittest.TestCase):
    def plan(self, target=10):
        return {"short": [{"sym": "000660", "target_pct": target}], "medium": []}

    def quote(self, price, tradable=True):
        return {"000660": {**q(price=price), "tradable": tradable}}

    def test_three_shares_within_target_pass(self):
        self.assertEqual(paper.feasibility_errors(self.plan(), 10_000_000, self.quote(300_000)), [])

    def test_share_price_above_target_is_rejected(self):
        got = paper.feasibility_errors(self.plan(12), 10_000_000, self.quote(1_495_000))
        self.assertTrue(any("3 shares" in e for e in got))

    def test_two_shares_are_not_enough_to_split(self):
        self.assertTrue(paper.feasibility_errors(self.plan(), 10_000_000, self.quote(400_000)))

    def test_suspended_stock_is_rejected(self):
        got = paper.feasibility_errors(self.plan(), 10_000_000, self.quote(1000, tradable=False))
        self.assertTrue(any("not tradable" in e for e in got))


class Finalize(unittest.TestCase):
    def test_checked_draft_becomes_final_and_is_marked_unreviewed(self):
        draft = {"short": [{"sym": "000660", "target_pct": 10}], "medium": [], "cash_pct": 90}
        final = paper.finalize_unreviewed(draft)
        self.assertEqual(final["short"][0]["review"]["signal"], "unreviewed")
        self.assertEqual((final["cash_pct"], final["rejected"]), (90, []))


class StopReview(unittest.TestCase):
    TRADES = [{"id": 1, "at": "2026-11-05T10:20:00+09:00", "sym": "A", "name": "a", "side": "sell", "qty": 2, "price": 100.0, "source": "stop", "pnl": -20},
              {"id": 2, "at": "2026-11-05T10:30:00+09:00", "sym": "A", "name": "a", "side": "sell", "qty": 9, "price": 100.0, "source": "ai", "pnl": 5}]
    CANDLES = {"A": [{"d": d, "c": c} for d, c in [("20261105", 99), ("20261106", 90), ("20261107", 95), ("20261110", 104), ("20261111", 120)]]}

    def test_measures_what_the_stop_saved_after_n_trading_days(self):
        got = paper.stop_review(self.TRADES, self.CANDLES)
        row = got["trades"][0]
        self.assertEqual(len(got["trades"]), 1)  # an AI sale is not a stop-loss
        self.assertEqual(row["after"]["1"], {"close": 90, "pct": -10.0, "saved": 20})
        self.assertEqual(row["after"]["3"], {"close": 104, "pct": 4.0, "saved": -8})
        self.assertIsNone(row["after"]["5"])  # five sessions have not passed yet
        self.assertEqual(row["latest"], {"d": "20261111", "close": 120, "final": True, "saved": -40})
        self.assertEqual(got["total"]["1"], {"n": 1, "saved": 20})
        self.assertEqual(got["total"]["5"], {"n": 0, "saved": 0})

    def test_todays_candle_during_market_hours_is_not_a_close(self):
        got = paper.stop_review(self.TRADES, self.CANDLES, partial_day="20261111")["trades"][0]
        self.assertEqual(got["after"]["3"]["close"], 104)
        self.assertFalse(got["latest"]["final"])
        partial = paper.stop_review(self.TRADES, {"A": self.CANDLES["A"][:2]}, partial_day="20261106")["trades"][0]
        self.assertIsNone(partial["after"]["1"])


class Session(unittest.TestCase):
    """Naver says OPEN through after-market trading; only the regular session may fill orders."""

    def quote_at(self, hhmm, weekday=3):
        raw = {"datas": [{"stockName": "삼성전자", "closePrice": "100", "fluctuationsRatio": "0", "marketStatus": "OPEN",
                          "marketSessionType": "afterMarket", "localTradedAt": "x"}]}
        at = datetime(2026, 11, 9 + weekday, int(hhmm[:2]), int(hhmm[2:]), tzinfo=timezone(timedelta(hours=9)))
        with mock.patch.object(paper, "http_json", return_value=raw), mock.patch.object(paper, "kst_now", return_value=at, create=True):
            return paper.fetch_quote("005930")["open"]

    def test_after_market_is_not_open(self):
        self.assertFalse(self.quote_at("1726"))
        self.assertFalse(self.quote_at("0850"))

    def test_regular_session_is_open(self):
        self.assertTrue(self.quote_at("1000"))

    def test_weekend_is_not_open(self):
        self.assertFalse(self.quote_at("1000", weekday=5))


class Running(unittest.TestCase):
    def test_a_live_job_is_reported_and_a_dead_one_is_not(self):
        with tempfile.TemporaryDirectory() as d:
            f = paper.Path(d) / "ai-running"
            self.assertIsNone(paper.ai_running(f))
            f.write_text(f"{paper.os.getpid()} plan 1794444704")
            self.assertEqual(paper.ai_running(f)["job"], "plan")
            f.write_text("999999999 decide 1794444704")
            self.assertIsNone(paper.ai_running(f))


class Journal(unittest.TestCase):
    def test_newest_first_and_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            path = paper.Path(d) / "journal.jsonl"
            for i in range(4):
                paper.journal_add(path, "hold", f"n{i}")
            self.assertEqual([x["text"] for x in paper.journal_tail(path, 2)], ["n3", "n2"])

    def test_missing_file_is_empty(self):
        self.assertEqual(paper.journal_tail(paper.Path("/nonexistent/j.jsonl"), 3), [])


if __name__ == "__main__":
    unittest.main()
