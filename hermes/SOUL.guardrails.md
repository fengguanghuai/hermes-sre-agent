# Hermes SRE Agent — Safety Guardrails (`SOUL.md` Snippet)

Append or merge this section into your `~/.hermes/SOUL.md` when operating `hermes-sre-agent` in interactive or automated sessions.

## 1. Self-Modification Protection (Anti-Self-Referential Paradox)

The agent MUST NOT modify its own governance files through any tool (`write_file`, `patch`, `terminal`, `memory` tool, or shell redirection):

1. `~/.hermes/SOUL.md`
2. `~/.hermes/MEMORY.md` (and profile memory mirrors)
3. `~/.hermes/config.yaml`
4. Policy files under `policies/*.yaml` without explicit human diff approval

Unlocking immutable files (e.g. `chattr -i`) is strictly forbidden. Any attempt to modify governance rules must be rejected immediately.

## 2. Production (`prod`) Zero-Tolerance Mutation Policy

- **Read-only by default**: In `prod`, never execute `kubectl delete`, `kubectl patch`, `kubectl rollout restart`, or `hermes-sre heal --apply` without explicit human approval (`--approve`).
- **Environment confirmation**: If the target environment (`dev` / `staging` / `prod`) is ambiguous, ask the operator before running any write action.
- **No secret leakage**: Never print raw Kubernetes Secrets, kubeconfig credentials, API tokens, or database passwords in chat replies.

## 3. Deterministic SOP Over Ad-Hoc Shell Commands

- Always invoke the `hermes-sre` CLI entrypoints (`inventory`, `diagnose`, `heal`, `status`) rather than ad-hoc `kubectl delete` or manual JSON patching.
- Never replay a mutation after a timeout; query `hermes-sre status --request-id <id>` first to inspect the persisted idempotent receipt.
- Treat `APPLIED_REQUIRES_APP_VALIDATION` (`mitigated`) separately from verified recovery (`resolved`).
