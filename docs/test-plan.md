# kubeagt test plan

All testing is manual; there is no CI workflow. Results below are from real runs, with the date of each run.

## Test environment

Evals run on a single-node kind cluster (`kubeagt-test`, context `kind-kubeagt-test`, created from
`test/kind-config.yaml`) on the developer machine. The cluster persists between runs. Each case gets a
fresh `kubeagt-eval-<case>` namespace that is deleted afterwards. Before every case the harness checks
that no `kubeagt-eval-*` namespace is left over and that node labels still match the start of the run,
and fails the case with the reason if not. Rules-only mode uses no LLM. Mock mode uses a keyless stand-in
that calls the real read-only kubectl tools and then accepts the rule's candidate fix; it makes no API calls.
Scenario cases approve every fix automatically (`--yes`); human response cases feed scripted answers
to the real prompt instead.

## Table 1: scenario cases

Latest results: rules-only and mock on 2026-10-28 (PDT), commit 00de69b.

| Case | What's broken | Expected fix | How verified | Rules-only | Mock |
|---|---|---|---|---|---|
| 00-healthy | Nothing | No fix proposed | Nothing applied, namespace healthy | PASS | PASS |
| 01-nodeselector | nodeSelector disktype=ssd, no node has it | Remove the nodeSelector | nodeSelector absent on the live Deployment, rollout done, healthy | PASS | PASS |
| 02-probe-port | Redis readiness probe on 5432, Redis listens on 6379 | Probe port 6379 | Live probe port is 6379, pod Ready | PASS | PASS |
| 03-memory-5mi | 5Mi memory request and limit | 128Mi request, 512Mi limit | Live resources match, healthy | PASS | PASS |
| 04-redis-combined | All three bugs above on one Deployment | Three fixes, one at a time | All four field checks on the live Deployment, healthy | PASS | PASS |
| 05-bad-image-tag | Image tag that does not exist | Back to busybox:1.36 | Live image matches, healthy | PASS | PASS |
| 06-service-selector | Service selects app=redis-cache, pods are app=redis | Selector app=redis | Live Service selector, endpoints present | PASS | PASS |
| 07-crashloop-bad-command | Command typo redis-srv | Remove the command override | Live command absent, healthy | PASS | PASS |
| 08-missing-configmap | envFrom a ConfigMap that does not exist | None: detect only, never create a ConfigMap | Detected, nothing applied, stays unhealthy | PASS | PASS |

Totals: rules-only 9/9, mock 9/9.

## Table 2: human response cases

Answers are fed to the real fix prompt in order. When the list runs out the prompt sees end of input,
the same as Ctrl-D. Every case also checks these rules from the flow chart in the audit log: no rule
re-run after n or s, no patch offered twice, a skipped finding never sent to the AI, nothing proposed or
written after q, and rule checks equal one plus the number of applies.

| Case | Answers typed | Expected behavior | What's checked in cluster and audit | Rules-only | Mock |
|---|---|---|---|---|---|
| 10-answer-yes | y | Fix applied and verified | nodeSelector gone; one decision yes, one apply, one verified | PASS (9/9 checks) | PASS (9/9) |
| 11-answer-no | n, y | Declined; the same patch is never offered again, so the spare y is never asked | Deployment spec and generation unchanged; one decision no; one rule check; finding still open | PASS (11/11) | PASS (11/11) |
| 12-answer-skip | s, y | Finding set aside, not sent to the AI, still open at the end | Deployment unchanged; one decision skip; finding open at session end | PASS (11/11) | PASS (11/11) |
| 13-answer-quit | y, q, y | Redis case: first fix lands, session stops at q | nodeSelector gone, generation 2, probe still 5432, memory still 5Mi; decisions yes, quit; nothing written after q | PASS (15/15) | PASS (15/15) |
| 14-answer-invalid | maybe, (empty line), y | Asked again twice, then applied | Two prompt_invalid events, one apply, verified, healthy | PASS (9/9) | PASS (9/9) |
| 15-answer-eof | none (end of input) | Treated as quit, nothing applied | Deployment unchanged; one prompt_eof; decision quit | PASS (11/11) | PASS (11/11) |
| 16-answer-mixed | n, y, s, y | Redis case: selector declined, memory fixed, probe skipped, last y never asked | Selector still redisnode=true, memory 128Mi and 512Mi, probe still 5432; decisions no, yes, skip | PASS (16/16) | PASS (16/16) |

Totals: rules-only 7/7, mock 7/7.

## Table 3: unit test groups

`pytest -q`, run against a fake cluster and scripted AI. 139 tests pass.

| Group | What it covers | Count |
|---|---|---|
| test_kube_safety | Read-only kubectl allowlist, refused writes and flags, write path argv, context and kubeconfig flags | 35 |
| test_agent_llm | Agent loop, caps on steps, tokens and spend, mock provider, OpenAI-compatible client, provider presets | 20 |
| test_rules | Each deterministic rule on good and broken objects | 19 |
| test_fixer | Fix loop: one fix at a time, y, n, s, q, AI feedback, repeat blocking, verification, caps | 18 |
| test_human_responses | Every prompt answer (y, n, s, q, invalid, end of input, mixed) with audit checks, eval checker, pre-case check | 17 |
| test_relay | Relay mode file exchange, allowlist, timeouts, malformed responses | 13 |
| test_cli | CLI commands, offline bundle check, eval safety, scenario files, expectation checks | 8 |
| test_misc | Quantities, redaction, JSON patch, diffs, bundle round trip | 6 |
| test_policy | Policy filter skeleton: hook runs per proposal and allows everything | 3 |

## Not yet tested

A clean install on a fresh machine, and a real cluster with more than one node.
