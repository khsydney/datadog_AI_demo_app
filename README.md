# Chat LLM RAG · Datadog edition

Migration of [`khsydney/splunk-chat-LLM` (Chat-LLM-Cleaned)](https://github.com/khsydney/splunk-chat-LLM/tree/Chat-LLM-Cleaned)
from a Splunk Observability + Deepeval stack to a fully Datadog stack
deployed on AWS EKS (EC2 node groups).

## What changed

| Concern                | Before                                         | After                                                                                       |
| ---------------------- | ---------------------------------------------- | ------------------------------------------------------------------------------------------- |
| Tracing / metrics      | `opentelemetry-sdk` → OTel Collector → SignalFx | `ddtrace` (auto-injected via Cluster Agent Admission Controller) → node-local Datadog Agent |
| LLM Observability      | `splunk-otel-util-genai` handler + manual spans | `ddtrace.llmobs` `workflow / retrieval / task / agent / llm` spans                          |
| Evaluations            | `deepeval` (offline, locally)                  | Datadog **Managed Evaluations** + custom `LLMObs.submit_evaluation_for(...)` metrics        |
| LLM security           | none                                           | **Datadog AI Guard** (`ddtrace.appsec.ai_guard`) on prompt + response, w/ heuristic fallback |
| Cloud security posture | none                                           | **Datadog CSPM** (compliance benchmarks on the Agent)                                       |
| Identity entitlements  | none                                           | **Datadog CIEM** (configured via the Datadog AWS Integration)                               |
| SIEM signals           | none                                           | **Cloud SIEM** detection rules on app-emitted JSON security logs                            |
| Deployment             | local Docker Compose                           | EKS-on-EC2 (eksctl), NodePort exposure, ECR images                                          |

## Layout

```
.
├── app/
│   ├── main.py                # FastAPI + LLMObs.enable() + AI Guard input hook + sec router
│   ├── rag_pipeline.py        # workflow / retrieval / task / agent spans + submit_evaluation
│   ├── ai_guard.py            # Datadog AI Guard wrapper with heuristic fallback
│   └── security_demo.py       # endpoints that emit Cloud SIEM-friendly logs
├── index/                     # unchanged ingest pipeline (env-driven Milvus URI now)
├── streamlit_app.py           # UI (rebranded; ddtrace-run auto-instruments httpx)
├── docker/
│   ├── Dockerfile.api
│   └── Dockerfile.ui
├── eks/
│   └── cluster.yaml           # 2 × t3.xlarge managed node group
├── k8s/
│   ├── 00-namespace.yaml      # ns + ConfigMap (DD_SITE, MILVUS_URI, REDIS_URL, …)
│   ├── 01-secrets.yaml        # template (replace placeholders)
│   ├── 10-milvus.yaml         # etcd + minio + milvus standalone
│   ├── 20-redis.yaml
│   ├── 30-rag-api.yaml
│   ├── 40-rag-ui.yaml         # Service type: NodePort 30080
│   └── 50-indexer-job.yaml
├── datadog/
│   └── values.yaml            # Helm values: APM, Logs, CSPM, CWS, SBOM, USM, AdmCtrl
├── scripts/
│   ├── deploy.sh
│   └── trigger-siem-signals.sh
└── docs/
    ├── architecture.md
    └── siem-rules.md
```

## Quick start (AWS)

```bash
# 1. Auth + env
aws configure                              # or env vars
export AWS_REGION=ap-southeast-1
export DD_API_KEY=<your key>
export DD_APP_KEY=<your app key with ai_guard_evaluate scope>
export OPENAI_API_KEY=<sk-...>

# 2. Everything in one go
./scripts/deploy.sh

# 3. Browse the UI
#    The deploy script prints the NodePort URL. If your browser hangs,
#    the node SG isn't open on 30080 — the script prints the exact
#    `aws ec2 authorize-security-group-ingress` command to fix it.
```

## Tracing & LLM Observability

`ddtrace-run` is the entrypoint in both Dockerfiles. The Cluster Agent's
Admission Controller (`apm.instrumentation.enabled=true`) also injects
the ddtrace library; the explicit `ddtrace-run` is belt-and-braces.

Every request produces this LLM Observability trace:

```
workflow(rag-pipeline)
├── retrieval(milvus-search)         metrics.docs_retrieved
├── task(rerank)                     metrics.docs_after_rerank
├── task(format-context)
└── agent(rag-agent)
    └── llm(openai.chat.completions) ← auto-captured
```

Open **LLM Observability → Applications → chat-llm-rag** in the Datadog UI.

### Managed evaluations to turn on

In **LLM Observability → Evaluations → Managed**, enable:

- Failure to Answer
- Topic Relevance
- Prompt Injection (LLM-as-a-judge)
- Toxicity
- Sentiment
- Hallucination

These run automatically on every LLM span — no code change needed.

### Custom evaluations

The pipeline already calls `LLMObs.submit_evaluation_for()` with three
labels (`answer.length`, `retrieval.docs_returned`, `ai_guard.blocked`).
They show up in the trace view next to managed evals.

## AI Guard

Wired in `app/ai_guard.py` and called on the prompt (in `main.py`) and
the response (in `rag_pipeline.py`). On a `DENY`/`ABORT` verdict the
chat endpoint streams `[blocked by AI Guard: …]` and tags the LLMObs
workflow span with `ai_guard.response.action`.

> **Site availability:** AI Guard is supported on US1, US3, US5, EU,
> AP1 and AP2 (only `app.ddog-gov.com` / GovCloud is excluded). This
> stack runs on AP2. AI Guard is still in **Preview**, so before the
> demo you must:
>
> 1. Request access at <https://www.datadoghq.com/product-preview/ai-security/>
>    so Datadog flips the feature flag on your org.
> 2. Create an Application Key (Org Settings → Application Keys → New)
>    and restrict its scope to `ai_guard_evaluate`. Put it in
>    `datadog-secrets.DD_APP_KEY`.
>
> The wrapper falls back to a local heuristic guard if AI Guard isn't
> reachable, so the demo never breaks.
>
> **Known limitations and notes:**
>
> - Only the current turn is sent to AI Guard, not the full conversation.
>   For multi-turn injection detection use the LiteLLM proxy guardrail
>   (`ddtrace.appsec.ai_guard.integrations.litellm.DatadogAIGuardGuardrail`)
>   or the Strands Agents plugin (`AIGuardStrandsPlugin`).
> - Prompt-side evaluation adds ~300-800 ms before the first streamed
>   token. Response-side evaluation runs *after* streaming completes and
>   doesn't add to perceived latency.
> - Set `DD_AI_GUARD_BLOCK=false` in the ConfigMap to run in
>   monitor-only mode (evaluations recorded, no blocking).

## Cloud Security Posture (CSPM)

`datadog/values.yaml` sets `securityAgent.compliance.enabled=true`.
The Agent runs the bundled CIS benchmarks for Kubernetes / Docker and
posts findings to **Security → Cloud Security → Misconfigurations**.

To force a finding for the demo, intentionally leave a misconfig in
the cluster:

```bash
# An overly-permissive ServiceAccount as a CSPM smoke-test
kubectl -n chat-llm-rag create sa demo-overly-permissive
kubectl create clusterrolebinding demo-cluster-admin \
    --clusterrole=cluster-admin \
    --serviceaccount=chat-llm-rag:demo-overly-permissive
```

The CIS K8s "Minimize cluster-admin role bindings" check will flag it.

## CIEM (Identity & Entitlements)

CIEM is **not** an in-cluster feature. Configure it in the Datadog UI:

1. **Integrations → AWS → Add an AWS Account**.
2. Use the CloudFormation template Datadog provides. It creates the
   `DatadogIntegrationRole` IAM role.
3. Toggle **Cloud Security Posture Management** *and* **Identity Risks (CIEM)**.
4. Datadog starts pulling IAM / STS / Access Analyzer data.

A demo IAM principal to make findings light up:

```bash
aws iam create-user --user-name demo-overly-permissive
aws iam attach-user-policy --user-name demo-overly-permissive \
    --policy-arn arn:aws:iam::aws:policy/AdministratorAccess
aws iam create-access-key --user-name demo-overly-permissive   # never rotated
```

Datadog will surface this in **Security → Identity Risks** as
"AWS user with administrative privileges and unrotated access key".

## Cloud SIEM

The Datadog Agent's `containerCollectAll=true` ships every pod's logs
to Datadog. The structured JSON logs emitted by `app/security_demo.py`
have `@evt.name` fields that map cleanly onto detection rules. The four
recommended rules are documented inline in `app/security_demo.py` and
in `docs/siem-rules.md`.

To trigger them all in one shot once the stack is up:

```bash
API=http://<node-ip>:30080 ./scripts/trigger-siem-signals.sh
```

Then look at **Security → Cloud SIEM → Signals** (~5-15 min latency).

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Docker Compose for backing services
docker compose -f docker/milvus-compose.yaml up -d
docker compose -f docker/redis/docker-compose.yaml up -d

export OPENAI_API_KEY=sk-...
export DD_SITE=ap2.datadoghq.com
export DD_API_KEY=...
export DD_LLMOBS_ENABLED=1
export DD_LLMOBS_ML_APP=chat-llm-rag
# Agentless mode (no Datadog Agent locally):
export DD_LLMOBS_AGENTLESS_ENABLED=1

ddtrace-run uvicorn app.main:app --host 0.0.0.0 --port 8000
ddtrace-run streamlit run streamlit_app.py
```

## Teardown

```bash
helm -n datadog uninstall datadog
kubectl delete ns chat-llm-rag
eksctl delete cluster -f eks/cluster.yaml
```

## Security note about the API key in this repo

The `DD_API_KEY` is
embedded in `k8s/01-secrets.yaml` for the demo. **Rotate it in
Datadog → Organisation Settings → API Keys after the demo.** Long term,
keep it in AWS Secrets Manager or SOPS-encrypted manifests rather than
in cleartext YAML.
