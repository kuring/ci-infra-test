set shell := ["mise", "exec", "--", "bash", "-euo", "pipefail", "-c"]

install-static:
    #!/usr/bin/env bash
    set -euo pipefail
    source /Users/kuring/github/pytorch/ci-infra/osdc/scripts/mise-activate.sh
    cd /Users/kuring/github/pytorch/ci-infra/osdc
    PYTHONPATH=kind/scripts/python uv run python kind/diagnosis/scripts/python/static-capacity-control.py install-static

restore-capacity-aware:
    #!/usr/bin/env bash
    set -euo pipefail
    source /Users/kuring/github/pytorch/ci-infra/osdc/scripts/mise-activate.sh
    cd /Users/kuring/github/pytorch/ci-infra/osdc
    PYTHONPATH=kind/scripts/python uv run python kind/diagnosis/scripts/python/static-capacity-control.py restore

run-control:
    #!/usr/bin/env bash
    set -euo pipefail
    source /Users/kuring/github/pytorch/ci-infra/osdc/scripts/mise-activate.sh
    cd /Users/kuring/github/pytorch/ci-infra/osdc
    PYTHONPATH=kind/scripts/python uv run python kind/diagnosis/scripts/python/static-capacity-control.py test
