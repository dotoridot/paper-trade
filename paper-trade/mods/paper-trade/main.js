import { View, div } from "gpui-kit";
import { v_flex, h_flex, InputState, Input, TextView } from "gpui-base";
import { exec, theme, run, appHome } from "herdr";

// Paper trading on Korean stocks. engine/paper.py owns quotes, the ledger and fills; engine/run.sh runs the AI with the claude CLI.
// This mod only shows things and sends commands. Data lives in ~/.paper-trade.
const MOD = "paper-trade"; // Change this if the folder is renamed.
const ENGINE = () => `${appHome()}/mods/${MOD}/engine`;
const REFRESH_MS = 60000;
const DECIDE_MS = 30 * 60000;
const AUTO_KEY = "paper-trade.auto";
const CANDLES = 200;
const VISIBLE = 110;
const CHART_H = 196;
const VOLUME_H = 48;
const FEE_AND_SLIP = 1.00065;
const UP = "#f04452";
const DOWN = "#4a9dff";
const AVERAGES = [[5, "#f5c542"], [20, "#2ec27e"], [60, "#b084f5"]];
// Naver's key-fact labels (lookup keys) and the names shown for them.
// Journal entries written before the English build used Korean action names.
const FAILED = new Set(["failed", "\uc2e4\ud328"]);
const FACTS = [["시총", "Mkt cap"], ["PER", "PER"], ["PBR", "PBR"], ["EPS", "EPS"], ["배당수익률", "Div yield"], ["외인소진율", "Foreign own"]];
const FRAMES = [["day", "D"], ["week", "W"], ["month", "M"]];

