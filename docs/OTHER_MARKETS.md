# Paper trading another market

Paper Trade is built for the Korean market: KOSPI and KOSDAQ stocks, prices in KRW, the regular KRX session (weekdays 09:00 to 15:30 KST), and quotes from Naver Finance. This guide is for adapting it to the US market or any other country. It is written so you can hand it to your coding agent:

```
Read docs/OTHER_MARKETS.md in the paper-trade mod and adapt it to the US market using <data source>.
```

The engine, the AI loop, the profile interview and the panel layout stay the same. What changes is where prices come from, the trading calendar, money and costs, and a few Korea-only rules and labels.

## 1. Pick a data source

The panel and the AI need: a live quote, daily/weekly/monthly candles, symbol search, one or more benchmark indices, and ideally an order book, key facts, news and a list of the day's most active stocks. Few free sources cover all of these, so decide what you can live without (the panel already shows "Loading order book..." or empty cards when a source has nothing).

| Kind | Examples | Trade-off |
| --- | --- | --- |
| No key, unofficial | Naver Finance's world-stock pages (the engine already reads `worldstock/stock/<ticker>.O` quotes), Yahoo Finance's public chart endpoints | Works out of the box like today's Korean build, but can change or be blocked without notice. Read the site's terms before publishing anything that depends on it. |
| Free key | [Alpaca](https://docs.alpaca.markets/us/docs/about-market-data-api) (its free plan covers paper accounts with IEX-only real-time data), [Finnhub](https://finnhub.io/pricing) (free tier rate-limited per minute), Alpha Vantage, Massive (formerly Polygon.io) | Stable and documented, but each user must sign up and paste a key, and free tiers limit calls, history or real-time coverage. Limits change often: check each provider's current pricing page before you choose. |
| Broker API | A broker's own paper-trading or market-data API | Closest to real trading, most setup per user. |

If you use a key, read it from an environment variable (for example `PAPER_DATA_KEY`) in the engine, never hard-code it, and say in the README how to set it. Keep the rule that the engine writes only under `$PAPER_HOME`.

## 2. What to change

All paths are under `paper-trade/mods/paper-trade/`.

### Engine: `engine/paper.py`

| Area | Korea-specific today | Change to |
| --- | --- | --- |
| Quotes | `fetch_quote`, `fetch_kospi`, `fetch_index`, `board` read `polling.finance.naver.com` | Your source. Keep the returned field names (`sym`, `name`, `price`, `change_pct`, `open`, `tradable`, `currency`) so the rest of the engine and the panel need no change. |
| Candles | `fetch_chart` reads `api.stock.naver.com/chart/domestic/...` | Return the same rows: `{"d": "YYYYMMDD", "o", "h", "l", "c", "v"}`, oldest first. |
| Search | `search` keeps only `nationCode == "KOR"` | Your market's symbols, same `{"sym", "name", "market"}` rows. |
| Order book, key facts, flows | `fetch_book`, `fetch_detail` | Your source, or return `None` / empty lists; `view` already tolerates failures. |
| Financials, news, themes, scan | `fetch_finance`, `fetch_news`, `market_news`, `stock_list`, `group_list`, `theme_members`, `market_scan`, `macro` | Your source, or trim what the AI gets in `pulse`. `scan_parts` already reports a missing part instead of failing. |
| Session | `KST`, `kst_now`, `kr_regular_hours` (09:00 to 15:30, Mon to Fri); `pulse` and `tick` decide "market open" from `fetch_quote("005930")` | Your exchange's time zone and hours, and a liquid local stock (or index) for the open check. The US regular session is 09:30 to 16:00 New York time and moves against UTC with daylight saving, so use a real time zone (`zoneinfo.ZoneInfo("America/New_York")`), not a fixed offset. Holidays come from your data source's "market open" flag. |
| Money and costs | `START_CASH = 10_000_000`, `KRW_ONLY`, `DEFAULT_RULES` (`fee_kr_pct`, `tax_kr_sell_pct`, `fee_us_pct`, `slippage_pct`) | Your currency, a starting amount that makes sense in it, and your broker's fees and sale taxes. The ledger already stores a `currency` per position. |
| Symbols | `plan_errors` rejects codes that are not 6 characters; `DEFAULT_WATCH` holds Korean codes; the `buy`, `sell`, `view` and `watch` commands refuse non-Korean symbols (`is_us`) | Your symbol format (for example 1 to 5 letters for US tickers), a default watchlist, and remove or invert the "only Korean stocks" guards. |
| AI buy guard | `buy_errors` and the `buy` command use KOSPI's daily change for `dip_stop_pct` | Your benchmark index (for example the S&P 500). |
| Data keys | Naver's Korean field names: `시총`, `52주 최고`, `영업이익`, preferred-share suffix `우`, `스팩` | Your source's field names. |

### Runner and prompts: `engine/run.sh`, `engine/prompts/`

- `run.sh` exports `TZ=Asia/Seoul`; set your market's time zone so log file dates match the trading day.
- `plan.md` asks for candidates "across all KOSPI and KOSDAQ common stocks"; name your market. Check every engine command a prompt mentions still exists and is in the `run.sh` allow-lists (`LOOKUP` and the per-stage lists).

### Panel: `main.js`

- `krMarketHours`: the same session as the engine, used to run `tick` and auto-trade only during market hours.
- `won`: the currency format.
- `UP` / `DOWN`: Korea shows gains in red and losses in blue. Most other markets use green for up and red for down.
- `FACTS` and the `52주 최고` / `52주 최저` lookups: your source's key-fact names.
- The default symbol in `init` (`this.sym = "000660"`).

### Tests: `engine/test_paper.py`

`Session` tests the KRX hours; `Market` and `MarketScan` use Naver-shaped fixtures. Update them to your source and session and keep the rest: the ledger, fills, stop-losses, plan checks and AI buy guards do not depend on the market.

## 3. Check it

```sh
cd paper-trade/mods/paper-trade/engine
python3 -m unittest test_paper
PAPER_HOME=$(mktemp -d) python3 -I paper.py search apple
PAPER_HOME=$(mktemp -d) python3 -I paper.py view AAPL --tf day --count 60
herdr-app check ~/.config/herdr-app/mods/paper-trade
```

Then open the panel during your market's hours and check the quote, the chart, a 1-share buy that fills, and the ticker. Run "Set up profile" and "Make plan" once with your own `claude` to see the AI use your market.

## 4. Share it

If you publish your version, rename the mod folder (and `MOD` at the top of `main.js`) so it can sit next to the Korean one, update `RECIPE.md` and `preview.png`, and add the `herdr-mod` topic to your repo. A link back to this repo is appreciated.
