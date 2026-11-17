#!/bin/bash
# Runs as root before every start of the workspace service (ExecStartPre): latest code from S3, settings and keys,
# data restored from S3 on a fresh disk. Nothing here may stop the server from starting except missing code.
# Settings come from /etc/workspace.conf (BUCKET, REGION, PARAM_PATH, TABLE, FEED), written by the stack / deploy.sh.
set -u
. /etc/workspace.conf
log() { echo "ws-update: $*"; }

if aws s3 cp "s3://$BUCKET/release/workspace.zip" /tmp/ws.zip --region "$REGION" --quiet; then
  rm -rf /opt/workspace.new && mkdir /opt/workspace.new && unzip -q /tmp/ws.zip -d /opt/workspace.new \
    && rm -rf /opt/workspace && mv /opt/workspace.new /opt/workspace
  [ -f /opt/workspace/infra/ws-update.sh ] && install -m 755 /opt/workspace/infra/ws-update.sh /usr/local/bin/ws-update
else
  log "could not download the release; keeping the current code"
fi
[ -f /opt/workspace/server/server.py ] || { log "no code yet"; exit 1; }

ENV=/etc/workspace.env.new
: > "$ENV"; chmod 600 "$ENV"
aws ssm get-parameters-by-path --path "$PARAM_PATH" --with-decryption --region "$REGION" --query 'Parameters[].[Name,Value]' --output text 2>/tmp/ws-err \
  | while IFS=$'\t' read -r n v; do [ -n "$n" ] && echo "${n##*/}=$v"; done >> "$ENV" || log "ssm: $(cat /tmp/ws-err)"
echo "ALPACA_FEED=${FEED:-iex}" >> "$ENV"

# Alpaca keys: the ones saved in the journal (Data -> Alpaca), KMS-encrypted in DynamoDB
alpaca() {
  # the scan is paginated (the table is big): collect every saved key, try each until one decrypts
  local all enc plain
  all=$(aws dynamodb scan --table-name "$TABLE" --filter-expression "SK = :s" --expression-attribute-values '{":s":{"S":"DATASOURCE#alpaca"}}' \
        --projection-expression "enc" --region "$REGION" --query 'Items[].enc.S' --output text 2>/tmp/ws-err) || { log "alpaca: scan failed: $(cat /tmp/ws-err)"; return; }
  all=$(echo "$all" | tr '\t ' '\n\n' | grep -v -e '^None$' -e '^$' || true)
  if [ -z "$all" ]; then log "alpaca: no key saved on the Data page (simulated data)"; return; fi
  for enc in $all; do
    echo "$enc" | base64 -d > /tmp/alp.bin 2>/dev/null || continue
    plain=$(aws kms decrypt --ciphertext-blob fileb:///tmp/alp.bin --region "$REGION" --query Plaintext --output text 2>/tmp/ws-err) || { log "alpaca: kms decrypt failed: $(cat /tmp/ws-err)"; continue; }
    rm -f /tmp/alp.bin
    if echo "$plain" | base64 -d | /usr/bin/python3.12 -c 'import json,sys; k,s=json.load(sys.stdin); print("ALPACA_KEY_ID="+k); print("ALPACA_SECRET_KEY="+s)' >> "$ENV" 2>/tmp/ws-err; then
      log "alpaca: key loaded"; return
    fi
    log "alpaca: unexpected key format"
  done
  rm -f /tmp/alp.bin
}
[ -n "${TABLE:-}" ] && alpaca
mv "$ENV" /etc/workspace.env

if [ -z "$(ls -A /var/lib/workspace 2>/dev/null)" ]; then
  aws s3 sync "s3://$BUCKET/data/" /var/lib/workspace/ --region "$REGION" --quiet || true
  chown -R workspace /var/lib/workspace
fi
exit 0
