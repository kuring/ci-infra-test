repair:
    #!/usr/bin/env bash
    set -euo pipefail
    just --justfile /Users/kuring/github/pytorch/ci-infra/osdc/kind/justfile kind-drain-guard
    NODE=osdc-drain-local-control-plane
    [[ "$(docker inspect "$NODE" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["Config"]["Labels"].get("io.x-k8s.kind.cluster"))' )" == osdc-drain-local ]]
    docker exec "$NODE" curl --cacert /etc/kubernetes/pki/ca.crt --connect-timeout 5 --max-time 10 --fail --silent --output /dev/null https://osdc-drain-local-control-plane:6443/version
    docker exec "$NODE" sh -c '
        set -eu
        grep -q "server: https://192.168.148.3:6443" /etc/kubernetes/kubelet.conf
        test ! -e /etc/kubernetes/kubelet.conf.before-dns-fix
        cp -p /etc/kubernetes/kubelet.conf /etc/kubernetes/kubelet.conf.before-dns-fix
        sed -i "s#server: https://192.168.148.3:6443#server: https://osdc-drain-local-control-plane:6443#" /etc/kubernetes/kubelet.conf
        systemctl restart kubelet
    '
    docker exec "$NODE" grep 'server:' /etc/kubernetes/kubelet.conf
