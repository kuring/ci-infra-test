set shell := ["mise", "exec", "--", "bash", "-euo", "pipefail", "-c"]

probe initial="3":
    #!/usr/bin/env bash
    set -euo pipefail
    INITIAL="{{initial}}"
    [[ "$INITIAL" == 0 || "$INITIAL" == 3 ]]
    source /Users/kuring/github/pytorch/ci-infra/osdc/scripts/mise-activate.sh
    cd /Users/kuring/github/pytorch/ci-infra/osdc
    PYTHONPATH=kind/scripts/python uv run python kind/diagnosis/scripts/python/broker-protocol-probe.py --initial-capacity {{initial}}

recover-session:
    #!/usr/bin/env bash
    set -euo pipefail
    source /Users/kuring/github/pytorch/ci-infra/osdc/scripts/mise-activate.sh
    cd /Users/kuring/github/pytorch/ci-infra/osdc
    PYTHONPATH=kind/scripts/python uv run python kind/diagnosis/scripts/python/broker-protocol-probe.py --initial-capacity 0 --poll-seconds 100 --recover-session

refresh-metadata:
    #!/usr/bin/env bash
    set -euo pipefail
    source /Users/kuring/github/pytorch/ci-infra/osdc/scripts/mise-activate.sh
    cd /Users/kuring/github/pytorch/ci-infra/osdc
    PYTHONPATH=kind/scripts/python uv run python kind/diagnosis/scripts/python/broker-protocol-probe.py --initial-capacity 0 --poll-seconds 100 --refresh-metadata

compare-fresh-job:
    #!/usr/bin/env bash
    set -euo pipefail
    source /Users/kuring/github/pytorch/ci-infra/osdc/scripts/mise-activate.sh
    cd /Users/kuring/github/pytorch/ci-infra/osdc
    PYTHONPATH=kind/scripts/python uv run python kind/diagnosis/scripts/python/broker-protocol-probe.py --initial-capacity 0 --poll-seconds 100 --fresh-job
