# Paper Trade (모의투자)

A Korean-stock paper-trading desk in one Herdr panel. You start with 10,000,000 KRW of virtual money, watch live KOSPI/KOSDAQ quotes, a candle chart and the order book, and place orders that fill against real prices. An AI that you shape interviews you about how you invest, writes your investor profile, builds a short and medium-term portfolio from it, and (if you turn it on) trades the account every 30 minutes during market hours, explaining each trade in plain Korean. No broker, no real money.

![Paper Trade](preview.png)

## What you see

One mod, `paper-trade`, filling a tab or a pane. Everything is in Korean. Colors follow the Korean convention: up is red `#f04452`, down is blue `#4a9dff`. All other colors come from `theme()`.

Top to bottom, 10px padding, 8px gaps, cards are `surface` with 8px radius:

1. **Header row**: "모의투자", the account value and return (red/blue), a "가상 자금 · 실제 주문 아님" pill; on the right the KOSPI, KOSDAQ and KOSPI200 ticker, the quote time and a 새로고침 button.
2. **AI bar** ("AI 운용"): a status line and four buttons: 성향 설정 (or 성향 다시 설정), 계획 세우기, 지금 판단, 자동 판단 켜짐/꺼짐. While the AI works, a braille spinner (`frames`, 90ms) and "계획 작성 중 · 14:02부터" in the `working` color. Buttons that can't run yet are drawn at 45% opacity and explain why when clicked.
3. **Account line**: start money, cash available, stock weight, number of holdings and open orders.
4. **Plan row** (200px high): 시장 판단 (the AI's market summary and target cash), 단기 포트폴리오, 중기 포트폴리오 (each name with current / target weight), AI 판단 기록 (the journal of each 30-minute decision, failures in `blocked`).
5. **Trading row**: the chart card (day/week/month switch, ☆ 관심 toggle, a search box; 110 candles with 5/20/60-day moving average dots, volume bars, price axis, a hover readout, a 52-week range bar), the order book (258px; click a level to use it as the limit price) and the order form (236px; buy/sell, limit, quantity, 10/25/50/최대 buttons, expected amount).
6. **Bottom row** (186px): 왜 샀나요 (the AI's plain-language reason per trade), 보유 · 미체결 (click to chart it, 취소 to cancel), 관심 종목, 내 투자 성향 (the profile as markdown), 투자자 동향 · 투자정보 (5 days of retail/foreign/institution net buying, PER, PBR and so on).

## How it behaves

- Every 60 seconds it reloads the account and the market board. During Korean market hours (weekdays 09:00 to 15:30 KST) it first runs `tick`, which fills open orders at live prices and applies the stop-loss rules.
- 성향 설정 opens a new tab running `claude` with an interview prompt. It asks 7 to 10 questions one at a time (holding period, loss tolerance, max weight per stock, sectors, how to pick stocks, financial bars, roles, excluded stocks, trading pace) and writes `investor_profile.md`, `strategy.json` and `rules.json` in `~/.paper-trade`.
- 계획 세우기 starts `run.sh plan` in the background. Claude reads the profile, scans the whole market through the engine, writes `portfolio.draft.json`, and the engine checks it against `strategy.json` before it becomes the plan. Takes 10 to 40 minutes.
- 지금 판단 starts one decision (`run.sh decide`): hold, buy, sell or note a candidate, always ending with a journal entry. Outside the regular session (weekdays 09:00 to 15:30 KST) it does nothing. Naver reports `marketStatus: OPEN` during after-market trading too, so the engine also checks the clock.
- 자동 판단 is off by default. When on (remembered in `localStorage`), each copy of the panel asks for `run.sh start auto` at most every 30 minutes during market hours; `run.sh` starts a decision only if 25 minutes have passed since the last automatic one (`~/.paper-trade/last-auto`) and otherwise answers `{"skipped": "auto"}`, so several open copies never double up or show a spinner for nothing.
- Only one AI job runs at a time (`~/.paper-trade/ai-running`). `run.sh start` checks for `claude`, the profile or the plan before detaching and reports the reason at once.
- Orders placed by hand are `source: user`. Inside AI runs `run.sh` sets `PAPER_SOURCE=ai`, so every order the AI places is an AI order even without `--source ai`. The engine refuses AI buys in the first 30 minutes, on days KOSPI is down 2% or more, the day after a +10% jump, and while a holding is at a loss; the limits come from `strategy.json`.

## Built from

- `mods/paper-trade/main.js`: one `View`. Uses `exec`, `run("tab.new", { name, argv })`, `appHome`, `theme`, `localStorage`, `cx.timer.every`, `TextView.markdown`, `InputState`/`Input`. `mod.json` is `{ "place": "none" }`; open it with `tab.new { name: "모의투자", mod: "paper-trade" }`.
- `mods/paper-trade/engine/paper.py`: Python 3 standard library only. Commands the panel calls: `status`, `board`, `view SYM --tf day|week|month --count 200`, `search TEXT`, `buy|sell SYM QTY [--limit P] --source user`, `cancel ID`, `watch add|rm SYM`, `tick`. Prints JSON. Quotes, charts, order books, finance, news and themes come from Naver Finance's public JSON endpoints (`polling.finance.naver.com`, `m.stock.naver.com`, `api.stock.naver.com`).
- `mods/paper-trade/engine/run.sh`: `onboard` (interactive claude), `plan`, `decide`, `auto`, and `start plan|decide|auto` (detaches with `nohup bash`, because `exec` has a 30-second limit). Claude runs with `~/.paper-trade` as its working folder and may read only there, edit only its own output file, and run only named engine commands: lookups (`quote`, `fin`, `news`, `theme`, `search`, `chart`, `status`) in every stage, `pulse`, `macro` and `plan` while planning, and `buy`, `sell`, `journal` only while deciding.
- `mods/paper-trade/engine/prompts/`: `onboard.md`, `plan.md`, `decide.md`, with `{{ENGINE}}` and `{{HOME}}` filled in by `run.sh`.
- `mods/paper-trade/engine/test_paper.py`: 58 unit tests, `python3 -m unittest test_paper` from the engine folder.

## Watch out

- The panel runs programs: `python3` (the engine) and `bash run.sh`, which runs the `claude` CLI and uses the person's Claude plan. It reaches the network (Naver Finance). It reads and writes only `~/.paper-trade` (or `$PAPER_HOME`).
- Naver Finance endpoints are unofficial and can change or block requests without notice. When one fails the panel keeps the last data and says so.
- Korean stocks only. Prices are KRW; fees (0.015%), sell tax (0.20%) and market-order slippage (0.05%) are simulated.
- If the folder is renamed, change `MOD` at the top of `main.js`, since the engine path is `appHome()/mods/<MOD>/engine`.
- `exec` gives 30 seconds; anything longer must go through `run.sh start`.
- The engine never writes inside the mod folder, so saving data doesn't hot-reload the panel.
- Needs `python3` (standard library only), `bash` and, for the AI parts, the `claude` CLI signed in. Without `claude`, manual trading still works.

## Done when

- `herdr-app check ~/.config/herdr-app/mods/paper-trade` passes.
- `python3 -m unittest test_paper` in `engine/` passes.
- The panel shows a 10,000,000원 account, the index ticker and a chart for SK하이닉스 (000660) on first open.
- Searching "삼성" lists 삼성전자; buying 1 share during market hours fills and appears under 보유 · 미체결.
- 성향 설정 opens a tab with claude asking the first question; after it finishes, 내 투자 성향 shows the profile and 계획 세우기 becomes active.
- 계획 세우기 shows the spinner, and later the plan row fills with names and target weights.
