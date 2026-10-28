# kdiag

kdiag diagnoses a broken Kubernetes namespace and fixes it **one change at a time, with your approval**.

It collects a redacted snapshot, runs deterministic checks, optionally lets an LLM (Grok via OpenRouter by default)
investigate with read-only kubectl tools, then proposes a single fix with the diagnosis, the evidence,
an exact diff, and the reasoning. Nothing changes until you answer `y`. After each change kdiag waits
for the rollout, re-checks the namespace, and feeds the new state into the next proposal.

```
$ kdiag fix -n shop
kdiag: namespace shop, context prod-east
Found 3 problem(s) and 3 related symptom(s):
  [critical] Deployment/redis: nodeSelector redisnode=true matches no node
  [warning] Deployment/redis container redis: memory limit 5Mi is far too small
  [critical] Deployment/redis container redis: readinessProbe checks port 5432 but the container listens on 6379
...
Apply this fix? [y]es / [n]o / [s]kip / [q]uit:
```

## Install

Python 3.9 or newer and `kubectl` on your PATH.

```bash
pip install "git+https://github.com/tydev-new/kdiag.git"
kdiag --version
```

For development:

```bash
git clone https://github.com/tydev-new/kdiag.git && cd kdiag
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

The only runtime dependency is PyYAML. HTTP calls to the LLM use the Python standard library.

## Usage

All commands accept `--kubeconfig`, `--context`, and `-n/--namespace`. kdiag uses kubectl's own
defaults when you leave them out, and it always prints the context it is acting on.

| Command | What it does | Changes the cluster? |
|---|---|---|
| `kdiag collect -n NS [-o bundle.zip]` | Writes a redacted snapshot (pods, deployments, replica sets, services, endpoints, events, config map keys, PVCs, node labels and capacity, recent logs). Secret values are never read. | No |
| `kdiag check -n NS` | Runs the deterministic checks live and prints findings. Exit code 1 if problems are found. | No |
| `kdiag check --bundle bundle.zip` | Same checks, offline, against a bundle someone sent you. | No |
| `kdiag fix -n NS` (alias `kdiag run`) | The interactive loop: diagnose, propose one fix, ask, apply, verify, repeat. | Only after you answer `y` |
| `kdiag eval --context kind-test` | Runs the bundled broken scenarios on a test cluster and writes a scorecard. | Yes, in `kdiag-eval-*` namespaces only |

Answers at the prompt:

- `y` applies the fix through the write path, then verifies it.
- `n` rejects this fix. With an LLM enabled, kdiag tells the model and it may propose a different fix.
- `s` skips this problem for the rest of the session.
- `q` stops immediately. Nothing else is changed.

`--yes` approves every fix without asking. It exists for CI and evals; do not use it on a cluster you care about.

Each `fix` session writes `audit.jsonl` and `report.json` under `~/.kdiag/sessions/<time>/`
(override with `--session-dir` or `KDIAG_HOME`).

## What it checks

Rules that can name a specific, safe change come with a fix. Rules that describe a symptom are linked
to the root cause on the same workload, so you see one problem instead of five.

| Rule | Detects | Automatic fix |
|---|---|---|
| `nodeselector-no-match` | nodeSelector that no node satisfies | remove the unmatched keys |
| `node-affinity-no-match` | required node affinity that no node satisfies | remove the required affinity |
| `probe-port-mismatch` | readiness, liveness, or startup probe on a port the container does not declare (or not the image's well-known port) | point the probe at the declared port |
| `memory-too-small` | memory request or limit under 32Mi (configurable with `--min-memory`) | 128Mi request, 512Mi limit |
| `oom-killed` | container OOMKilled | double the limit (minimum 128Mi) |
| `image-pull-failure` | ErrImagePull, ImagePullBackOff, InvalidImageName | roll back to the image of the previous revision, when there is one |
| `crash-loop` | CrashLoopBackOff, RunContainerError, repeated non-zero exits, with log tail | remove a command override whose executable does not exist; otherwise none |
| `container-config-error` | missing ConfigMap or Secret reference | none (kdiag never creates config or secrets) |
| `service-no-matching-pods` | Service selector that matches no pod | point the selector at the only workload serving the target port |
| `service-target-port-mismatch` | Service targetPort not declared by the selected pods | use the declared port |
| `unschedulable`, `probe-failing`, `pvc-pending`, `service-no-ready-endpoints`, `workload-unavailable` | status and event checks | none; reported as symptoms or handed to the LLM |

## LLM provider

kdiag works with no LLM at all: the deterministic rules detect and fix every bundled scenario.
With an LLM, the model leads each round. It sees the rule findings and candidate fixes, runs its own
read-only kubectl commands, and proposes one fix (it may accept a candidate or propose a better one).

Any OpenAI-compatible chat completions API with tool calling works. The default provider is
[OpenRouter](https://openrouter.ai) with a Grok model, so one key gives access to Grok and every
other model OpenRouter serves. Each user brings their own key; kdiag never stores it.

```bash
export OPENROUTER_API_KEY=...     # from https://openrouter.ai/settings/keys
kdiag fix -n shop                 # --llm auto (default) uses OpenRouter when OPENROUTER_API_KEY is set
```

The default model slug is `x-ai/grok-4.7` (Grok 4.7 as listed on OpenRouter in October 2026).
Model slugs change often: check OpenRouter's current model list
([openrouter.ai/models?q=grok](https://openrouter.ai/models?q=grok)) for the exact slug you want and
set it with `--llm-model` or `KDIAG_LLM_MODEL`, for example `KDIAG_LLM_MODEL=x-ai/grok-4.6`.

| Setting | Flag | Environment | Default |
|---|---|---|---|
| Mode | `--llm auto\|none\|mock\|live` | | `auto` (provider if its key is set, otherwise rules only) |
| Provider preset | `--llm-provider openrouter\|xai\|openai\|custom` | `KDIAG_LLM_PROVIDER` | `openrouter` |
| Base URL | `--llm-base-url` | `KDIAG_LLM_BASE_URL` | from the preset (`https://openrouter.ai/api/v1`) |
| Model | `--llm-model` | `KDIAG_LLM_MODEL` | from the preset (`x-ai/grok-4.7`) |
| Key variable name | `--llm-key-env` | `KDIAG_LLM_API_KEY_ENV` | from the preset (`OPENROUTER_API_KEY`) |
| Turn cap per investigation | `--max-steps` | | 12 |
| Token cap per session | `--max-tokens` | | 200000 |
| Spend cap per session (USD) | `--max-cost` | `KDIAG_PRICE_IN_PER_MTOK`, `KDIAG_PRICE_OUT_PER_MTOK` | 0.50 |

