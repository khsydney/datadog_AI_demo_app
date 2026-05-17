# Cloud SIEM detection rules

Create these in Datadog under **Security → Cloud SIEM → Detection Rules
→ New Rule → Log Detection**. All four sit on top of the JSON logs that
`app/security_demo.py` and `app/ai_guard.py` emit.

The log source is `python` (matches `ad.datadoghq.com/<container>.logs`
set in `k8s/30-rag-api.yaml`) and the service is `rag-api`.

---

## Rule 1 — Brute-force login attempt

| Field        | Value                                                                                      |
| ------------ | ------------------------------------------------------------------------------------------ |
| Severity     | Medium                                                                                     |
| Search query | `service:rag-api @evt.name:user.login.failed`                                              |
| Group by     | `@usr.id`, `@network.client.ip`                                                            |
| Condition    | `> 5 in 5 minutes`                                                                         |
| Message      | "Brute-force login attempt against user {{@usr.id}} from {{@network.client.ip}}"           |
| MITRE        | T1110 — Brute Force                                                                        |

**Trigger from the demo script:** the 8 sequential failed logins from
the same IP/user fire this rule reliably.

---

## Rule 2 — Unauthorised admin endpoint access

| Field        | Value                                                                                              |
| ------------ | -------------------------------------------------------------------------------------------------- |
| Severity     | High                                                                                               |
| Search query | `service:rag-api @evt.name:admin.access.unauthorized`                                              |
| Group by     | `@network.client.ip`                                                                               |
| Condition    | `> 3 in 10 minutes`                                                                                |
| Message      | "Multiple unauthorised hits on admin endpoint from {{@network.client.ip}}"                         |
| MITRE        | T1078 — Valid Accounts (attempted bypass)                                                          |

**Trigger:** the demo script issues 6 hits in a tight loop.

---

## Rule 3 — LLM prompt-injection burst

| Field        | Value                                                                                                |
| ------------ | ---------------------------------------------------------------------------------------------------- |
| Severity     | High                                                                                                 |
| Search query | `service:rag-api @evt.name:ai_guard.prompt @ai_guard.action:(DENY OR ABORT)`                         |
| Group by     | `@network.client.ip`                                                                                 |
| Condition    | `> 3 in 5 minutes`                                                                                   |
| Message      | "Repeated prompt-injection attempts blocked by AI Guard from {{@network.client.ip}}: {{@ai_guard.tags}}" |
| MITRE        | T1059 — Command and Scripting Interpreter (LLM abuse) / OWASP LLM01                                 |

**Trigger:** the demo script sends 3 injection prompts in sequence;
adjust the threshold to `>= 3` if you want a single-run firing.

---

## Rule 4 — Suspicious file access

| Field        | Value                                                                                              |
| ------------ | -------------------------------------------------------------------------------------------------- |
| Severity     | Critical                                                                                           |
| Search query | `service:rag-api @evt.name:file.download @file.suspicious:true`                                    |
| Group by     | `@network.client.ip`                                                                               |
| Condition    | `>= 1 in 1 minute` (i.e. any hit)                                                                  |
| Message      | "Attempted access to sensitive path {{@file.path}} from {{@network.client.ip}}"                    |
| MITRE        | T1083 — File and Directory Discovery / T1552 — Unsecured Credentials                              |

**Trigger:** any request to `/demo/security/files/download?path=` with a
path containing `/etc/passwd`, `/etc/shadow`, `.ssh/`, `.aws/credentials`,
or `private.key`.

---

## Rule 5 (bonus) — IAM misconfiguration emitted by the app

| Field        | Value                                                                              |
| ------------ | ---------------------------------------------------------------------------------- |
| Severity     | High                                                                               |
| Search query | `service:rag-api @evt.name:iam.policy.overly_permissive`                          |
| Condition    | `>= 1 in 1 minute`                                                                |
| Message      | "App reported an overly-permissive IAM policy: {{@iam.principal}}"                |

Useful to **stitch** Cloud SIEM signals with CIEM findings on the same
dashboard — the principal in the log matches a real IAM role you can
also examine in **Security → Identity Risks**.

---

## Testing & validation

```bash
# After the stack is up and the indexer Job has finished:
API=http://<node-ip>:30080 ./scripts/trigger-siem-signals.sh

# In Datadog:
#   1. Logs → Search:  service:rag-api source:python
#      You should see the JSON events streaming in within ~30s.
#   2. Security → Cloud SIEM → Signals
#      The four rule names appear within ~5-15 min (rule eval cadence).
```

## Tuning

- The thresholds above are tuned for a single-run demo. In production
  set them to multi-minute windows with `top N IPs` group-by to keep
  noise down.
- Add **Suppression rules** for known good test IPs so demo runs from
  the office network don't keep firing on the same signal.
- Enable **Notification rules** (Slack/PagerDuty) on Severity ≥ High so
  the SIEM is wired all the way through to humans.
