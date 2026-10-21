set shell := ["mise", "exec", "--", "bash", "-euo", "pipefail", "-c"]

apply:
    #!/usr/bin/env bash
    set -euo pipefail
    source /Users/kuring/github/pytorch/ci-infra/osdc/scripts/mise-activate.sh
    just --justfile /Users/kuring/github/pytorch/ci-infra/osdc/kind/justfile kind-drain-guard
    kubectl --kubeconfig /Users/kuring/github/pytorch/ci-infra/osdc/kind/.state/kubeconfig --context kind-osdc-drain-local apply -f /Users/kuring/github/pytorch/ci-infra/osdc/kind/.state/diagnosis/classic-active.json

cleanup:
    #!/usr/bin/env bash
    set -euo pipefail
    source /Users/kuring/github/pytorch/ci-infra/osdc/scripts/mise-activate.sh
    just --justfile /Users/kuring/github/pytorch/ci-infra/osdc/kind/justfile kind-drain-guard
    KUBE=/Users/kuring/github/pytorch/ci-infra/osdc/kind/.state/kubeconfig
    for RESOURCE in pod/osdc-classic-diagnostic secret/osdc-classic-diagnostic-registration; do
        DATA=$(kubectl --kubeconfig "$KUBE" --context kind-osdc-drain-local get "$RESOURCE" -n arc-runners --ignore-not-found -o json)
        if [[ -n "$DATA" ]]; then
            python3 -c 'import json,sys; o=json.load(sys.stdin); sys.exit(0 if o["metadata"].get("labels",{}).get("osdc.io/drain-kind-diagnostic")=="classic-routing" else "Not the owned diagnostic resource")' <<< "$DATA"
            kubectl --kubeconfig "$KUBE" --context kind-osdc-drain-local delete "$RESOURCE" -n arc-runners --wait=true --timeout=90s
        fi
    done

start:
    #!/usr/bin/env bash
    set -euo pipefail
    source /Users/kuring/github/pytorch/ci-infra/osdc/scripts/mise-activate.sh
    just --justfile /Users/kuring/github/pytorch/ci-infra/osdc/kind/justfile kind-drain-guard
    KUBE=/Users/kuring/github/pytorch/ci-infra/osdc/kind/.state/kubeconfig
    kubectl --kubeconfig "$KUBE" --context kind-osdc-drain-local get pod/osdc-classic-diagnostic -n arc-runners -o json \
        | python3 -c 'import json,sys; o=json.load(sys.stdin); sys.exit(0 if o["metadata"].get("labels",{}).get("osdc.io/drain-kind-diagnostic")=="classic-routing" else "Not the owned diagnostic resource")'
    kubectl --kubeconfig "$KUBE" --context kind-osdc-drain-local exec -n arc-runners osdc-classic-diagnostic -- /bin/bash -c '[[ -f /tmp/diag-configured ]] && touch /tmp/diag-start'

probe-routing:
    #!/usr/bin/env bash
    set -euo pipefail
    source /Users/kuring/github/pytorch/ci-infra/osdc/scripts/mise-activate.sh
    cd /Users/kuring/github/pytorch/ci-infra/osdc
    PYTHONPATH=kind/scripts/python uv run python kind/diagnosis/scripts/python/routing-stages-probe.py