Flags override environment variables, which override the preset. For the spend cap kdiag uses the
actual cost OpenRouter reports in each response (`usage.cost`); for other providers it estimates cost
from token counts and the per-million-token prices (preset defaults, or the two env vars above).

Presets:

| Preset | Base URL | Key variable | Default model |
|---|---|---|---|
| `openrouter` (default) | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` | `x-ai/grok-4.7` |
| `xai` (Grok direct) | `https://api.x.ai/v1` | `XAI_API_KEY` | `grok-4.7` |
| `openai` | `https://api.openai.com/v1` | `OPENAI_API_KEY` | none, set `--llm-model` |
| `custom` | none, set `--llm-base-url` | `KDIAG_LLM_API_KEY` | none, set `--llm-model` |

Examples:

```bash
# xAI direct
XAI_API_KEY=... kdiag fix -n shop --llm-provider xai
# OpenAI
OPENAI_API_KEY=... kdiag fix -n shop --llm-provider openai --llm-model <model>
# Local OpenAI-compatible server (Ollama, vLLM, LiteLLM, ...)
KDIAG_LLM_API_KEY=unused kdiag fix -n shop --llm-provider custom \
  --llm-base-url http://localhost:11434/v1 --llm-model <model>
```

`--llm mock` is a keyless, offline stand-in (no API calls) that runs the real agent loop (it calls read-only kubectl tools
against your cluster) and then accepts the first deterministic candidate. It has no reasoning of its
own; it exists to test the plumbing.

## Safety model

