# Architecture

## High-level

```
┌─────────────────────────────────────────────────────────────────────┐
│                          AWS account                                │
│  ┌────────────────────────────────────────────────────────────┐    │
│  │                EKS cluster: chat-llm-rag                   │    │
│  │                                                            │    │
│  │  ns: chat-llm-rag              ns: datadog                 │    │
│  │  ┌──────────────────┐          ┌───────────────────────┐  │    │
│  │  │ rag-ui (Streamlit)│         │ Cluster Agent         │  │    │
│  │  │  Service:NodePort │         │  ↳ Admission Ctrl     │──┼────┼──→ Datadog
│  │  │  30080            │         │     auto-injects      │  │    │   (ap2.datadoghq.com)
│  │  └────────┬──────────┘         │     ddtrace into pods │  │    │
│  │           │ http               └───────────────────────┘  │    │
│  │           ▼                    ┌───────────────────────┐  │    │
│  │  ┌──────────────────┐          │ Datadog Agent         │  │    │
│  │  │ rag-api (FastAPI)│ ──UDS──▶ │  DaemonSet            │  │    │
│  │  │  ddtrace-run     │ traces   │  • APM 8126           │  │    │
│  │  │  + LLMObs        │ metrics  │  • DogStatsD 8125     │  │    │
│  │  │  + AI Guard      │ logs     │  • Logs (tailing)     │  │    │
│  │  └─────┬────────────┘          │  • CSPM / CWS / SBOM  │  │    │
│  │        │                       │  • USM / NPM          │  │    │
│  │        ▼                       └───────────────────────┘  │    │
│  │  ┌──────────┐   ┌──────────┐                              │    │
│  │  │ Milvus   │   │ Redis    │                              │    │
│  │  │ (etcd +  │   │ chat-mem │                              │    │
│  │  │  minio)  │   └──────────┘                              │    │
│  │  └──────────┘                                             │    │
│  └────────────────────────────────────────────────────────────┘    │
│                                                                    │
│  Datadog AWS Integration (out of cluster, set in Datadog UI):      │
│   • IAM role: DatadogIntegrationRole                               │
│   • Pulls CloudTrail, IAM, STS, Access Analyzer, Config            │
│   • Powers CSPM (AWS posture) + CIEM (Identity Risks)              │
└─────────────────────────────────────────────────────────────────────┘
```

## Request flow (chat)

```
User  →  rag-ui  ──http──▶  rag-api /chat  ──┐
                                              ├─▶ AI Guard: evaluate_prompt
                                              │   (DENY → stream "[blocked]" and return)
                                              ▼
                                          LLMObs.workflow("rag-pipeline")
                                          ├── retrieval("milvus-search")
                                          │       Milvus :19530  → top 100 docs
                                          ├── task("rerank")
                                          │       BGE reranker → top 70
                                          ├── task("format-context")
                                          └── agent("rag-agent")
                                              └── llm(openai.chat.completions)   ← auto-captured
                                                  stream chunks back to UI
                                              ▼
                                          AI Guard: evaluate_response
                                          ▼
                                          submit_evaluation_for:
                                             answer.length
                                             retrieval.docs_returned
                                             ai_guard.blocked
```

## Span tree (LLM Observability)

| Span name           | Span kind   | Notable annotations                                                        |
| ------------------- | ----------- | --------------------------------------------------------------------------- |
| `rag-pipeline`      | `workflow`  | `session_id`, `model`, response `ai_guard.response.action`                  |
| `milvus-search`     | `retrieval` | `top_k`, `retriever`, `collection`, `metrics.docs_retrieved`               |
| `rerank`            | `task`      | `top_k`, `reranker`, `metrics.docs_after_rerank`                            |
| `format-context`    | `task`      | input `n_docs`, output `chars`                                              |
| `rag-agent`         | `agent`     | `provider`, `model`                                                         |
| auto-captured `llm` | `llm`       | full input/output messages, token usage, latency — captured by ddtrace      |

## Security data flow

```
                          stdout (JSON)
       ┌──────────────────┐       ┌──────────────────┐       ┌────────────────────┐
       │ app/security_demo │ ────▶ │ Datadog Agent    │ ────▶ │ Datadog Cloud SIEM │
       │ app/ai_guard     │ JSON  │ log tailer       │       │ detection rules    │
       └──────────────────┘       └──────────────────┘       └────────────────────┘

                                                              ┌────────────────────┐
       Node OS / containers ──▶  CWS (eBPF) / CSPM rules ──▶ │ Datadog Cloud Sec  │
                                                              │ Misconfigs + Sigs  │
                                                              └────────────────────┘

       AWS APIs (CloudTrail/IAM/STS) ──▶ Datadog AWS Integ ──▶ Identity Risks (CIEM)
```

## Port reference

| Service        | Port  | Type      | Notes                                            |
| -------------- | ----- | --------- | ------------------------------------------------ |
| rag-ui         | 8501  | NodePort 30080 | The only externally-reachable surface       |
| rag-api        | 8000  | ClusterIP | UI-internal only                                  |
| milvus         | 19530 | ClusterIP | gRPC                                              |
| milvus health  | 9091  | ClusterIP | `/healthz`                                       |
| etcd           | 2379  | Headless  | metadata for Milvus                              |
| minio API      | 9000  | ClusterIP | object store for Milvus                          |
| minio console  | 9001  | ClusterIP | not exposed (port-forward if needed)             |
| redis          | 6379  | ClusterIP | chat memory                                       |
| Datadog Agent  | 8126  | hostPort  | APM traces                                       |
| Datadog Agent  | 8125  | hostPort  | DogStatsD                                        |

## Why NodePort instead of an ALB?

Per the brief: minimal resources, no ALB, no Route53. NodePort gives a
fixed port (`30080`) on every worker node's ENI. After opening the node
SG, the UI is reachable at `http://<any-node-public-ip>:30080`. There
is no DNS or LB cost. For HA you would put an NLB in front of the node
group; for the demo, even one node suffices.

## Why `t3.xlarge × 2` instead of smaller?

The API pod's working set is:

| Component             | Approx. RAM |
| --------------------- | ----------- |
| BGE-m3 embedding      | ~2.3 GB     |
| BGE reranker v2-m3    | ~2.3 GB     |
| Python + libs         | ~0.5 GB     |
| Inference workspace   | ~0.5 GB     |
| **API pod total**     | **~5.5 GB** |

Milvus standalone wants ~1-4 GB depending on collection size. Datadog
Agent + Cluster Agent + system pods consume another ~1.5 GB per node.
Two t3.xlarge (16 GB each) hits the comfort zone with headroom for
bursts. You can scale down to `t3.large` if you accept that the API
pod will land on only one node and Milvus will compete with it.
