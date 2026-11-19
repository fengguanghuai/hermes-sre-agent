---
name: hermes-sre-agent
description: Policy-driven Kubernetes workload diagnosis and bounded self-healing. Use when checking abnormal pods in a namespace, diagnosing CrashLoopBackOff/OOMKilled/ImagePullBackOff/mount failures, or executing controlled dry-run/applied self-healing with 4-layer anti-avalanche guardrails.
metadata:
  version: 0.1.0
  category: sre
---

# Hermes SRE Agent Skill

Use the deterministic `hermes-sre` CLI instead of ad-hoc `kubectl` mutations. Every stage is decoupled: `Observation -> Classification -> Policy Gate -> Bounded Action -> Reconciliation`.

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

### 4. Offline Benchmark Verification

To verify all 6 built-in fault-injection scenarios (true OOM, startup-probe false OOM, missing config block, transient mount heal, code defect escalation, avalanche circuit breaker):

```bash
hermes-sre benchmark --format markdown
```

## Hard Boundaries

- Never bypass `confidence_threshold`, `preflight_log_guard`, `infra_heal_boundary`, `approval_required`, or `cooldown:*` blocks by running raw `kubectl delete` / `kubectl patch`.
- Never equate `mitigated` with `resolved`; always report the reconciliation status and any rejected root-cause candidates transparently.
