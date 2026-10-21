prefetch:
    #!/usr/bin/env bash
    set -euo pipefail
    just --justfile /Users/kuring/github/pytorch/ci-infra/osdc/kind/justfile kind-drain-guard
    docker exec osdc-drain-local-worker crictl pull mirror.aone.alibaba-inc.com/mirrors/ghcr.io/actions/actions-runner:2.338.0
