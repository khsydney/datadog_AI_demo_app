#!/usr/bin/env bash
# scripts/deploy.sh — bring the whole stack up on a fresh AWS account.
#
# Prereqs on the workstation:
#   * awscli authenticated (`aws sts get-caller-identity` returns your acct)
#   * eksctl, kubectl, helm, docker installed
#   * Datadog API key + App key in env: DD_API_KEY, DD_APP_KEY
#   * OPENAI_API_KEY in env
#
# Usage:
#   chmod +x scripts/deploy.sh && ./scripts/deploy.sh

set -euo pipefail

CLUSTER=chat-llm-rag
REGION="${AWS_REGION:-us-east-1}"
NS=chat-llm-rag
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGISTRY="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
TAG="${TAG:-1.0.0}"

: "${DD_API_KEY:?DD_API_KEY must be set}"
: "${DD_APP_KEY:?DD_APP_KEY must be set (Application Key with ai_guard_evaluate scope)}"
: "${OPENAI_API_KEY:?OPENAI_API_KEY must be set}"

echo "▶︎  1/7  Provisioning EKS (this takes ~15 min)…"
eksctl create cluster -f eks/cluster.yaml || echo "  (cluster may already exist — continuing)"

echo "▶︎  2/7  Ensuring ECR repos exist…"
for repo in chat-llm-rag-api chat-llm-rag-ui; do
  aws ecr describe-repositories --repository-names "$repo" --region "$REGION" >/dev/null 2>&1 \
    || aws ecr create-repository --repository-name "$repo" --region "$REGION" >/dev/null
done

aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"

echo "▶︎  3/7  Building & pushing images…"
docker buildx build --platform linux/amd64 --load -f docker/Dockerfile.api -t "$REGISTRY/chat-llm-rag-api:$TAG" .
docker push "$REGISTRY/chat-llm-rag-api:$TAG"
docker buildx build --platform linux/amd64 --load -f docker/Dockerfile.ui  -t "$REGISTRY/chat-llm-rag-ui:$TAG" .
docker push "$REGISTRY/chat-llm-rag-ui:$TAG"

echo "▶︎  4/7  Installing Datadog Agent (Helm)…"
helm repo add datadog https://helm.datadoghq.com 2>/dev/null || true
helm repo update
kubectl create ns datadog --dry-run=client -o yaml | kubectl apply -f -
kubectl -n datadog delete secret datadog-keys --ignore-not-found
kubectl -n datadog create secret generic datadog-keys \
  --from-literal=api-key="$DD_API_KEY" \
  --from-literal=app-key="$DD_APP_KEY"
helm upgrade --install datadog datadog/datadog -n datadog -f datadog/values.yaml

echo "▶︎  5/7  Applying app manifests…"
kubectl apply -f k8s/00-namespace.yaml

# Seed real secret values.
kubectl -n "$NS" delete secret rag-secrets datadog-secrets --ignore-not-found
kubectl -n "$NS" create secret generic rag-secrets \
  --from-literal=OPENAI_API_KEY="$OPENAI_API_KEY"
kubectl -n "$NS" create secret generic datadog-secrets \
  --from-literal=DD_API_KEY="$DD_API_KEY" \
  --from-literal=DD_APP_KEY="$DD_APP_KEY"

# Replace REGISTRY placeholder in the deployment manifests.
for f in k8s/30-rag-api.yaml k8s/40-rag-ui.yaml k8s/50-indexer-job.yaml; do
  sed "s|REGISTRY|$REGISTRY|g; s|:1.0.0|:$TAG|g" "$f" | kubectl apply -f -
done
kubectl apply -f k8s/10-milvus.yaml
kubectl apply -f k8s/20-redis.yaml

echo "▶︎  6/7  Waiting for Milvus to become Ready (up to 5 min)…"
kubectl -n "$NS" rollout status statefulset/etcd --timeout=120s
kubectl -n "$NS" rollout status statefulset/minio --timeout=120s
kubectl -n "$NS" rollout status statefulset/milvus --timeout=300s

echo "▶︎  7/7  Running indexer Job…"
kubectl -n "$NS" delete job rag-indexer --ignore-not-found
sed "s|REGISTRY|$REGISTRY|g; s|:1.0.0|:$TAG|g" k8s/50-indexer-job.yaml | kubectl apply -f -
kubectl -n "$NS" wait --for=condition=complete --timeout=600s job/rag-indexer || \
  kubectl -n "$NS" logs job/rag-indexer

echo
echo "✅  Done. Access the UI:"
NODE_IP=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="ExternalIP")].address}')
if [ -n "$NODE_IP" ]; then
  echo "    http://$NODE_IP:30080"
  echo
  echo "    (If your browser hangs, the node SG isn't open on 30080. Run:"
  echo "       NG_SG=\$(aws ec2 describe-instances --filters \\"
  echo "         Name=tag:eks:cluster-name,Values=$CLUSTER \\"
  echo "         --query 'Reservations[0].Instances[0].SecurityGroups[0].GroupId' --output text)"
  echo "       aws ec2 authorize-security-group-ingress --group-id \$NG_SG \\"
  echo "         --protocol tcp --port 30080 --cidr 0.0.0.0/0 )"
else
  echo "    kubectl -n $NS port-forward svc/rag-ui 8501:8501"
  echo "    http://localhost:8501"
fi
