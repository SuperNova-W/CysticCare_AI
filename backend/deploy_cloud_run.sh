#!/usr/bin/env bash
set -euo pipefail

/Users/supernova/google-cloud-sdk/bin/gcloud run deploy cysticcare-backend \
  --source . \
  --project cysticcare-ai \
  --region us-east1 \
  --allow-unauthenticated \
  --min-instances 0 \
  --cpu-throttling \
  --max-instances 3 \
  --concurrency 4 \
  --timeout 60 \
  --cpu 2 \
  --memory 8Gi \
  --env-vars-file cloud_run_env.yaml \
  --set-secrets "OPENAI_API_KEY=OPENAI_API_KEY:latest,FI_API_KEY=FI_API_KEY:latest,FI_SECRET_KEY=FI_SECRET_KEY:latest"
