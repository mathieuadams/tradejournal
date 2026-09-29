# Trade Journal

Trade journal with AI coaching, deployed as one AWS SAM stack.

It covers:
- **Import:** CSV import from any broker (IBKR Flex works as-is) and Alpaca sync.
- **Trades:** fills are automatically grouped into trades.
- **Journal:** a plan, tags and notes for each trade, plus a daily journal.
- **Analytics:** a leak breakdown in dollars, stats, what-if tools and a prop challenge tracker.
- **AI coach:** a review after each trade, a weekly report with one rule for next week, and chat with your data.

## What gets deployed

| Piece | AWS service |
|---|---|
| Web app (`frontend/`) | S3 (private) + CloudFront (OAC, HTTPS, security headers) |
| Sign-in | Cognito user pool + hosted sign-in page (code + PKCE, optional TOTP MFA) |
| API | API Gateway HTTP API with Cognito JWT authorizer → `api.handler` |
| Data | DynamoDB single table (on-demand, encrypted, point-in-time recovery, retained on stack delete) |
| CSV imports | S3 uploads bucket (presigned PUT) → `importer.handler` |
| Post-trade review | DynamoDB stream (new closed trades) → `reviewer.handler` → Claude |
| Weekly report | EventBridge Scheduler, Sundays 10:00 ET → `weekly.handler` → Claude |
| Alpaca sync | EventBridge Scheduler, weekdays 17:30 ET → `sync.handler` |
| Secrets | Anthropic key in Secrets Manager; users' broker keys encrypted with KMS |

The backend is plain Python 3.12 with no third-party packages, so there is no build step.

## Deploy with GitHub Actions (nothing to install locally)
Every push to `main` runs the tests and then deploys the whole stack. You can also start a run from the Actions tab (**deploy** → **Run workflow**).

1. **Create the deploy role (once).**
   Open AWS CloudShell (the `>_` icon in the AWS console, in the region you want) and run:
   ```bash
   git clone https://github.com/mathieuadams/tradejournal.git && cd tradejournal
   aws cloudformation deploy --template-file infra/github-oidc.yaml --stack-name tradejournal-github --capabilities CAPABILITY_NAMED_IAM
   aws cloudformation describe-stacks --stack-name tradejournal-github --query "Stacks[0].Outputs[0].OutputValue" --output text
   ```
   If it fails because the GitHub OIDC provider already exists in your account, add `--parameter-overrides CreateOidcProvider=false`.

   This lets only this repo's `main` branch deploy, with no stored AWS keys.
2. **Add repository variables.**
   In the GitHub repo, go to **Settings → Secrets and variables → Actions → Variables** and add:
   - `AWS_ROLE_ARN`: the ARN printed in step 1
   - `AWS_REGION`: `us-east-1`
   - `COGNITO_DOMAIN_PREFIX`: globally unique and lowercase, e.g. `tradejournal-mathieu`
   - `STACK_NAME`: optional, defaults to `trade-journal`
3. **Optional: add the Anthropic key.**
   Add the secret `ANTHROPIC_API_KEY` to turn on the AI coach.
4. **Deploy.**
   Push to `main`, or re-run the workflow. The run summary shows the app URL.

## Deploy from your own machine

### What you need
- An AWS account and the AWS CLI v2, configured with `aws configure`
- The AWS SAM CLI
- Optional: an Anthropic API key. It is only used by the AI coach (reviews, weekly report, chat). Without it, everything else works and the coach says it isn't configured.

### Mac / Linux / Git Bash
```bash
cd tradejournal
REGION=us-east-1 ANTHROPIC_API_KEY=sk-ant-... ./deploy.sh
```

### Windows PowerShell
```powershell
cd tradejournal
$env:ANTHROPIC_API_KEY="sk-ant-..."
.\deploy.ps1 -Region us-east-1
```

### First deploy prompts
The first run is guided. Answer the prompts like this:
- **Stack name / region:** press Enter to accept.
- **CognitoDomainPrefix:** something globally unique and lowercase, e.g. `tradejournal-mathieu`. It can't contain "aws", "amazon" or "cognito".
- **Other parameters:** press Enter for the defaults.
- **"Confirm changes before deploy":** N.
- **"Allow SAM CLI IAM role creation":** Y.
- **"Save arguments to configuration file":** Y.

### What the script does
The script:
1. Deploys the stack.
2. Stores your Anthropic key in Secrets Manager.
3. Writes `frontend/config.js` from the stack outputs.
4. Uploads the web app.
5. Prints the app URL.

Open the URL, click **Sign in or create an account**, sign up with your email, and confirm the code.

### Later updates
Run the same script again. You can leave `ANTHROPIC_API_KEY` unset after the first time.

