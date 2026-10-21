set shell := ["mise", "exec", "--", "bash", "-euo", "pipefail", "-c"]

delete:
    #!/usr/bin/env bash
    set -euo pipefail
    umask 077
    source /Users/kuring/github/pytorch/ci-infra/osdc/scripts/mise-activate.sh
    cd /Users/kuring/github/pytorch/ci-infra/osdc
    just --justfile kind/justfile kind-drain-guard
    uv run python - <<'PY'
    import json,subprocess
    from pathlib import Path
    ctx='kind-osdc-drain-local'
    for path in (Path('kind/.state/kubeconfig'),Path.home()/'.kube/config'):
        obj=json.loads(subprocess.run(['kubectl','--kubeconfig',str(path),'config','view','-o','json'],capture_output=True,text=True,check=True).stdout)
        entry=next(c for c in obj['contexts'] if c['name']==ctx)
        assert entry['context']['cluster']==ctx and entry['context']['user']==ctx
        server=next(c['cluster']['server'] for c in obj['clusters'] if c['name']==ctx)
        if path.name=='kubeconfig': expected=server
        else: assert server==expected
    PY
    cp "$HOME/.kube/config" "kind/.state/kubeconfig-before-cluster-delete-$(date -u +%Y%m%dT%H%M%SZ)"
    helm uninstall osdc-kind-drain --namespace arc-runners --kubeconfig kind/.state/kubeconfig --kube-context kind-osdc-drain-local --wait --timeout 180s
    kubectl --kubeconfig kind/.state/kubeconfig --context kind-osdc-drain-local wait --for=delete autoscalingrunnerset/osdc-kind-drain -n arc-runners --timeout=180s
    kind delete cluster --name osdc-drain-local --kubeconfig "$HOME/.kube/config"
    uv run python - <<'PY'
    import json,subprocess
    from pathlib import Path
    ctx='kind-osdc-drain-local'
    home=Path.home()/'.kube/config'
    obj=json.loads(subprocess.run(['kubectl','--kubeconfig',str(home),'config','view','-o','json'],capture_output=True,text=True,check=True).stdout)
    assert not any(c['name']==ctx for c in obj.get('contexts',[]))
    if obj.get('current-context')==ctx:
        subprocess.run(['kubectl','--kubeconfig',str(home),'config','unset','current-context'],check=True)
    Path('kind/.state/kubeconfig').unlink(missing_ok=True)
    print('Removed only the deleted kind cluster connection; local scripts and PAT retained')
    PY

stop-owned-test-proxy:
    #!/usr/bin/env bash
    set -euo pipefail
    source /Users/kuring/github/pytorch/ci-infra/osdc/scripts/mise-activate.sh
    uv run python - <<'PY'
    import os,signal,subprocess,time
    pid=11078
    cmd=subprocess.run(['ps','-p',str(pid),'-o','command='],capture_output=True,text=True).stdout.strip()
    if cmd:
        assert '/bin/pproxy ' in cmd and '-l http://127.0.0.1:13660 -r socks5://127.0.0.1:13659' in cmd, 'PID is no longer the owned test proxy'
        os.kill(pid,signal.SIGTERM)
        time.sleep(1)
        print('Stopped owned test HTTP proxy on 13660')
    else:
        print('Owned test HTTP proxy already stopped')
    PY
