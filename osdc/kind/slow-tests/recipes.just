# Standalone slow-test entrypoint; also imported by osdc/kind/justfile.
SLOW_LOCAL := parent_directory(source_directory())
SLOW_OSDC := env_var_or_default("OSDC_UPSTREAM", SLOW_LOCAL / "../../../ci-infra/osdc")
SLOW_SCRIPT := source_directory() / "scripts/python/kind_slow.py"

kind-slow action="guard" *args:
    UV_PROJECT_ENVIRONMENT="{{SLOW_LOCAL}}/.state/venv" UV_CACHE_DIR="{{SLOW_LOCAL}}/.state/uv-cache" mise --cd "{{ SLOW_OSDC }}" exec -- uv run --frozen --project "{{SLOW_OSDC}}" "{{ SLOW_SCRIPT }}" "{{ action }}" {{ args }}

kind-slow-render: (kind-slow "render")

kind-slow-host-limits *args: (kind-slow "host-limits" args)

kind-slow-create: (kind-slow "create")

kind-slow-deploy: (kind-slow "deploy")

kind-slow-preload: (kind-slow "preload")

kind-slow-cache-uv-public: (kind-slow "cache-uv-public")

kind-slow-install-arc: (kind-slow "install-arc")

kind-slow-load *args: (kind-slow "load" args)

kind-slow-compactor: (kind-slow "compactor")

kind-slow-janitor: (kind-slow "janitor")

kind-slow-test *args: (kind-slow "test" args)

kind-slow-restore *args: (kind-slow "restore" args)

kind-slow-cleanup-github: (kind-slow "cleanup-github")
