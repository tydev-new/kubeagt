# Anti-pattern policy filter: plan

Some fixes make an error go away without solving anything, or solve it by doing damage. The policy
filter is meant to catch those before the server dry run and before you are asked to approve.

## Status

Skeleton only. `src/kubeagt/policy.py` defines the `PolicyCheck` interface
(`check(proposal, live_object, context)` returns allow, or block or warn with a reason) and an empty
registry. The fix loop calls it once per proposal, between the proposal and the dry run, and writes a
`policy_check` event to `audit.jsonl`. With the registry empty every proposal is allowed, so behavior
is unchanged and the flow chart in `docs/design.html` is unchanged. A check that blocks would record
the proposal as blocked, tell the AI why in the session history, and go back to "Any errors left?"
without re-running the rules, the same as a rejected dry run.

## Planned categories

| Category | Example the filter should catch | Planned behavior | Status |
|---|---|---|---|
| Hiding the symptom: removing probes | Delete the readinessProbe so a pod with a wrong probe port shows Ready | Block | planned |
| Hiding the symptom: absurd thresholds | Set failureThreshold to 1000 or initialDelaySeconds to 3600 | Block | planned |
| Hiding the symptom: removing limits | Remove resources.limits to stop an OOMKill instead of sizing memory | Warn | planned |
| Weakening security: privileged | Set securityContext.privileged to true | Block | planned |
| Weakening security: host access | Turn on hostNetwork, hostPID, hostIPC, or a hostPort | Block | planned |
| Weakening security: hostPath | Add a hostPath volume | Block | planned |
| Weakening security: root | Set runAsUser to 0, runAsNonRoot to false, or allowPrivilegeEscalation to true | Block | planned |
| Weakening security: capabilities | Add capabilities such as SYS_ADMIN or NET_ADMIN | Block | planned |
| Destructive: scale to 0 | Set replicas to 0 so a crash-looping pod stops reporting errors | Block | planned |
| Destructive: selector orphaning | Change a Deployment selector or pod labels so existing pods or Services no longer match | Block | planned |
| Destructive: PVC shrink | Lower a PVC storage request | Block | planned |
| Reaching past the problem: off-target resources | Patch an object that is not named in any open finding | Warn | planned |
| Reaching past the problem: other namespaces | Patch a resource outside the session namespace | Block | planned |
| Reaching past the problem: system namespaces | Any change in kube-system, kube-public, or kube-node-lease | Block | planned |
| Swapping the workload: image repo change | Replace redis:7.2 with some-other-registry/redis or a different image | Block (a tag change in the same repo is allowed) | planned |
| Swapping the workload: sleep commands | Replace the command with sleep infinity so the container stops crashing | Block | planned |
| Sizing jumps | Raise memory from 128Mi to 64Gi, or CPU by more than a set factor | Warn | planned |
| Secret leaks | Put a password or token as a literal env value or in a ConfigMap | Block | planned |

## Order of work

1. Hiding the symptom, scale to 0, sleep commands, and security checks first, since they are the most
   likely bad fixes from a model trying to make an error disappear.
2. Then scope checks (namespaces, off-target objects), then sizing and image checks.
3. Each check gets unit tests with a good fix and a bad fix, and an eval case where a scripted AI
   proposes the bad fix and the session must block it.
4. Add the filter to the flow chart beside the dry run once the first checks ship.
