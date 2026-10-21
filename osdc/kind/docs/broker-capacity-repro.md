# Repo-scoped scale set: queued jobs are not rematched on capacity 0 -> positive

This is a report draft based on a live reproduction on 2026-10-13. It has not been published.

## Environment and scope

- GitHub.com, repository-scoped scale sets on `kuring/ci-infra`, default runner group ID 1.
- Python HTTP client following the `actions/scaleset` v0.3.0 API/session/message protocol.
- Dedicated temporary scale sets, unique labels and temporary PR workflows; no actual runners created.
- ARC and Kubernetes are not involved in this minimal message-routing reproduction.
- All credentials remain local; traces contain only capacities, status codes, job IDs and statistics.

## Reproduction sequence

1. Create a new scale set with a unique name/label and create a message session.
2. Poll its returned messageQueueUrl with `X-ScaleSetMaxCapacity: 0`.
3. Create a PR workflow with three jobs targeting this scale-set name. Confirm the jobs are queued.
4. Poll at capacity 0 once more; the response is HTTP 202 without messages.
5. Change the header to `X-ScaleSetMaxCapacity: 3` and poll repeatedly.
6. Observe HTTP 202, with all three jobs still queued and `totalAssignedJobs = 0`.
7. Without changing those jobs, the scale set, the session or the capacity, create one fresh job
   targeting the same scale-set name.
8. The next poll returns HTTP 200 in about 0.3 seconds with three JobAssigned messages for
   the ORIGINAL jobs. `totalAssignedJobs` becomes 3. The fresh job is still waiting.

Client code checks the prepared request header before sending and response.request afterward.
It acknowledges received messages but does not generate JIT configs or create actual runners.
Thus the evidence concerns broker assignment; jobs remain queued awaiting a real worker.

## Controls

- Advertising 3 BEFORE creating jobs delivers all three messages immediately (about 0.3 seconds).
- Closing the zero-start session and opening a new one whose first poll advertises 3 did not
  recover the existing jobs: two complete polls still returned 202, statistics stayed at zero.
- Updating scale-set metadata with the same parameters did not recover the jobs.
- A successful update request adding an unused alias while preserving the requested label
  also did not recover the jobs.
- An earlier ARC-based experiment observed recovery when an unrelated-label ordinary runner
  came online. That ordinary runner did not execute the jobs; ARC runners did.

## Evidence

Positive-capacity control:
https://github.com/kuring/ci-infra/actions/runs/38353100158

Session-recreation control:
https://github.com/kuring/ci-infra/actions/runs/38354470160

Metadata controls:
https://github.com/kuring/ci-infra/actions/runs/38355224043

Old jobs:
https://github.com/kuring/ci-infra/actions/runs/38356239517

Fresh job that triggered dispatch of the old jobs:
https://github.com/kuring/ci-infra/actions/runs/38356513110

Old job IDs: 115159684817, 115159684936, 115159684994.
Fresh job ID: 115160565518.
At 2026-10-13 06:24:38 UTC, the response contained three distinct old jobIds and
`totalAssignedJobs: 3`; the prepared request advertised capacity 3 throughout.

## Expected behavior / question

Should increasing the advertised capacity from 0 to a positive number cause existing queued
jobs to be rematched without requiring a new job enqueue or an ordinary runner online event?
Is an additional supported API operation required for that transition?

The observation suggests a missing rematch/wakeup on capacity change. It does not identify
an internal GitHub cache, queue implementation or particular server code path.
The reproduction is repository scoped; no organization-scoped staging claim is made.

## Local reproduction

From the local ci-infra `osdc/` checkout with the migrated kind directory:

```zsh
HTTPS_PROXY=http://127.0.0.1:13660 NO_PROXY=localhost,127.0.0.1 \
  mise exec -- just --justfile kind/diagnosis/recipes/broker-protocol.just compare-fresh-job
```

The local helper creates and cleans up temporary PRs, branches, sessions and scale sets.
Workflows are cancelled intentionally because this experiment tests messages rather than
worker execution. Do not interpret a cancelled workflow as a successful end-to-end run.

Protocol reference: https://github.com/actions/scaleset/tree/v0.3.0