const commas = (n, digits = 0) => {
  const [whole, rest] = Math.abs(n).toFixed(digits).split(".");
  return `${n < 0 ? "-" : ""}${whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}${rest ? `.${rest}` : ""}`;
};
const won = n => `₩${commas(n)}`;
const signed = n => `${n > 0 ? "+" : ""}${n.toFixed(2)}%`;
const signedCount = n => `${n > 0 ? "+" : ""}${commas(n)}`;
const compact = n => (n >= 1e9 ? `${(n / 1e9).toFixed(1)}B` : n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}K` : commas(n));
const moving = (rows, period) => rows.map((_, i) => (i + 1 < period ? null
  : rows.slice(i + 1 - period, i + 1).reduce((sum, c) => sum + c.c, 0) / period));

function quantityFor(pct, side, price, available, held) {
  if (side === "sell") return Math.floor((held * pct) / 100);
  return price > 0 ? Math.floor(((available * pct) / 100) / (price * FEE_AND_SLIP)) : 0;
}

// The regular KRX session (weekdays 09:00-15:30 KST). Fills and automatic decisions run only then.
function krMarketHours(now = Date.now()) {
  const k = new Date(now + 9 * 3600000);
  const day = k.getUTCDay();
  const hhmm = k.getUTCHours() * 100 + k.getUTCMinutes();
  return day >= 1 && day <= 5 && hhmm >= 900 && hhmm < 1530;
}

// Same shape as the engine's timestamps (+09:00); shows a start time right after a button press.
function kstIso(now = Date.now()) {
  return `${new Date(now + 9 * 3600000).toISOString().slice(0, 19)}+09:00`;
}

function loadAuto() {
  try { return localStorage.getItem(AUTO_KEY) === "on"; } catch { return false; }
}

export default class PaperTrade extends View {
  init(_props, cx) {
    this.snap = null;
    this.board = null;
    this.view = null;
    this.auto = loadAuto();
    this.lastDecide = 0;
    this.sym = "000660";
    this.tf = "day";
    this.side = "buy";
    this.hits = [];
    this.note = "";
    this.fault = "";
    this.hover = null;
    this.searchInput = InputState.new({ placeholder: "Search KRX stocks (Enter)" });
    this.qtyInput = InputState.new({ placeholder: "Shares" });
    this.limitInput = InputState.new({ placeholder: "Empty = market order" });
    this.searchInput.on("submit", (_e, cx) => this.search(cx));
    this.qtyInput.on("change", (_e, cx) => cx.notify());
    this.limitInput.on("change", (_e, cx) => cx.notify());
    cx.timer.every(REFRESH_MS, cx => this.refresh(cx));
    this.refresh(cx);
  }

  async paper(args) {
    const r = await exec("python3", ["-I", `${ENGINE()}/paper.py`, ...args]);
    if (r.code !== 0) throw new Error((r.stderr || `exit ${r.code}`).trim());
    return JSON.parse(r.stdout);
  }

  // Plans and decisions take minutes, so run.sh runs them in the background; status.running shows progress.
  // run.sh starts `auto` only 25 minutes after the last automatic run, so several open panels never double up.
  startJob(job, cx) {
    cx.spawn(async cx => {
      try {
        const r = await exec("bash", [`${ENGINE()}/run.sh`, "start", job]);
        if (r.code !== 0) throw new Error((r.stderr || `exit ${r.code}`).trim());
        if (JSON.parse(r.stdout).skipped) return; // Another panel just ran an automatic decision.
        if (job !== "auto") this.note = job === "plan" ? "The AI started planning. This can take 10-40 minutes." : "The AI started a decision.";
        this.snap = this.snap && { ...this.snap, running: { job, since: kstIso() } };
      } catch (err) {
        this.note = `Could not start the AI: ${err.message}`;
      }
      cx.notify();
    });
  }

  onboard() {
    run("tab.new", { name: "Paper Trade profile", argv: ["bash", `${ENGINE()}/run.sh`, "onboard"] });
  }

  setAuto(on, cx) {
    this.auto = on;
    try { localStorage.setItem(AUTO_KEY, on ? "on" : "off"); } catch {}
    this.note = on ? "Auto-trade on: the AI decides every 30 minutes during market hours." : "Auto-trade off.";
    cx.notify();
  }

  autoDecide(cx) {
    const s = this.snap;
    if (!this.auto || !s?.plan || s.running || !krMarketHours()) return;
    if (Date.now() - this.lastDecide < DECIDE_MS) return;
    this.lastDecide = Date.now();
    this.startJob("auto", cx);
  }

  refresh(cx) {
    cx.spawn(async cx => {
      try {
        // During market hours, fill open orders and check stop-losses first.
        if (krMarketHours()) await this.paper(["tick"]).catch(() => null);
        const [snap, board] = await Promise.all([this.paper(["status"]), this.paper(["board"])]);
        this.snap = snap;
        this.board = board;
        const owned = snap.positions[0]?.sym ?? snap.orders[0]?.sym;
        if (!this.picked && owned) this.sym = owned;
        this.fault = "";
      } catch (err) {
        this.fault = this.snap ? "Connection dropped; showing the last quotes. Retrying soon." : `Refresh failed: ${err.message}`;
      }
      this.autoDecide(cx);
      cx.notify();
      await this.loadStock(cx);
    });
  }

  async loadStock(cx) {
    const { sym, tf } = this;
    try {
      const view = await this.paper(["view", sym, "--tf", tf, "--count", String(CANDLES)]);
      if (sym === this.sym && tf === this.tf) this.view = view;
    } catch (err) {
      if (!this.fault) this.fault = this.view ? "Could not refresh the chart; showing the last one." : `Chart failed to load: ${err.message}`;
    }
    cx.notify();
  }

  pick(sym, cx) {
    this.sym = sym;
    this.picked = true;
    this.hits = [];
    this.hover = null;
    this.qtyInput.set_value("");
    this.limitInput.set_value("");
    cx.notify();
    cx.spawn(async cx => this.loadStock(cx));
  }

  search(cx) {
    const text = this.searchInput.value().trim();
    if (!text) return;
    cx.spawn(async cx => {
      try {
        this.hits = await this.paper(["search", text]);
        this.note = this.hits.length ? "" : "No Korean stocks match";
      } catch (err) {
        this.note = `Search failed: ${err.message}`;
      }
      cx.notify();
    });
  }

  act(args, done, cx) {
    cx.spawn(async cx => {
      try {
        const r = await this.paper(args);
        this.note = r.filled === undefined ? done : r.filled ? `${done}: filled` : `${done}: placed, fills during market hours`;
      } catch (err) {
        this.note = `Rejected: ${err.message}`;
      }
      cx.notify();
      this.refresh(cx);
    });
  }

  submit(cx) {
    const qty = this.qtyInput.value().trim();
    if (!/^[1-9]\d*$/.test(qty)) {
      this.note = "Enter a number of shares";
      cx.notify();
      return;
    }
    const limit = this.limitInput.value().replace(/,/g, "").trim();
    const name = this.view?.quote.name ?? this.sym;
    this.act([this.side, this.sym, qty, ...(limit ? ["--limit", limit] : []), "--source", "user"],
      `${this.side === "buy" ? "Buy" : "Sell"} ${qty} ${name}`, cx);
  }

  render() {
    const t = theme();
    const tone = n => (n > 0 ? UP : n < 0 ? DOWN : t.muted);
    const label = text => div().text_size(11).text_color(t.muted).whitespace_nowrap().child(text);
    const card = (title, body) => v_flex().p(12).gap(8).rounded(8).bg(t.surface).min_w(0).min_h(0).overflow_hidden()
      .child(div().flex_none().text_size(12).font_semibold().text_color(t.foreground).whitespace_nowrap().child(title)).child(body);
    const button = (id, text, onClick, active = false, color = null) => div().id(id).px(10).py(4).rounded(6).cursor_pointer()
      .flex_none().whitespace_nowrap().text_size(12).font_semibold()
      .bg(active ? (color ?? t.accent) : t.raised).text_color(active ? "#ffffff" : t.foreground)
      .hover(s => s.opacity(0.85)).child(text).on_click((_e, cx) => onClick(cx));
    const field = state => div().h(30).px(8).flex().items_center().rounded(6).border(1).border_color(t.border).bg(t.input ?? t.raised).text_size(12).child(Input.new(state).w_full().text_size(12));

    const s = this.snap;
    if (!s) return v_flex().size_full().bg(t.canvas).items_center().justify_center().text_color(t.muted)
      .child(this.fault || this.note || "Loading quotes...");

    const info = key => this.view?.infos.find(i => i.key === key)?.value ?? "-";
    const held = s.positions.find(p => p.sym === this.sym);
    const watching = this.board?.watch.some(q => q.sym === this.sym) ?? false;

    const invested = s.equity > 0 ? ((s.equity - s.cash) / s.equity) * 100 : 0;
    const job = s.running?.job;
    const hasProfile = (s.profile ?? "") !== "";
    const gated = (id, text, ok, why, onClick, active = false) => ok ? button(id, text, onClick, active)
      : button(id, text, cx => { this.note = why; cx.notify(); }).opacity(0.45);
    const status = job ? `${job === "plan" ? "Planning" : job === "auto" ? "Auto decision" : "Deciding"} · since ${s.running.since.slice(11, 16)}`
      : !hasProfile ? "Set up your profile first: the AI interviews you and writes down how you invest."
      : !s.plan ? "Profile ready. Make a plan and the AI builds a short- and medium-term portfolio."
      : `Plan ${s.plan.as_of?.slice(5, 16).replace("T", " ") ?? ""} · auto-trade ${this.auto ? "on (every 30 min in market hours)" : "off"}`;
    const aiBar = h_flex().flex_none().items_center().justify_between().gap(12).p(12).rounded(8).bg(t.surface)
      .child(v_flex().min_w(0).gap(2)
        .child(h_flex().items_center().gap(8)
          .child(div().text_size(12).font_semibold().text_color(t.foreground).whitespace_nowrap().child("AI trader"))
          .when(job != null, el => el.child(div().text_size(12).text_color(t.working).frames(["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"], 90))))
        .child(div().text_size(12).text_color(job ? t.working : t.muted).truncate().child(status)))
      .child(h_flex().flex_none().gap(4)
        .child(button("onboard", hasProfile ? "Redo profile" : "Set up profile", () => this.onboard(), !hasProfile))
        .child(gated("plan", "Make plan", hasProfile && !job, job ? "The AI is already working." : "Set up your profile first.", cx => this.startJob("plan", cx), hasProfile && !s.plan && !job))
        .child(gated("decide", "Decide now", s.plan && !job, job ? "The AI is already working." : "Make a plan first.", cx => this.startJob("decide", cx)))
        .child(button("auto", this.auto ? "Auto-trade on" : "Auto-trade off", cx => this.setAuto(!this.auto, cx), this.auto)));

    const ticker = h_flex().gap(14).children((this.board?.market ?? []).map(m => h_flex().gap(4).flex_none().text_size(12)
      .child(div().text_color(t.muted).child(m.name))
      .child(div().text_color(t.foreground).child(commas(m.price, 2)))
      .child(div().text_color(tone(m.change_pct)).child(signed(m.change_pct)))));
    const header = h_flex().flex_none().items_center().justify_between().gap(12)
      .child(h_flex().items_center().gap(10)
        .child(div().text_size(16).font_semibold().text_color(t.foreground).whitespace_nowrap().child("Paper Trade"))
        .child(div().text_size(14).font_semibold().text_color(tone(s.return_pct)).whitespace_nowrap().child(`${won(s.equity)} ${signed(s.return_pct)}`))
        .child(div().px(7).py(1).rounded(5).bg(t.raised).text_size(11).text_color(t.muted).whitespace_nowrap().child("Virtual money · no real orders")))
      .child(h_flex().items_center().gap(14).child(ticker)
        .child(div().text_size(11).text_color(t.dim).whitespace_nowrap().child(s.as_of.slice(11, 19)))
        .child(button("refresh", "Refresh", cx => this.refresh(cx))));

    const chartCard = this.chart(t, tone, label, button, field, info, watching);
    const book = this.book(t, card);
    const orderCard = this.orderForm(t, card, button, field, held, s);

    const rows = (items, empty, draw) => (items.length ? items.map(draw) : [div().text_size(12).text_color(t.dim).child(empty)]);
    const holdings = card("Holdings · open orders", v_flex().gap(4).id("holdings").flex_1().min_h(0).overflow_y_scrollbar()
      .children(rows(s.positions, "No holdings yet.", p => h_flex().id(`p-${p.sym}`).justify_between().gap(6).text_size(12).cursor_pointer()
        .hover(x => x.bg(t.hover)).on_click((_e, cx) => this.pick(p.sym, cx))
        .child(div().truncate().text_color(t.foreground).child(`${p.name} × ${p.qty}`))
        .child(div().flex_none().text_color(tone(p.pnl_pct)).child(`${commas(p.price)} ${signed(p.pnl_pct)}`))))
      .children(s.orders.map(o => h_flex().id(`o-${o.id}`).justify_between().gap(6).text_size(12)
        .child(div().truncate().text_color(t.muted).child(`#${o.id} ${o.side === "buy" ? "Buy" : "Sell"} ${o.qty} ${o.name} · ${o.source === "ai" ? "AI" : "you"}`))
        .child(div().id(`cancel-${o.id}`).flex_none().text_color(t.dim).cursor_pointer().hover(x => x.text_color(t.foreground)).child("Cancel")
          .on_click((_e, cx) => this.act(["cancel", String(o.id)], `Order ${o.id} canceled`, cx)))))
      .children(s.trades.slice(0, 3).map(x => div().text_size(11).text_color(t.dim).truncate()
        .child(`${x.at.slice(5, 16).replace("T", " ")} ${x.side === "buy" ? "Bought" : "Sold"} ${x.qty} ${x.name}`)))).flex_1();
    const reasons = [...s.orders.filter(o => o.source !== "user"), ...s.trades.filter(x => x.source !== "user")].slice(0, 5);
    const why = card("Why the AI traded", v_flex().gap(8).id("why").flex_1().min_h(0).overflow_y_scrollbar()
      .children(rows(reasons, "The AI has not traded yet.", x => v_flex().gap(2).text_size(12)
        .child(div().font_semibold().text_color(x.side === "buy" ? UP : DOWN).child(`${x.side === "buy" ? "Buy" : "Sell"} ${x.qty} ${x.name}${x.status === "open" ? " · waiting to fill" : ""}`))
        .child(div().text_color(t.foreground).child(x.why || x.reason))))).flex_1();
    const watch = card("Watchlist", v_flex().gap(4).id("watch").flex_1().min_h(0).overflow_y_scrollbar()
      .children(rows(this.board?.watch ?? [], "Watchlist is empty.", q => h_flex().id(`w-${q.sym}`).justify_between().gap(6).text_size(12).cursor_pointer()
        .hover(x => x.bg(t.hover)).on_click((_e, cx) => this.pick(q.sym, cx))
        .child(div().truncate().text_color(q.sym === this.sym ? t.accent : t.foreground).child(q.name))
        .child(div().flex_none().text_color(tone(q.change_pct)).child(`${commas(q.price)} ${signed(q.change_pct)}`))))).flex_1();
    const profile = card("My investor profile", v_flex().id("profile").flex_1().min_h(0).overflow_y_scrollbar().text_size(12).text_color(t.foreground)
      .child(hasProfile ? TextView.markdown("paper-profile", s.profile) : div().text_color(t.dim).child("No profile yet. Press \"Set up profile\" above."))).flex_1();
    const planRows = (key, items) => v_flex().gap(4).id(`plan-${key}`).flex_1().min_h(0).overflow_y_scrollbar()
      .children(rows(items ?? [], "No plan yet.", i => v_flex().id(`plan-${i.sym}`).gap(1).text_size(12).cursor_pointer()
        .hover(x => x.bg(t.hover)).on_click((_e, cx) => this.pick(i.sym, cx))
        .child(h_flex().justify_between().gap(6)
          .child(div().truncate().text_color(t.foreground).child(i.name))
          .child(div().flex_none().text_color(i.weight > 0 ? t.foreground : t.dim).child(`${i.weight.toFixed(1)} / ${i.target_pct}%`)))
        .child(div().text_size(11).text_color(t.dim).truncate().child(`${i.theme}${i.role ? ` · ${i.role}` : ""}`))));
    const planShort = card("Short term · now / target", planRows("short", s.plan?.short)).flex_1();
    const planMedium = card("Medium term · now / target", planRows("medium", s.plan?.medium)).flex_1();
    const planMarket = card(`Market view · target cash ${s.plan?.cash_pct ?? "-"}%`, v_flex().gap(4).id("plan-market").flex_1().min_h(0).overflow_y_scrollbar().text_size(12)
      .child(div().text_color(t.foreground).child(s.plan?.market?.summary ?? "No plan yet."))
      .children((s.plan?.market?.leading ?? []).map(x => div().text_color(t.muted).child(`Leading · ${x}`)))
      .children((s.plan?.market?.future ?? []).map(x => div().text_color(t.muted).child(`Rising · ${x}`)))).flex_1();
    const journal = card("AI decision log", v_flex().gap(6).id("journal").flex_1().min_h(0).overflow_y_scrollbar()
      .children(rows(s.journal ?? [], "No decisions yet.", j => v_flex().gap(1).text_size(12)
        .child(div().font_semibold().text_color(FAILED.has(j.action) ? t.blocked : t.foreground).child(`${j.at.slice(11, 16)} ${j.action}`))
        .child(div().text_color(t.muted).child(j.text))))).flex_1();
    const flow = (this.view?.flows ?? []).slice(0, 4);
    const facts = card("Investor flows · key facts", v_flex().gap(3).id("facts").flex_1().min_h(0).overflow_y_scrollbar().text_size(11)
      .child(h_flex().text_color(t.dim).child(div().w(52).child("Date")).child(div().flex_1().text_right().child("Retail")).child(div().flex_1().text_right().child("Foreign")).child(div().flex_1().text_right().child("Institution")))
      .children(flow.map(x => h_flex().child(div().w(52).text_color(t.muted).child(`${x.date.slice(4, 6)}.${x.date.slice(6, 8)}`))
        .children([x.person, x.foreign, x.organ].map(n => div().flex_1().text_right().whitespace_nowrap().text_color(tone(n)).child(compact(Math.abs(n)) === commas(Math.abs(n)) ? signedCount(n) : `${n > 0 ? "+" : "-"}${compact(Math.abs(n))}`)))))
      .child(div().h(1).my(3).bg(t.border))
      .child(h_flex().flex_wrap().children(FACTS.map(([k, name]) => h_flex().w("50%").gap(4)
        .child(div().flex_none().text_color(t.muted).child(name)).child(div().truncate().text_color(t.foreground).child(info(k))))))).flex_1();

    const bottom = v_flex().id("paper-body").flex_1().min_h(0).gap(8).overflow_y_scrollbar()
      .child(aiBar)
      .child(h_flex().flex_none().gap(14).text_size(12).text_color(t.muted)
        .child(div().whitespace_nowrap().child(`Start ${won(s.start)}`))
        .child(div().whitespace_nowrap().child(`Buying power ${won(s.available)}`))
        .child(div().whitespace_nowrap().child(`Invested ${invested.toFixed(1)}%`))
        .child(div().whitespace_nowrap().child(`${s.positions.length} holdings · ${s.orders.length} open orders`)))
      .child(h_flex().flex_none().h(200).gap(8).items_stretch().overflow_hidden()
        .child(planMarket.h_full()).child(planShort.h_full()).child(planMedium.h_full()).child(journal.h_full()))
      .child(h_flex().flex_none().gap(8).items_start().child(chartCard.flex_1()).child(book.w(258).flex_none()).child(orderCard.w(236).flex_none()))
      .child(h_flex().flex_none().h(186).gap(8).items_stretch().overflow_hidden()
        .child(why.h_full()).child(holdings.h_full()).child(watch.h_full()).child(profile.h_full()).child(facts.h_full()));
    return v_flex().size_full().bg(t.canvas).p(10).gap(8).font_family("Geist")
      .child(header)
      .when(this.fault !== "", el => el.child(div().flex_none().text_size(12).text_color(t.blocked).child(this.fault)))
      .when(this.note !== "", el => el.child(div().flex_none().text_size(12).text_color(t.blocked).child(this.note)))
      .child(bottom);
  }

  chart(t, tone, label, button, field, info, watching) {
    const v = this.view;
    const tools = h_flex().items_center().justify_between().gap(8)
      .child(h_flex().gap(4).items_center()
        .children(FRAMES.map(([id, text]) => button(`tf-${id}`, text, cx => {
          this.tf = id; this.hover = null; cx.notify(); cx.spawn(async cx => this.loadStock(cx));
        }, this.tf === id)))
        .child(button("star", watching ? "★ Watching" : "☆ Watch", cx => this.act(["watch", watching ? "rm" : "add", this.sym], watching ? "Removed from watchlist" : "Added to watchlist", cx))))
      .child(div().w(220).child(field(this.searchInput)));
    const found = this.hits.length === 0 ? null : h_flex().flex_wrap().gap(4).children(this.hits.map(h =>
      button(`hit-${h.sym}`, `${h.name} ${h.sym}`, cx => this.pick(h.sym, cx))));
    const shell = v_flex().p(12).gap(6).rounded(8).bg(t.surface).min_w(0).overflow_hidden();
    if (!v) return shell.h(CHART_H + VOLUME_H + 104).child(tools).child(div().flex_1().flex().items_center().justify_center().text_color(t.dim).child("Loading chart..."));

    const q = v.quote;
    const title = h_flex().items_center().gap(10)
      .child(div().text_size(15).font_semibold().text_color(t.foreground).whitespace_nowrap().child(q.name))
      .child(div().text_size(11).text_color(t.dim).whitespace_nowrap().child(`${v.sym} · ${q.open ? "market open" : "market closed"}`))
      .child(div().text_size(17).font_semibold().text_color(tone(q.change_pct)).whitespace_nowrap().child(won(q.price)))
      .child(div().text_size(13).text_color(tone(q.change_pct)).whitespace_nowrap().child(signed(q.change_pct)));

    const all = v.candles;
    const candles = all.slice(-VISIBLE);
    const shown = this.hover != null ? candles[this.hover] : candles[candles.length - 1];
    const ohlc = !shown ? null : h_flex().gap(10).text_size(11).text_color(t.muted).whitespace_nowrap().overflow_hidden()
      .child(div().child(`${shown.d.slice(0, 4)}.${shown.d.slice(4, 6)}.${shown.d.slice(6, 8)}`))
      .child(div().child(`O ${commas(shown.o)}`)).child(div().child(`H ${commas(shown.h)}`))
      .child(div().child(`L ${commas(shown.l)}`)).child(div().child(`C ${commas(shown.c)}`))
      .child(div().child(`Vol ${commas(shown.v)}`))
      .children(AVERAGES.map(([p, color]) => div().text_color(color).child(`MA${p}`)));
    if (candles.length === 0) return shell.child(tools).child(title).child(label("No chart data."));

    const high = Math.max(...candles.map(c => c.h));
    const low = Math.min(...candles.map(c => c.l));
    const span = high - low || 1;
    const y = p => ((high - p) / span) * (CHART_H - 1);
    const peak = Math.max(...candles.map(c => c.v)) || 1;
    const start = all.length - candles.length;
    const averages = AVERAGES.map(([period, color]) => [moving(all, period).slice(start), color]);
    const dot = (top, color) => h_flex().absolute().left(0).right(0).top(top).justify_center().child(div().w(2).h(2).rounded(1).bg(color));

    const columns = candles.map((c, i) => {
      const color = c.c >= c.o ? UP : DOWN;
      const top = y(Math.max(c.o, c.c));
      const body = Math.max(1, y(Math.min(c.o, c.c)) - top);
      const column = div().id(`c-${i}`).relative().flex_1().min_w(0).h(CHART_H)
        .when(this.hover === i, el => el.bg(t.hover))
        .on_hover((over, cx) => { this.hover = over ? i : this.hover === i ? null : this.hover; cx.notify(); })
        .child(h_flex().absolute().left(0).right(0).top(y(c.h)).h(Math.max(1, y(c.l) - y(c.h))).justify_center().child(div().w(1).h_full().bg(color)))
        .child(div().absolute().left(1).right(1).top(top).h(body).bg(color));
      return column.children(averages.filter(([values]) => values[i] != null && values[i] >= low && values[i] <= high)
        .map(([values, ma]) => dot(y(values[i]), ma)));
    });
    const volumes = candles.map((c, i) => v_flex().flex_1().min_w(0).h(VOLUME_H).justify_end().when(this.hover === i, el => el.bg(t.hover))
      .child(div().mx(1).h(Math.max(1, (c.v / peak) * (VOLUME_H - 2))).bg(c.c >= c.o ? UP : DOWN).opacity(0.75)));

    const last = candles[candles.length - 1];
    const axis = 70;
    const ticks = [0, 1, 2, 3, 4].map(i => div().absolute().right(0).top(Math.min(CHART_H - 14, (i / 4) * CHART_H)).text_size(10).text_color(t.dim).child(commas(high - (span * i) / 4)));
    const dates = [];
    let seen = "";
    candles.forEach((c, i) => {
      const key = this.tf === "month" ? c.d.slice(0, 4) : c.d.slice(0, 6);
      if (key !== seen && i > 0) dates.push([i, this.tf === "month" ? key : `${c.d.slice(2, 4)}.${c.d.slice(4, 6)}`]);
      seen = key;
    });
    const plot = h_flex().relative().h(CHART_H).child(h_flex().relative().flex_1().min_w(0).h(CHART_H).children(columns)
      .child(div().absolute().left(0).right(0).top(y(last.c)).h(1).bg(t.dim).opacity(0.5)))
      .child(div().relative().flex_none().w(axis).h(CHART_H).children(ticks)
        .child(div().absolute().left(0).right(0).top(Math.max(0, y(last.c) - 8)).h(16).px(4).flex().items_center().justify_end().rounded(3).bg(last.c >= last.o ? UP : DOWN)
          .text_size(10).font_semibold().text_color("#ffffff").child(commas(last.c))));
    const volume = h_flex().h(VOLUME_H).child(h_flex().flex_1().min_w(0).h(VOLUME_H).children(volumes))
      .child(div().flex_none().w(axis).text_right().text_size(10).text_color(t.dim).child(compact(peak)));
    const dateRow = h_flex().h(14).pr(axis).children(candles.map((_, i) => div().relative().flex_1().min_w(0).h(14)
      .children(dates.filter(d => d[0] === i).map(d => div().absolute().left(0).top(0).whitespace_nowrap().text_size(10).text_color(t.dim).child(d[1])))));

    const range = (() => {
      const lo = Number(info("52주 최저").replace(/[^\d.]/g, ""));
      const hi = Number(info("52주 최고").replace(/[^\d.]/g, ""));
      if (!lo || hi <= lo) return null;
      const pct = Math.max(0, Math.min(100, ((q.price - lo) / (hi - lo)) * 100));
      return h_flex().items_center().gap(6).text_size(10).text_color(t.dim)
        .child(div().flex_none().child(`52w ${commas(lo)}`))
        .child(div().relative().flex_1().h(3).rounded(2).bg(t.raised).child(div().absolute().top(-2).left(`${pct}%`).w(7).h(7).rounded(4).bg(t.accent)))
        .child(div().flex_none().child(commas(hi)));
    })();

    return shell.child(tools).when(found != null, el => el.child(found)).child(title).child(ohlc).child(plot).child(volume).child(dateRow).when(range != null, el => el.child(range));
  }

  book(t, card) {
    const book = this.view?.book;
    if (!book) return card("Order book", div().text_size(12).text_color(t.dim).child("Loading order book..."));
    const now = this.view.quote.price;
    const row = (l, ask) => {
      const bar = Math.max(2, (l.rate / 100) * 86);
      const side = active => div().relative().flex_1().h(19).overflow_hidden()
        .when(active, el => el.child(div().absolute().top(2).h(15).w(bar).bg(ask ? DOWN : UP).opacity(0.22).when(ask, x => x.right(0)).when(!ask, x => x.left(0))))
        .when(active, el => el.child(div().absolute().left(4).right(4).top(2).text_size(11).text_color(ask ? DOWN : UP).when(ask, x => x.text_right()).child(commas(l.qty))));
      return h_flex().id(`lv-${l.price}`).cursor_pointer().h(19).hover(x => x.bg(t.hover))
        .on_click((_e, cx) => { this.limitInput.set_value(String(l.price)); cx.notify(); })
        .child(side(ask))
        .child(div().flex_none().w(78).text_center().text_size(12).font_semibold().text_color(l.price === now ? t.accent : t.foreground).child(commas(l.price)))
        .child(side(!ask));
    };
    return card("Order book", v_flex()
      .children(book.asks.map(l => row(l, true)))
      .children(book.bids.map(l => row(l, false)))
      .child(h_flex().justify_between().pt(5).text_size(11).text_color(t.muted)
        .child(div().child(`Asks ${commas(book.total_ask)}`)).child(div().child(`Bids ${commas(book.total_bid)}`))));
  }

  orderForm(t, card, button, field, held, s) {
    const color = this.side === "buy" ? UP : DOWN;
    const limit = Number(this.limitInput.value().replace(/,/g, ""));
    const price = limit || this.view?.quote.price || 0;
    const qty = Number(this.qtyInput.value()) || 0;
    return card("Order", v_flex().gap(7)
      .child(div().text_size(12).text_color(t.muted).truncate().child(`${this.view?.quote.name ?? this.sym} · last ${this.view ? commas(this.view.quote.price) : "-"}`))
      .child(h_flex().gap(4)
        .child(button("side-buy", "Buy", cx => { this.side = "buy"; cx.notify(); }, this.side === "buy", UP).flex_1().text_center())
        .child(button("side-sell", "Sell", cx => { this.side = "sell"; cx.notify(); }, this.side === "sell", DOWN).flex_1().text_center()))
      .child(div().text_size(11).text_color(t.muted).child("Limit price")).child(field(this.limitInput))
      .child(div().text_size(11).text_color(t.muted).child("Shares")).child(field(this.qtyInput))
      .child(h_flex().gap(3).children([10, 25, 50, 100].map(pct => button(`pct-${pct}`, pct === 100 ? "Max" : `${pct}%`, cx => {
        this.qtyInput.set_value(String(quantityFor(pct, this.side, price, s.available, held?.qty ?? 0)));
        cx.notify();
      }).flex_1().px(2).text_center())))
      .child(h_flex().justify_between().text_size(11).text_color(t.muted)
        .child(div().child(this.side === "buy" ? "Buying power" : "Held"))
        .child(div().child(this.side === "buy" ? won(s.available) : `${held?.qty ?? 0} shares`)))
      .child(h_flex().justify_between().text_size(12).text_color(t.foreground)
        .child(div().child("Estimated total")).child(div().font_semibold().child(won(price * qty))))
      .child(button("submit", this.side === "buy" ? "Place buy order" : "Place sell order", cx => this.submit(cx), true, color).text_center().py(7)));
  }
}