To change the key later:
```bash
aws secretsmanager put-secret-value --secret-id <AnthropicSecretArn output> --secret-string sk-ant-...
```
Then force a cold start by redeploying, or wait for the Lambdas to recycle.

## Push this folder to GitHub
The zip already contains a git repository with one commit and `origin` set to https://github.com/mathieuadams/tradejournal.git:
```bash
cd tradejournal
git push -u origin main
```
`samconfig.toml` is ignored, so your stack settings stay local. The GitHub Action in `.github/workflows/test.yml` runs the backend tests on every push, with no AWS credentials needed.

## Using it

### Import
Go to **Import** and pick an account name, e.g. "Topstep 50K" or "IBKR cash". Then drop a CSV.

**Columns:**
- Required: time, symbol, side, quantity and price. Header names are matched loosely.
- Optional: fees, multiplier and account.
- For futures, common contract multipliers (MNQ, NQ, MES, ES, CL, GC…) are applied automatically. OCC option symbols use 100.

**Times:**
- Times without a zone are kept as written.
- Times ending in `Z` or with an offset are converted to US/Eastern.

**Duplicates:** fills are never edited, and re-uploading a file only adds fills that are new.

**Samples:** `samples/sample-fills.csv` and `samples/ibkr-flex-sample.csv`.

### Journal each trade
Open a trade to add the setup, planned entry/stop/target, thesis, notes, mistake tags and emotion. Tags save the moment you click them.

R multiples, planned risk, what-ifs and the leak breakdown all use the plan stop and the tags.

### Coach
New trades that closed in the last 3 days are reviewed automatically. For any other trade, click **Get coach review** in the trade.

The **Coach** page shows the weekly report (or **Write report now**) and a chat that answers only from your data.

### Charles Schwab
**Settings → Charles Schwab** connects through Schwab's official API with OAuth and syncs executed trades: stocks, ETFs and options, with fees.

It needs a Schwab developer app. Store its key and secret as the GitHub secrets `SCHWAB_APP_KEY` / `SCHWAB_APP_SECRET`, and register the callback `https://<your CloudFront domain>/schwab`. SETUP.md step 7 has the details.

Schwab requires a new login every 7 days.

### Charts
Each trade has 5m, 15m, 1h, 4h and daily candles:
- **Options:** charted on the underlying stock, with markers where the option was bought and sold.
- **Futures:** charted on the continuous front-month contract.

Bars come from Alpaca when it's connected. Otherwise they come from Yahoo Finance's public chart data, which needs no key. With Yahoo, 5m and 15m bars only go back about 60 days, and 1h bars about 2 years.

### Alpaca
Go to **Settings → Alpaca** and paste read-only keys (paper or live). This gives you:
- Fill sync.
- Candle charts with replay for stocks.
- How far each stock trade went against/for you (MAE/MFE), which is filled in when the review runs.

### Prop challenge
Go to **Settings → Prop challenge** and pick the account, start date, balance, trailing drawdown, target and daily loss limit.

## Run it on your machine (no AWS, no API key)
Needs Node 18+ and Python 3.
```bash
npm test        # backend tests + front-end syntax check
npm run build   # tests, then copies the web app to dist/
npm start       # app + API at http://localhost:5173
```
`npm start` needs no AWS account and no sign-in:
- **Data:** saved to `dev/.localdb.json`. Delete that file to start fresh.
- **Import:** try it with the files in `samples/`.
- **AI coach:** reviews, the weekly report and chat stay off until you set an `ANTHROPIC_API_KEY` environment variable. Everything else works without one.

## Project layout
```
template.yaml          SAM stack (all resources)
deploy.sh / deploy.ps1 deploy + publish web app
backend/
  api.py               HTTP routes (JWT user id only, input validation, audit trail)
  importer.py          S3 CSV import → fills → trades
  parsers.py           CSV parsing (generic + IBKR Flex)
  grouping.py          FIFO fills → trades, multipliers
  ingest.py            fill storage + trade rebuild
  views.py             trade + journal merge, R / MAE / MFE
  analytics.py         stats, leaks, patterns (numbers the AI explains)
  review.py            post-trade review
  reviewer.py          stream trigger for reviews
  weekly.py            weekly report
  chat.py              chat with tool use over your trades
  alpaca.py            Alpaca fills + bars, KMS-encrypted keys
  claude.py            Anthropic API client (stdlib)
  db.py / util.py      DynamoDB access, time and HTTP helpers
frontend/              static web app (index.html, app.js, auth.js, styles.css, config.js)
samples/               example CSV files
tests/                 in-memory tests for the whole API flow
```

## Removing the stack
```bash
sam delete --stack-name trade-journal --region us-east-1
```
The DynamoDB table and uploads bucket are retained on purpose so journal data isn't lost by accident. Delete them in the console if you really want them gone.
