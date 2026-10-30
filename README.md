# Tradejournal Futures

NQ and ES: research, validate and paper trade intraday / swing strategies on CME Globex data from Databento. One AWS SAM stack.

## Pages
- **Running**: NQ / ES tiles, risk used today, the strategies paper trading, today's fills.
- **Chart**: candles 1m / 5m / 15m / 1h, session VWAP, prior-day and overnight levels, volume profile, delta, WaveTrend [Krypt], a strategy's trades drawn on the chart, and **replay** of a session bar by bar with its footprint, volume at price and big-print tape.
- **Research**: every strategy and its stage. New strategies from four setups: opening range break + FVG retest, range 50% retrace, ICT Silver Bullet, WaveTrend dots.
- **Strategy**: the protocol, always the same: Data → Study → Validation (train 60% / test 40%, random-entry baseline, 2× costs, 5 gates) → approved market conditions → Paper (simulated, kill switch) → Live (needs a futures broker: not yet).
- **Data**: connect Databento (key stored encrypted with KMS), download 1-minute candles and trades (footprint) with the cost shown first, automatic refresh every 15 minutes in the session and the day's trades after the close.

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
