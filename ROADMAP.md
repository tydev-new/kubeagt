# Roadmap

kubeagt today runs a one-fix-at-a-time loop: diagnose, propose, server dry run, ask you, apply, verify, audit. This page lists what is known to be weak and what is planned next. Each next step will become a GitHub issue.

## Known limitations

These come from real eval runs (see [docs/test-plan.md](docs/test-plan.md)).

- **Live evals score 8/9** for both Grok 4.7 and DeepSeek v4.1 Flash through OpenRouter.
- **Both models hid the symptom in `08-missing-configmap`.** They set `configMapRef.optional: true`, so the pod became Ready while running without its config. Verify passed because it checks only rollout status and the rules, not app logs. The scorecard counts it as a failure.
- **The policy filter is a skeleton.** It runs on every proposal and allows everything (see [docs/policy-plan.md](docs/policy-plan.md)).
- **The log check in verify and the standard audit schema are designed, not implemented** (see [docs/design.html](docs/design.html)).
- **Human-response evals** (y, n, skip, quit, invalid input, Ctrl-D, mixed) pass in rules-only and mock modes but have not been run with a live model.
- **Scenario 08 uses a silent `busybox` app** that never reads its config, so no log check could catch the bad fix.
- **`00-healthy` is flaky.** `probe-failing` can fire on readiness Unhealthy events during Redis startup, even after the pod is Ready.
- **Tested only on a single-node kind cluster.** No multi-node or managed cluster runs yet.

## Next steps

Priority: P1 next, P2 soon, P3 later. Size: S is under a day, M is a few days, L is a week or more.

| # | Item | Why | Priority | Size | Docs |
|---|---|---|---|---|---|
| 1 | Log check in verify: before/after log diff with pass, warn, or fail. Ship it as warn-only first. | Verify passed on a pod that was Ready but broken. | P1 | M | [design](docs/design.html) |
| 2 | Contextual warning for `optional: true` on a missing ConfigMap or Secret. Treat env and envFrom as worse than volumes, require evidence that the config is optional, never auto-approve with `--yes`, and require the log check. | Both live models used it to hide a missing ConfigMap. It is sometimes legitimate, so warn rather than block. | P1 | M | [policy plan](docs/policy-plan.md) |
| 3 | Allowed patch paths per finding type for the AI. Any other path needs explicit approval and is excluded from `--yes`. | Stops off-target and symptom-hiding patches by design. | P1 | M | [policy plan](docs/policy-plan.md) |
| 4 | Realistic app for scenario 08 that logs an error and exits when its config is missing. | Makes the case honest and gives the log check a signal. | P1 | S | [test plan](docs/test-plan.md) |
| 5 | State the AI's goal as "restore intended function, not silence the errors" in the prompt. | Prompt wording affects how often agents take shortcuts. | P1 | S | [design](docs/design.html) |
| 6 | Reuse kube-score, kube-linter, and Kyverno Pod Security policies as before/after checks on the patched object. Build only what they do not cover: absurd probe thresholds, sleep commands, scope, sizing jumps. | Most planned checks already exist as maintained rules. | P2 | M | [policy plan](docs/policy-plan.md) |
| 7 | Durability step in verify: restart the pod, or re-check after a watch window, before marking a fix verified. | A fix can look good at first and fail later. | P2 | M | [design](docs/design.html) |
| 8 | Evals both ways: the reference fix passes, doing nothing fails, and a scripted symptom-hiding fix is blocked or warned. | Proves the scorecard and safeguards can actually fail. | P2 | M | [test plan](docs/test-plan.md) |
| 9 | Standard audit schema: CloudEvents envelope, OpenTelemetry `k8s.*` and `gen_ai.*` attribute names, and patches applied with `--field-manager=kubeagt`. | Consistent, queryable logs that match the API server audit log. | P2 | M | [design](docs/design.html) |
| 10 | Fix the `00-healthy` probe-failing flake by ignoring Unhealthy events once the pod is Ready. | Removes a false failure from the scorecard. | P2 | S | [test plan](docs/test-plan.md) |
| 11 | Remaining policy-plan checks. | Covers the rest of the planned anti-patterns. | P3 | L | [policy plan](docs/policy-plan.md) |
| 12 | Human-response evals with live models. | The y, n, skip, and quit paths are untested against real model behavior. | P3 | S | [test plan](docs/test-plan.md) |
| 13 | Multi-node and real-cluster testing. | kind hides scheduling, network, and permission issues. | P3 | L | [test plan](docs/test-plan.md) |
| 14 | Cross-session outcome memory: remember which fixes worked or were rejected. | Avoids repeating bad proposals across sessions. | P3 | L | [design](docs/design.html) |

## Prior art

Tools and research that informed these items.

- k8sgpt operator auto remediation: https://github.com/k8sgpt-ai/k8sgpt-operator/blob/main/AUTO_REMEDIATION.md
- kubectl-ai: https://github.com/GoogleCloudPlatform/kubectl-ai
- HolmesGPT remediation MCP: https://holmesgpt.dev/latest/data-sources/builtin-toolsets/kubernetes-remediation-mcp/
- kagent human in the loop: https://kagent.dev/docs/kagent/1.x/agents/human-in-the-loop/
- Komodor approvals: https://docs.komodor.com/security-and-governance/controls/approvals
- Kyverno policies: https://kyverno.io/policies/
- Gatekeeper library: https://open-policy-agent.github.io/gatekeeper-library/website/
- Pod Security Standards: https://kubernetes.io/docs/concepts/security/pod-security-standards/
- Argo Rollouts analysis: https://argo-rollouts.readthedocs.io/en/stable/features/analysis/
- Keptn SLO: https://v1.keptn.sh/docs/1.0.x/reference/files/slo/
- Harness log verification: https://developer.harness.io/docs/continuous-delivery/verify/cv-results/interpret-log-results/
- Incident-Arena: https://arxiv.org/abs/2610.00648
- ImpossibleBench: https://arxiv.org/abs/2510.20270
- CloudEvents spec: https://github.com/cloudevents/spec/blob/main/cloudevents/spec.md
- OpenTelemetry k8s attributes: https://opentelemetry.io/docs/specs/semconv/registry/attributes/k8s/
- Kubernetes optional ConfigMaps: https://kubernetes.io/docs/tasks/configure-pod-container/configure-pod-configmap/

## Contributing

To work on an item, open an issue that references its number in this roadmap (for example "Roadmap #4: realistic app for scenario 08"), then link your pull request to that issue.
