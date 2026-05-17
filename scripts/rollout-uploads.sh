#!/usr/bin/env bash
# scripts/rollout-uploads.sh
# Apply the upload/reindex fix to a running chat-llm-rag deployment.
#
# Prereqs already in place from your existing deploy:
#   • EKS cluster `chat-llm-rag` reachable via kubectl
#   • ECR repos chat-llm-rag-api and chat-llm-rag-ui
#   • Namespace `chat-llm-rag` with rag-api, rag-ui, milvus running
set -euo pipefail

NS=chat-llm-rag
REGION="${REGION:-us-east-1}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"

echo "▶︎  1/6  ECR login"
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"

echo "▶︎  2/6  Build rag-api (new /docs/* endpoints)"
docker build --platform linux/amd64 --no-cache \
  -t "$REGISTRY/chat-llm-rag-api:latest" \
  -f docker/Dockerfile.api .
docker push "$REGISTRY/chat-llm-rag-api:latest"

echo "▶︎  3/6  Build rag-ui (httpx-based upload)"
docker build --platform linux/amd64 --no-cache \
  -t "$REGISTRY/chat-llm-rag-ui:latest" \
  -f docker/Dockerfile.ui .
docker push "$REGISTRY/chat-llm-rag-ui:latest"

echo "▶︎  4/6  Create the docs PVC"
kubectl apply -f k8s/01-pvc.yaml

echo "▶︎  5/6  Mount PVC on rag-api + set DOC_STORE_DIR"
# Strategic-merge patch — adds the volume, mount, and env var without touching
# the rest of the spec. Safe to re-run.
kubectl -n "$NS" patch deployment rag-api --patch '
spec:
  template:
    spec:
      containers:
        - name: rag-api
          env:
            - name: DOC_STORE_DIR
              value: /data/docs
          volumeMounts:
            - name: docs
              mountPath: /data/docs
      volumes:
        - name: docs
          persistentVolumeClaim:
            claimName: docs-pvc
'

echo "▶︎  6/6  Restart rag-api and rag-ui to pick up the new images"
kubectl -n "$NS" rollout restart deployment/rag-api
kubectl -n "$NS" rollout restart deployment/rag-ui
kubectl -n "$NS" rollout status  deployment/rag-api --timeout=5m
kubectl -n "$NS" rollout status  deployment/rag-ui  --timeout=5m

echo
echo "✅ Done. The indexer Job is no longer needed — uploads + reindex now happen via the API."
echo
echo "Smoke test:"
echo "  API_POD=\$(kubectl -n $NS get pod -l app=rag-api -o jsonpath='{.items[0].metadata.name}')"
echo "  kubectl -n $NS port-forward \$API_POD 8000:8000   # in one terminal"
echo "  curl -F 'files=@./data/docs/sample.pdf' http://localhost:8000/docs/upload"
echo "  curl -X POST http://localhost:8000/docs/reindex"
echo "  curl http://localhost:8000/docs/list"
