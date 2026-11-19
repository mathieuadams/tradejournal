#!/usr/bin/env bash
# Deploy or update the whole stack, then publish the front end.
# Usage: STACK=trade-journal REGION=us-east-1 ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"
STACK="${STACK:-trade-journal}"
REGION="${REGION:-us-east-1}"

command -v sam >/dev/null || { echo "Install the AWS SAM CLI first: https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html"; exit 1; }
command -v aws >/dev/null || { echo "Install the AWS CLI v2 first."; exit 1; }

out() { aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

# The trading workspace (workspace/) is one always-on server in its own stack ($STACK-workspace). The site's CloudFront
# sends /ws* to it, so it lives at <site>/ws/ with the journal's sign-in. Alpaca keys: read by the server from the
# Data page (DynamoDB + KMS). ANTHROPIC_API_KEY: the GitHub secret (workflow env), stored in SSM.
WS_STACK="$STACK-workspace"
P="/tradejournal/workspace"
param() { aws ssm get-parameter --name "$P/$1" --with-decryption --query Parameter.Value --output text --region "$REGION" 2>/dev/null || true; }
setp() { aws ssm put-parameter --name "$P/$1" --type SecureString --value "$2" --overwrite --region "$REGION" >/dev/null; }
wout() { aws cloudformation describe-stacks --stack-name "$WS_STACK" --region "$REGION" --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text 2>/dev/null || true; }
SECRET="$(param ORIGIN_SECRET)"
if [ -z "$SECRET" ]; then SECRET="$(openssl rand -hex 24)"; setp ORIGIN_SECRET "$SECRET"; fi
WS_DNS="$(wout InstanceDns)"; [ "$WS_DNS" = "None" ] && WS_DNS=""

deploy_site_stack() {   # $1 = workspace server DNS ("" = no /ws route yet; SAM refuses empty values, so "none")
  local cdp="${COGNITO_DOMAIN_PREFIX:-}"
  if [ -z "$cdp" ] && [ -f samconfig.toml ]; then
    cdp="$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" --query "Stacks[0].Parameters[?ParameterKey=='CognitoDomainPrefix'].ParameterValue" --output text)"
  fi
  if [ -n "$cdp" ]; then
    sam deploy --stack-name "$STACK" --region "$REGION" --resolve-s3 --capabilities CAPABILITY_IAM \
      --no-confirm-changeset --no-fail-on-empty-changeset \
      --parameter-overrides "CognitoDomainPrefix=$cdp" "WorkspaceOrigin=${1:-none}" "WorkspaceOriginSecret=$SECRET"
  else
    echo "First deploy: SAM will ask for a Cognito domain prefix (must be globally unique, lowercase)."
    sam deploy --guided --stack-name "$STACK" --region "$REGION" --capabilities CAPABILITY_IAM
  fi
}
deploy_site_stack "$WS_DNS"

# ---------------------------------------------------------------- trading workspace server
for k in ANTHROPIC_API_KEY; do
  if [ -n "${!k:-}" ]; then setp "$k" "${!k}"; fi
done
setp COGNITO_USER_POOL_ID "$(out UserPoolId)"
setp COGNITO_CLIENT_ID "$(out UserPoolClientId)"
setp COGNITO_REGION "$REGION"
setp FUT_BUCKET "$(out DataBucketName)"
aws cloudformation deploy --stack-name "$WS_STACK" --template-file workspace/infra/workspace.yaml --region "$REGION" \
  --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset --parameter-overrides "OriginSecret=$SECRET" "ParamPath=$P" "TableName=$(out TableName)" "KeyArn=$(out BrokerKeyArn)" "DataBucket=$(out DataBucketName)"
