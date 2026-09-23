# Trade Journal: setup guide (Windows + GitHub + AWS us-east-1)

Follow the steps in order.

**What you need:** a Windows PC, a GitHub account (owner of `mathieuadams/tradejournal`), and an AWS account.

**What you don't need:** Python, the AWS CLI or the SAM CLI on your PC. GitHub runs the tests and deploys to AWS for you.

---

## Step 1. Put the project on your PC

1. Download **tradejournal.zip** from the chat (click the file card and save it).
2. Open **PowerShell**: press the Windows key, type `powershell`, press Enter.
3. Paste this block and press Enter:

```powershell
cd $HOME\Downloads
$zip = Get-ChildItem tradejournal*.zip | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (Test-Path C:\Projects\tradejournal) { Remove-Item C:\Projects\tradejournal -Recurse -Force }
Expand-Archive -Path $zip.FullName -DestinationPath C:\Projects -Force
cd C:\Projects\tradejournal
dir
```

✅ **Check:** you see `backend`, `frontend`, `template.yaml`, `deploy.sh`, `README.md` and more in the list.

---

## Step 2. Push the code to GitHub

1. Check that Git is installed:

```powershell
git --version
```

   If you get "git is not recognized", install it, then **close and reopen PowerShell**:

```powershell
winget install --id Git.Git -e
```

   After reopening, run `cd C:\Projects\tradejournal`.

2. Push:

```powershell
git push -u origin main
```

   A browser window opens the first time. Sign in to GitHub and approve.

✅ **Check:** open https://github.com/mathieuadams/tradejournal. You should see the files. The **Actions** tab shows a **deploy** run.
- The **test** job should be green.
- The **deploy** job shows as *skipped*. That's expected until Step 4.

> If the push is rejected with "fetch first" (the GitHub repo already has a file, such as a README), run:
> `git pull origin main --rebase --allow-unrelated-histories` and then `git push -u origin main` again.

---

## Step 3. Create the AWS deploy role (one time only)

This creates a role that only the `main` branch of your GitHub repo can use to deploy. No AWS keys are stored in GitHub.

1. Go to https://console.aws.amazon.com and sign in.
2. In the **top-right** region menu, select **US East (N. Virginia) us-east-1**.
3. In the top bar, click the **`>_`** icon (CloudShell), just left of the bell. A terminal opens at the bottom. The first start takes about a minute.
4. Paste this and press Enter:

```bash
git clone https://github.com/mathieuadams/tradejournal.git && cd tradejournal
aws cloudformation deploy --region us-east-1 --template-file infra/github-oidc.yaml --stack-name tradejournal-github --capabilities CAPABILITY_NAMED_IAM
aws cloudformation describe-stacks --region us-east-1 --stack-name tradejournal-github --query "Stacks[0].Outputs[0].OutputValue" --output text
```

5. The last line prints the **role ARN**, which looks like this:

```
arn:aws:iam::123456789012:role/github-deploy-tradejournal-github
```

   **Copy it.** You need it in Step 4.

> If the second command fails and mentions that the provider **already exists**, run this instead, then run the third command again:
> ```bash
> aws cloudformation deploy --region us-east-1 --template-file infra/github-oidc.yaml --stack-name tradejournal-github --capabilities CAPABILITY_NAMED_IAM --parameter-overrides CreateOidcProvider=false
> ```

> **Lost the ARN?** In the AWS console, search **CloudFormation**, open the stack **tradejournal-github**, go to the **Outputs** tab, and copy **RoleArn**.

---

## Step 4. Add the settings in GitHub

1. Open https://github.com/mathieuadams/tradejournal.
2. Click **Settings**. It's the last tab in the repo's top bar, next to *Insights*.
3. In the left sidebar, click **Secrets and variables** → **Actions**.
4. Click the **Variables** tab (not *Secrets*).
5. Click **New repository variable**. Add each variable below, clicking **Add variable** after each one:

| Name | Value |
|---|---|
| `AWS_ROLE_ARN` | the ARN you copied in Step 3 |
| `AWS_REGION` | `us-east-1` |
| `COGNITO_DOMAIN_PREFIX` | a unique lowercase name, e.g. `tradejournal-mathieu`. Letters, numbers and dashes only. It must not contain `aws`, `amazon` or `cognito` |

