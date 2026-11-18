#!/usr/bin/env bash
# One-off: turn on Trace AI (with question/answer capture) for cysticcare-backend,
# deploy, verify scale-to-zero + Trace AI status. (Commit/push is done separately on origin/main.)
set -euo pipefail
cd "$(dirname "$0")"

GCLOUD=/Users/supernova/google-cloud-sdk/bin/gcloud
PROJECT=cysticcare-ai
REGION=us-east1
SERVICE=cysticcare-backend

# Guard: never deploy with min-instances above 0 (see CLAUDE.md)
grep -q -- '--min-instances 0' deploy_cloud_run.sh || { echo "ABORT: deploy_cloud_run.sh is not --min-instances 0"; exit 1; }

# Guard: the Dockerfile rejects a baked index that doesn't match its manifest;
# fail here in seconds instead of after a 10-minute Cloud Build.
python3 check_index_checksum.py || { echo "ABORT: run 'bash restore_index_from_prod.sh' first"; exit 1; }

echo "== 1/5 Storing Future AGI keys in Secret Manager (values read from .env, not printed)"
for NAME in FI_API_KEY FI_SECRET_KEY; do
  VALUE=$(grep -E "^${NAME}=" .env | head -1 | cut -d= -f2- | tr -d '"'"'"'\r\n')
  [ -n "$VALUE" ] || { echo "ABORT: $NAME missing from backend/.env"; exit 1; }
  if "$GCLOUD" secrets describe "$NAME" --project "$PROJECT" >/dev/null 2>&1; then
    printf %s "$VALUE" | "$GCLOUD" secrets versions add "$NAME" --data-file=- --project "$PROJECT" >/dev/null
    echo "   $NAME: added new version"
  else
    printf %s "$VALUE" | "$GCLOUD" secrets create "$NAME" --data-file=- --replication-policy=automatic --project "$PROJECT" >/dev/null
    echo "   $NAME: created"
  fi
done

echo "== 2/5 Granting the Cloud Run service account access to the secrets"
SA=$("$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format='value(spec.template.spec.serviceAccountName)' 2>/dev/null || true)
if [ -z "$SA" ]; then
  PNUM=$("$GCLOUD" projects describe "$PROJECT" --format='value(projectNumber)')
  SA="${PNUM}-compute@developer.gserviceaccount.com"
fi
echo "   service account: $SA"
for NAME in FI_API_KEY FI_SECRET_KEY; do
  "$GCLOUD" secrets add-iam-policy-binding "$NAME" --project "$PROJECT" \
    --member="serviceAccount:${SA}" --role=roles/secretmanager.secretAccessor >/dev/null
done

echo "== 3/5 Deploying $SERVICE"
bash ./deploy_cloud_run.sh

echo "== 4/5 Verifying scale-to-zero (no idle billing) and Trace AI status"
# Revision-level min instances (template annotation) must be 0
REV_MIN=$("$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --format='value(spec.template.metadata.annotations["autoscaling.knative.dev/minScale"])')
# Service-level min instances (service annotation) must be 0
SVC_MIN=$("$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --format='value(metadata.annotations["run.googleapis.com/minScale"])')
# Request-based billing: cpu-throttling must not be "false"
THROTTLE=$("$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --format='value(spec.template.metadata.annotations["run.googleapis.com/cpu-throttling"])')
echo "   revision min=${REV_MIN:-0}  service min=${SVC_MIN:-0}  cpu-throttling=${THROTTLE:-true (default)}"
if [ "${REV_MIN:-0}" != "0" ]; then
  echo "!! revision minScale=$REV_MIN, forcing 0"
  "$GCLOUD" run services update "$SERVICE" --project "$PROJECT" --region "$REGION" --min-instances 0
fi
if [ "${SVC_MIN:-0}" != "0" ]; then
  echo "!! service-level minScale=$SVC_MIN, forcing 0"
  "$GCLOUD" run services update "$SERVICE" --project "$PROJECT" --region "$REGION" --min 0 \
    || echo "!! Could not clear service-level min automatically. Set Cloud Run > $SERVICE > Edit > Scaling > Minimum instances = 0."
fi
if [ "$THROTTLE" = "false" ]; then
  echo "!! instance-based billing is on, switching to request-based"
  "$GCLOUD" run services update "$SERVICE" --project "$PROJECT" --region "$REGION" --cpu-throttling
fi
URL=$("$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format='value(status.url)')
echo "   /health traceai block:"
curl -s "$URL/health" | python3 -c '
import sys, json
t = json.load(sys.stdin).get("traceai") or {}
print(json.dumps(t, indent=2))
if not (t.get("enabled") and t.get("conversation_capture_enabled")):
    sys.exit("!! Trace AI is not recording questions in prod (see last_error above). Not committing.")
'
echo "   Sending one smoke-test question (session traceai-smoke-test)..."
curl -s -o /dev/null -w "   /chat HTTP %{http_code}\n" -X POST "$URL/chat" -H 'Content-Type: application/json' \
  -d '{"query":"What is ADPKD?","session_id":"traceai-smoke-test"}'

echo "== Done. Deployed and verified. Check Trace AI for a 'What is ADPKD?' trace in session traceai-smoke-test."
echo "   Not committing here: this local branch can't be pushed to GitHub (tracks 180 MB files);"
echo "   commits are built on origin/main separately."