[ -n "$(param ANTHROPIC_API_KEY)" ] || echo "Workspace AI is in demo mode: pass ANTHROPIC_API_KEY to deploy.sh in the workflow (env: ANTHROPIC_API_KEY: \${{ secrets.ANTHROPIC_API_KEY }})."
REL="$(mktemp -d)/workspace.zip"
(cd workspace && zip -qr "$REL" server web infra/ws-update.sh -x '*/__pycache__/*')
aws s3 cp "$REL" "s3://$(wout ReleaseBucket)/release/workspace.zip" --region "$REGION"
# refresh the settings file and the start script on the running server, restart it, show how it started
WS_ID="$(wout InstanceId)"; WS_BUCKET="$(wout ReleaseBucket)"; WS_TABLE="$(out TableName)"
SSM_JSON="$(mktemp)"
WS_BUCKET="$WS_BUCKET" REGION="$REGION" P="$P" WS_TABLE="$WS_TABLE" python3 - > "$SSM_JSON" <<'PY'
import json, os
e = os.environ
conf = f"BUCKET={e['WS_BUCKET']}\nREGION={e['REGION']}\nPARAM_PATH={e['P']}\nTABLE={e['WS_TABLE']}\nFEED=iex\n"
print(json.dumps({"commands": [
    "cat > /etc/workspace.conf <<'CONF'\n" + conf + "CONF",
    f"aws s3 cp s3://{e['WS_BUCKET']}/release/workspace.zip /tmp/ws.zip --region {e['REGION']} --quiet && unzip -o -q -j /tmp/ws.zip infra/ws-update.sh -d /tmp/wsu && install -m 755 /tmp/wsu/ws-update.sh /usr/local/bin/ws-update",
    "systemctl reset-failed workspace || true",
    "systemctl restart workspace",
    "sleep 8",
    "journalctl -u workspace --no-pager -n 15",
    "curl -s -o /dev/null -w 'local http %{http_code}\\n' localhost:8765/ws/ || true",
]}))
PY
CMD=$(aws ssm send-command --instance-ids "$WS_ID" --document-name AWS-RunShellScript --region "$REGION" \
  --parameters "file://$SSM_JSON" --query Command.CommandId --output text 2>/dev/null || true)
if [ -n "$CMD" ]; then
  sleep 15
  aws ssm get-command-invocation --command-id "$CMD" --instance-id "$WS_ID" --region "$REGION" --query StandardOutputContent --output text || true
else
  echo "(server not registered with SSM yet: it loads this release on its first start)"
fi
NEW_DNS="$(wout InstanceDns)"
if [ -n "$NEW_DNS" ] && [ "$NEW_DNS" != "None" ] && [ "$NEW_DNS" != "$WS_DNS" ]; then
  echo "Routing <site>/ws/ to the workspace server ($NEW_DNS)"
  deploy_site_stack "$NEW_DNS"
fi

cat > frontend/config.js <<CFG
// Generated by deploy.sh from stack $STACK
window.TJ_CONFIG = {
  apiUrl: "$(out ApiUrl)",
  region: "$REGION",
  userPoolId: "$(out UserPoolId)",
  clientId: "$(out UserPoolClientId)",
  cognitoDomain: "$(out CognitoDomain)",
  workspace: true
};
CFG

# stamp the script/style URLs so browsers load the new version right away
STAMP=$(date +%s)
SITE=$(mktemp -d)
cp -r frontend/. "$SITE/"
sed -i "s|src=\"app.js\"|src=\"app.js?v=$STAMP\"|; s|src=\"auth.js\"|src=\"auth.js?v=$STAMP\"|; s|src=\"chart.js\"|src=\"chart.js?v=$STAMP\"|; s|src=\"config.js\"|src=\"config.js?v=$STAMP\"|; s|href=\"styles.css\"|href=\"styles.css?v=$STAMP\"|" "$SITE/index.html"
aws s3 sync "$SITE" "s3://$(out SiteBucketName)" --delete --cache-control "no-cache" --region "$REGION"
aws s3 cp "$SITE/index.html" "s3://$(out SiteBucketName)/index.html" --cache-control "no-cache" --content-type "text/html" --region "$REGION"
aws cloudfront create-invalidation --distribution-id "$(out DistributionId)" --paths "/*" >/dev/null

echo
echo "Done. Open $(out SiteUrl)"
echo "Workspace: $(out SiteUrl)/ws/  (same sign-in as the journal)"
echo "(A brand-new CloudFront distribution can take a few minutes before the URL answers.)"