6. **Optional: turn on the AI coach.** Click the **Secrets** tab → **New repository secret**.
   - Name: `ANTHROPIC_API_KEY`
   - Value: your key starting with `sk-ant-`

   You can skip this. Everything except the AI coach works without it, and you can add the key later.

✅ **Check:** the Variables tab lists `AWS_ROLE_ARN`, `AWS_REGION`, `COGNITO_DOMAIN_PREFIX`.

---

## Step 5. Deploy

1. In the repo, click the **Actions** tab.
2. In the left list, click **deploy**.
3. On the right, click **Run workflow** → keep branch `main` → **Run workflow**.
4. Click the run that appears and wait for both jobs to turn green. The first deploy takes **5–15 minutes**, mostly CloudFront.
5. Open the finished run. The **Summary** at the top shows:

```
Deployed: https://dxxxxxxxxxxxx.cloudfront.net
```

   That is your app's address. Bookmark it.

**What this creates in us-east-1:**
- S3 buckets for the website and CSV uploads
- CloudFront
- Cognito sign-in
- API Gateway with 5 Lambda functions
- A DynamoDB table
- A KMS key and a Secrets Manager secret
- Two schedules: the nightly Alpaca sync and the Sunday weekly report

---

## Step 6. First login and first import

1. Open the CloudFront URL. If it doesn't load yet, wait 5 minutes: a new CloudFront distribution needs time to spread.
2. Click **Sign in or create an account** → **Sign up**.
3. Enter your email and a password: at least 10 characters with a lowercase letter and a number.
4. Enter the verification code from your email. Check spam, since it comes from `no-reply@verificationemail.com`.
5. In the app, go to **Import**.
6. Type an account name (e.g. `Topstep 50K`) and drop a CSV. To test first, use `C:\Projects\tradejournal\samples\sample-fills.csv`.
7. Wait a few seconds. The status becomes **done** and your trades appear on **Home** and **Trades**.

Optional next steps in the app:
- **Settings → Alpaca:** paste read-only keys to sync fills and see stock charts.
- **Settings → Prop challenge:** track drawdown buffer, target and daily loss limit.

---

## Updating the app later

Every time you change code and push to `main`, GitHub tests it and redeploys automatically:

```powershell
cd C:\Projects\tradejournal
git add -A
git commit -m "describe the change"
git push
```

When I send you a new zip, repeat **Step 1** and then run `git push`. The zip already contains the new commit.

---

## If something goes wrong

| What you see | What to do |
|---|---|
| **deploy** job shows *skipped* | One of the variables in Step 4 is missing or misspelled. Names must match exactly. |
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | `AWS_ROLE_ARN` is wrong, or you ran the workflow from a branch other than `main`. Copy the ARN again from CloudFormation → tradejournal-github → Outputs. |
| `Domain already associated with another user pool` or a domain error | Your `COGNITO_DOMAIN_PREFIX` is taken. Change it (e.g. add numbers) and click **Re-run jobs**. |
| Stack is stuck in `ROLLBACK_COMPLETE` after a failed first deploy | In the AWS console (us-east-1), go to CloudFormation → select **trade-journal** → **Delete**. When it's gone, re-run the workflow. |
| Site shows an XML *AccessDenied* page or doesn't load | Wait 10 minutes after the first deploy, then refresh. |
| Sign-in says `redirect_mismatch` | Open the site with the exact CloudFront URL from the run summary, not a different address. |
| Coach says "The AI coach isn't configured yet" | Add the `ANTHROPIC_API_KEY` secret (Step 4, point 6) and re-run the deploy workflow. |
| Import shows **error** | Read the message in the Import list. It names the missing column or the row that couldn't be read. |

---

## Removing everything

1. In AWS CloudShell (us-east-1), run:

```bash
aws cloudformation delete-stack --region us-east-1 --stack-name trade-journal
aws cloudformation delete-stack --region us-east-1 --stack-name tradejournal-github
```

2. The DynamoDB table and the uploads bucket are kept on purpose so journal data isn't lost by accident. To remove them too, delete them from the DynamoDB and S3 consoles.
