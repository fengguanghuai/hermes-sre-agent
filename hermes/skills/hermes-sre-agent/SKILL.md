---
name: hermes-sre-agent
description: Policy-driven Kubernetes and standalone Docker/Compose workload diagnosis and bounded self-healing. Use when checking abnormal pods/containers, diagnosing CrashLoopBackOff/OOMKilled/ImagePullBackOff/mount/healthcheck failures, or executing controlled dry-run/applied self-healing with 4-layer anti-avalanche guardrails.
metadata:
  version: 0.1.0
  category: sre
---

# Hermes SRE Agent Skill

Use the deterministic `hermes-sre` CLI instead of ad-hoc `kubectl` or `docker` mutations. Every stage is decoupled: `Observation -> Classification -> Policy Gate -> Bounded Action -> Reconciliation`.

For standalone Docker / `docker compose` hosts, add `--runtime docker` (and optional `--docker-host ssh://user@host` or `--docker-context <ctx>`). `--namespace` maps to the Compose project name, `standalone`, or `all` (default for Docker), and `--pod` maps to the container name.

## Workflow Selection

### 1. Namespace Abnormal Pod Inventory (Read-Only)

When the user asks *"what pods are abnormal in namespace X?"*:

```bash
hermes-sre inventory --namespace <ns> --env <dev|staging|prod> --format markdown
```

- Reports abnormal, transitioning (<5m startup / <=10m terminating), and succeeded pods separately.
- Ready pods with historical restarts are NOT counted as abnormal.
- Do not claim application logs or downstream dependencies are healthy from container state alone.

### 2. Single-Pod Root-Cause Diagnosis (Read-Only)

When the user asks to diagnose a specific pod (or offline scenario fixture):

```bash
hermes-sre diagnose --namespace <ns> --env <dev|staging|prod> --pod <pod-name> --format markdown
```

Add `--dep tcp:<name>:<host:port>` or `--dep http:<name>:<url>` to probe downstream dependencies. Add `--scenario <path>` when replaying an offline incident fixture.

### 3. Controlled Self-Healing (Dry-Run First, Apply Only When Requested)

1. Run **dry-run** first (omit `--apply`) to verify classification confidence, negative-evidence checks, environment risk tier, pre-flight log guards, and 4-layer cooldown status:

```bash
hermes-sre heal --namespace <ns> --env <dev|staging|prod> --pod <pod-name> --request-id <unique-id> --format markdown
```

2. Only pass `--apply` when the user explicitly requests remediation (and `--approve` if the policy or `prod` environment requires human approval).
3. If a command times out or connection drops, **do not replay** with a new request ID. Query the persisted receipt instead:

```bash
hermes-sre status --request-id <same-id>
```

### 4. Following Up When Auto-Fix Is Not Possible

Every `diagnose` / `heal` result contains a `next_steps` section (`summary`, `user_actions`, `ask_user`, `needs_user_input`):

1. Explain `summary` to the user in plain language, then list `user_actions` they can take.
2. If `needs_user_input` is true, ask the `ask_user` questions. Do not guess the answers.
3. When the user replies, re-run with what they supplied (`how_to_supply`), e.g. `--dep ...` for dependencies, `--apply` after confirmation, `--approve` after approval. Always use a NEW `--request-id` for a new heal attempt.
4. If the user only says the problem was fixed externally (e.g. config added), re-run `diagnose` first, then `heal`.

### 5. Offline Benchmark Verification

To verify all 6 built-in fault-injection scenarios (true OOM, startup-probe false OOM, missing config block, transient mount heal, code defect escalation, avalanche circuit breaker):

```bash
hermes-sre benchmark --format markdown
```

## Hard Boundaries

- Never bypass `confidence_threshold`, `preflight_log_guard`, `infra_heal_boundary`, `approval_required`, or `cooldown:*` blocks by running raw `kubectl delete` / `kubectl patch` or `docker restart` / `docker update` / `docker rm`.
- Never equate `mitigated` with `resolved`; always report the reconciliation status and any rejected root-cause candidates transparently.
