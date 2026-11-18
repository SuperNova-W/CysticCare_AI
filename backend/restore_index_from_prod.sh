#!/usr/bin/env bash
# Restore app/openai_chroma_data + app/index_manifest.json from the image that
# is currently serving prod (its index passed verify_baked_index at build time).
# Uses Cloud Build to copy the files out of the image, so Docker isn't needed locally.
set -euo pipefail
cd "$(dirname "$0")"

GCLOUD=/Users/supernova/google-cloud-sdk/bin/gcloud
# gcloud storage crashes on very new Pythons (ssl.PROTOCOL_TLSv1 removed);
# run gcloud on Homebrew's Python 3.11 when it's available.
for PY in /opt/homebrew/opt/python@3.11/bin/python3.11 /opt/homebrew/opt/python@3.12/bin/python3.12; do
  if [ -x "$PY" ]; then export CLOUDSDK_PYTHON="$PY"; break; fi
done
PROJECT=cysticcare-ai
REGION=us-east1
SERVICE=cysticcare-backend
STAMP=$(date +%Y%m%d%H%M%S)
DEST=gs://cysticcare-ai_cloudbuild/index-restore-$STAMP
WORK=../_index_restore_$STAMP          # outside backend/, so it is never uploaded
BROKEN=../_broken_index_$STAMP

echo "== 1/5 Finding the image prod is serving"
REV=$("$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --format='value(status.latestReadyRevisionName)')
IMAGE=$("$GCLOUD" run revisions describe "$REV" --project "$PROJECT" --region "$REGION" \
  --format='value(status.imageDigest)')
[ -n "$IMAGE" ] || { echo "ABORT: could not find the serving image"; exit 1; }
echo "   revision $REV"
echo "   image    $IMAGE"

# Reuse a copy from an earlier run if one is already in Cloud Storage.
PREVIOUS=$("$GCLOUD" storage ls gs://cysticcare-ai_cloudbuild/ 2>/dev/null | grep '/index-restore-' | sort | tail -1 || true)
if [ -n "$PREVIOUS" ] && "$GCLOUD" storage ls "${PREVIOUS}index_manifest.json" >/dev/null 2>&1; then
  DEST=${PREVIOUS%/}
  echo "== 2/5 Reusing the copy already in Cloud Storage: $DEST"
else
echo "== 2/5 Copying the index out of that image with Cloud Build (a few minutes)"
CFG=$(mktemp -t restore-index).yaml
cat > "$CFG" <<'YAML'
steps:
- name: gcr.io/cloud-builders/docker
  entrypoint: bash
  args:
  - -c
  - docker create --name idx "$_IMAGE" >/dev/null && docker cp idx:/app/app/openai_chroma_data /workspace/openai_chroma_data && docker cp idx:/app/app/index_manifest.json /workspace/index_manifest.json
- name: gcr.io/google.com/cloudsdktool/cloud-sdk:slim
  entrypoint: gcloud
  args: [storage, cp, -r, /workspace/openai_chroma_data, /workspace/index_manifest.json, "$_DEST/"]
timeout: 1800s
YAML
"$GCLOUD" builds submit --no-source --config "$CFG" --project "$PROJECT" --region "$REGION" \
  --substitutions "_IMAGE=$IMAGE,_DEST=$DEST"
rm -f "$CFG"
fi

echo "== 3/5 Downloading and verifying"
mkdir -p "$WORK"
"$GCLOUD" storage cp -r "$DEST/openai_chroma_data" "$DEST/index_manifest.json" "$WORK/"
python3 check_index_checksum.py "$WORK/openai_chroma_data" "$WORK/index_manifest.json" \
  || { echo "ABORT: the copy from prod does not match its own manifest; nothing was changed locally."; exit 1; }

echo "== 4/5 Swapping it in (broken copy kept at $BROKEN)"
mv app/openai_chroma_data "$BROKEN"
mv "$WORK/openai_chroma_data" app/openai_chroma_data
chmod -R u+rwX app/openai_chroma_data          # the image ships it read-only
if ! cmp -s "$WORK/index_manifest.json" app/index_manifest.json; then
  echo "   note: prod's index_manifest.json differs from yours; using prod's (it matches the index)"
  cp "$WORK/index_manifest.json" app/index_manifest.json
fi
python3 check_index_checksum.py

echo "== 5/5 Cleaning up the temporary copy in Cloud Storage"
"$GCLOUD" storage rm -r "$DEST" >/dev/null
echo "== Done. Delete $BROKEN once the deploy works. Next: bash enable_traceai_prod.sh"