1. **Two separate kubectl paths.** `ReadOnlyKubectl` is the only thing the LLM can reach. Every argv is
   checked against an allowlist of verbs (`get`, `describe`, `logs`, `events`, `top`, `explain`, ...),
   resources, and flags before a process starts. Secrets, `exec`, `--raw`, `--kubeconfig`, `--context`,
   `--token`, other namespaces, and go-templates are refused. `KubeWriter` is the write path; it only
   builds `kubectl patch` from validated fields (allowed kinds: Deployment, StatefulSet, DaemonSet,
   Service, ConfigMap, PVC) and the LLM has no tool that calls it.
2. **The LLM only proposes.** Its `propose_fix` tool returns data to kdiag. kdiag validates the patch with
   a server-side dry run, shows you the resulting diff, and applies it only if you say yes.
3. **One change at a time,** each verified with a rollout check and a fresh round of rules. A failed
   verification is fed back into the next round.
4. **Redaction everywhere.** Secret values are never read (only names and key names). Sensitive env
   values, token-like strings, bearer headers, private keys, and JWTs are masked in bundles, LLM tool
   output, and the audit log. `last-applied-configuration` annotations are dropped.
5. **Caps.** Turn, token, and estimated spend caps bound every LLM session.
6. **Audit log.** Every kubectl command (read, dry run, write, verify), every LLM call, every decision,
   and every result is one JSON line in the session's `audit.jsonl`.
7. **Evals are fenced.** `kdiag eval` refuses to run unless you pass `--context` or `--kubeconfig`
   explicitly, and only creates or deletes namespaces named `kdiag-eval-*`.

## Evals

Scenarios live in [`scenarios/`](scenarios/). Each has a `manifest.yaml` and an `expected.yaml` with the
findings that must be detected, the checks the fixed objects must pass, and whether the namespace should
be healthy afterwards.

| Scenario | Bug |
|---|---|
| 00-healthy | none; nothing should be proposed |
| 01-nodeselector | nodeSelector `disktype=ssd`, no such node |
| 02-probe-port | Redis readiness probe on 5432, Redis listens on 6379 |
| 03-memory-5mi | 5Mi memory request and limit |
| 04-redis-combined | all three of the above on one Redis Deployment |
| 05-bad-image-tag | working Deployment updated to a tag that does not exist |
| 06-service-selector | Service selects `app=redis-cache`, pods are `app=redis` |
| 07-crashloop-bad-command | command override `redis-srv` (typo) |
| 08-missing-configmap | `envFrom` a ConfigMap that does not exist; must be detected and must not be "fixed" |

Run them on a throwaway kind cluster:

```bash
kind create cluster --name kdiag-test --config test/kind-config.yaml
kind load docker-image redis:7.2-alpine busybox:1.36 --name kdiag-test   # optional, avoids pulls
kdiag eval --context kind-kdiag-test --out eval-results          # rules only
kdiag eval --context kind-kdiag-test --llm mock                 # exercise the agent loop without a key
OPENROUTER_API_KEY=... kdiag eval --context kind-kdiag-test --llm live   # real model, your account
```

The scorecard (`eval-results/scorecard.md` and `.json`) reports per scenario: detection, correct fix,
healthy after, fixes applied, steps (fix proposals plus LLM tool calls), and time. Transcripts and audit
logs for each scenario are written next to it.

## How it relates to k8sgpt

[k8sgpt](https://github.com/k8sgpt-ai/k8sgpt) pioneered this space with deterministic analyzers plus a
one-shot LLM explanation of what they find. kdiag builds on the same idea and adds:

- an **agent loop**: the model gathers its own evidence with allowlisted read-only tools, under step and spend caps;
- **gated fixes**: one concrete patch at a time, with diff, evidence, and reasoning, applied only on approval;
- **verification**: rollout and re-check after every change, with failures fed back into the next round;
- **offline bundles**: collect once, analyze anywhere, with secrets redacted;
- **evals**: reproducible broken scenarios on kind with a scorecard, so changes to rules or prompts are measured.

## Limitations

- One namespace per session; cluster-scoped problems (node pressure, CNI, DNS) are reported only through their symptoms.
- Automatic fixes are conservative heuristics. Memory values in particular are starting points, not tuning.
- kdiag does not modify nodes, RBAC, Secrets, or create new objects.
- The interactive prompt reads from stdin; answers can be piped, but `--yes` is the only non-interactive approval.

## License

MIT. See [LICENSE](LICENSE).
