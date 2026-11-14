# Tradejournal Futures

NQ and ES: research, validate and paper trade intraday / swing strategies on CME Globex data from Databento. One AWS SAM stack.

## Pages
- **Running**: NQ / ES tiles, risk used today, the strategies paper trading, today's fills.
- **Chart**: candles 1m / 5m / 15m / 1h, session VWAP, prior-day and overnight levels, volume profile, delta, WaveTrend [Krypt], a strategy's trades drawn on the chart, and **replay** of a session bar by bar with its footprint, volume at price and big-print tape.
- **Research**: every strategy and its stage. New strategies from four setups: opening range break + FVG retest, range 50% retrace, ICT Silver Bullet, WaveTrend dots.
- **Strategy**: the protocol, always the same: Data → Study → Validation (train 60% / test 40%, random-entry baseline, 2× costs, 5 gates) → approved market conditions → Paper (simulated, kill switch) → Live (needs a futures broker: not yet).
- **Fractal**: the TradeWellX Wave [N] rebuild (`backend/fractal.py`): MA vector, pivot, H/L wave counts, Direction, percent-probable clusters and the Option Analyzer wave odds. QQQ / stocks from Alpaca, NQ / ES from the stored Databento candles.
- **Earnings**: the fundamentals side (`backend/fundamentals.py`). **Screen**: revenue 3Y CAGR (TTM), TTM YoY, quarter YoY / QoQ, YoY acceleration, EPS YoY and inflection flags (TTM EPS turned positive, EPS growth turned up / accelerating, revenue accelerating), earnings quality (CFO / NI, Sloan accrual ratio) from SEC EDGAR XBRL; Q4 and quarterly cash flow derived from the 10-K / year-to-date figures. **Company**: 16 quarters and every 8-K item 2.02 release with its reaction (gap, day 0, vs benchmark, σ, 5 / 20 days, drift). **Event study**: your own release CSV (consensus, actuals, guidance, any extra signal columns) plus the 8-K dates of the screen's tickers; reactions from Alpaca split-adjusted daily bars; grouped by beat / raise, guidance, terciles of every signal, with N / mean / median / hit rate / t and a Spearman signal → reaction matrix; CSV export. **Financial Modeling Prep** (your key, Data page; plan check shows what it includes): consensus / actual EPS and revenue, report timing, the next scheduled release, the S&P 500 list (+ removed members). **Before the release**: returns 1 / 5 / 21 / 63 sessions vs the benchmark, 50 / 200-day trend, volume vs the 50-day average (1 / 5 / 20 days), accumulation (Chaikin money flow, on-balance volume), consensus growth, own past reactions. **Forward-looking model** (`backend/model.py`): signals ranked against the previous year's releases (trailing percentiles, no look-ahead), ridge regression tested walk-forward (no look-ahead) for day 0, the size of the day-0 move, 5 days, 1 and 2 months (before the release) and the drift after day 0; scorecard per target and a verdict; the tested model is applied to each company's next release on the Company tab, with the expected size of the move next to the options-implied move (Alpaca at-the-money straddle). A fixed post-release score (EPS / revenue surprise, call tone, tone change, revenue acceleration, low accruals) is scored on the 1- and 2-month drift. **After the call** model: from the day-0 open (exact benchmark open) to the close and to 5 days, using the reported numbers, surprises, the gap and the call (after-close releases only, so no call runs past the open). **Today** tab: the universe's releases reacting at a day's open, with the post-release score band, the call, the gap, the model's expectation and the flags “bad news + gap up” / “good news + gap down”. **Analysts**: upgrades − downgrades in the 30 / 90 days before each release; consensus price target (next release only). **Call transcripts** (FMP Ultimate; `backend/sentiment.py`): downloaded during the study and kept in S3 for good; tone (prepared remarks vs Q&A), uncertainty, guidance raise / maintain / lower from the outlook sentences, change vs the previous call; viewer on the Company tab; call-derived guidance fills beat-and-raise when your CSV has none. **Strategy backtest** (`backend/backtest.py`): legs A–D at a fixed stake per trade over the study's releases, the last 5 years as test; C and D enter at the day-0 close or open and exit on a stop, a trailing stop or a profit target (from the stock's daily bars after day 0, saved once per study) or after up to 60 sessions, as the stock or as a Black-Scholes-priced call / put (the stock's 20-day volatility × a multiplier, spread in % of the premium; no historical option quotes). **Real option prices** (`backend/optreal.py`, Unusual Whales): every C / D trade since 2022 re-priced with the contract listed that day nearest to the expiry / strike settings, bought at the NBBO ask and sold at the bid (mid shown), next to the model; the real implied volatility / model volatility ratio sets the multiplier for the earlier years. Setup filter toggles on C (beat SPY 1m, cash > earnings, guidance raised, market up, no Jan–Apr, no weak sectors, no heavy volume) and D (calm market, gap down, EPS miss, guidance not raised). Optimizer in four stages (entries, setup filters, exits, option): entries, then exits on the best entry, then the option on the best exits, each ranked on train only. `SEC_USER_AGENT` (optional) overrides the User-Agent sent to SEC (default: your sign-in e-mail).
- **Data**: connect Databento and Alpaca (keys stored encrypted with KMS); Databento (key stored encrypted with KMS), download 1-minute candles and trades (footprint) with the cost shown first, automatic refresh every 15 minutes in the session and the day's trades after the close.

## What gets deployed
| Piece | AWS |
|---|---|
| Web app (`frontend/`) | S3 + CloudFront |
| Sign-in | Cognito |
| API (`api.handler`) | HTTP API + Lambda |
| Jobs (`jobs.handler`): downloads, studies, paper, schedule | Lambda (15 min), EventBridge Scheduler |
| Settings, strategies | DynamoDB |
| Candles, footprints, strategy trades | S3 data bucket (gzip JSON) |

Plain Python 3.12, no third-party packages.

## Run it locally
    FAKE_DATABENTO=1 npm start      # synthetic NQ / ES data, any key starting with db-
    npm start                       # your real Databento key
    npm test

## Deploy
Push to `main`: GitHub Actions runs the tests and `deploy.sh`.
