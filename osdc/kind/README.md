# 在独立 Kind 环境验证 ci-infra drain-test

本目录属于 `ci-infra-test`，用于适配 `ci-infra` 的本地验证环境。上游代码保持不变。
当前对照版本为 `ci-infra` 的 `06c039c1de9eb7983f0b5415046a28deb28d612d`
（`test: add OSDC drain and undrain slow test`）。

`kind-drain` 直接导入上游 `DrainTest`、`run_controller`、断言和工作流模板，
并通过生成的 consumer justfile 导入上游 drain/resume recipe，仅覆盖 kubeconfig 连接。
本地适配仓库、runner 标签、Default runner group 和配置路径，不复制或修改上游 drain-test。

## 路径与依赖

默认目录布局：`ci-infra-test` 和 `ci-infra` 是同级目录。
不符合该布局时，将 `OSDC_UPSTREAM` 设置为目标 checkout 的 **osdc 目录绝对路径**。
切换 checkout 后重新执行 render；不要在同一测试进程中切换上游版本。
render 同时把上游 `mise.toml` 写入 consumer 状态目录，使上游 recipe 从
`OSDC_ROOT` 激活工具时使用相同配置；仅为子进程信任该生成目录。

以下命令均从本目录执行：

```bash
cd /Users/kuring/github/kuring/ci-infra-test/osdc/kind
export OSDC_UPSTREAM=/Users/kuring/github/kuring/ci-infra/osdc
command -v mise just kind docker kubectl helm uv gh jq
mise --cd "$OSDC_UPSTREAM" exec -- python --version
```

Python 依赖和版本来自上游项目，`uv run --frozen` 使用其锁文件。
默认虚拟环境和 uv 缓存位于本目录 `.state/`，不会写入上游项目。
首次使用需能访问 Python 包仓库。`.state/` 已通过本项目 `.gitignore` 排除。

已有上游虚拟环境且暂时不能联网时，可显式只读复用：

```bash
export UV_PROJECT_ENVIRONMENT="$OSDC_UPSTREAM/.venv"
export UV_NO_SYNC=1 UV_OFFLINE=1
```

正常联网准备本地依赖时，取消这些覆盖变量即可。

## 离线回归与预览

```bash
just kind-drain-unit
just kind-drain test --dry-run
```

`kind-drain-unit` 运行上游 drain-test 的单元测试及本地适配回归；不访问 GitHub 或集群。
其中上游进程回收测试需要操作系统允许 `ps` 和进程组信号。
`--dry-run` 不读取 PAT，不访问集群，不创建 GitHub 对象，不生成 `.state` 配置。
它仍需要已安装的 Python 依赖。

## 创建与部署

| 配置 | 值 |
| --- | --- |
| 集群 / context | `osdc-drain-local` / `kind-osdc-drain-local` |
| ARC fork | `0.14.1-jeanschmidt.20` |
| GitHub 仓库 / scale set | `kuring/ci-infra` / `osdc-kind-drain` |
| 容量 | min 0 / max 3，capacity-aware 开启 |
| 命名空间 | `arc-systems` / `arc-runners` |

先启动 Docker 引擎，确认 `docker info` 成功。仅在集群不存在时创建：

```bash
kind get clusters
just kind-drain-create
export KUBECONFIG="$PWD/.state/kubeconfig"
just kind-drain-guard
```

现有网络配置使用 `http://host.docker.internal:13660` 访问外网。
需要代理时，先启动本机 SOCKS5 端口 13659，再在单独终端保持 `just kind-drain-proxy` 运行。
宿主机访问 GitHub/镜像仓库时设置 `HTTP_PROXY`、`HTTPS_PROXY` 为
`http://127.0.0.1:13660`，并保留本地地址的 `NO_PROXY`。
在可直连 GitHub/GHCR 的 ECS 上，创建和部署前设置以下显式选项；不改变镜像版本：

```bash
export OSDC_KIND_DIRECT_NETWORK=true
export OSDC_KIND_ARC_PUBLIC_IMAGES=true
```

前者取消 Kind 节点代理并忽略 `.state/proxy.yaml`，后者使用同版本官方
`ghcr.io/jeanschmidt/gha-runner-scale-set-controller` 和 `ghcr.io/actions/actions-runner` 镜像。
整个 create/controller/install/probe/test 过程保留这些环境变量。

Pod 代理配置读取已有的 `.state/proxy.yaml`（chart 的 `proxy` 配置结构）；
`noProxy` 需包含 Kubernetes 服务、`.svc`、`.cluster.local`、节点名称和集群网段。
本地网络不需要代理时，创建集群前调整本项目 `justfile` 中的节点代理配置，并移走旧 proxy.yaml。

专用细粒度 PAT 放在 `.state/github-token`：普通文件、权限 `600`，目录权限 `700`。
PAT 仅授权 `kuring/ci-infra`，需要 Administration、Actions、Contents、Pull requests、Workflows
读写权限及 Metadata 读取权限。不要提交或打印凭证。

```bash
just kind-drain render
just kind-drain-controller
just kind-drain install
kubectl -n arc-systems get pods
kubectl -n arc-runners get autoscalingrunnersets,pods
```

安装会向 GitHub 注册 scale set，不创建测试 PR。guard 会核对独立 kubeconfig、context、
节点名称及所有权标签，避免连接到 AWS 或其他集群。

## 运行与验收

同一集群一次只运行一个测试，期间不要 install、reset 或重启 listener。

```bash
# 先验证上游 recipe 的容量切换：3 → 0 → 3，不创建 GitHub 任务
just kind-drain probe

# 真实 E2E：临时分支、PR、Actions 任务，结束时清理
just kind-drain test --start-timeout 600 --finish-timeout 600
```

可用参数还有 `--drain-timeout`（默认 180 秒）、`--observe-seconds`（默认 60 秒）。
旧 `--simulate-drain-noop` 参数在上游已无对应实现，已移除，避免无效的反向控制。

通过标准保持上游语义：inflight 在 drain 后成功完成；maxRunners 与 listener advertised
capacity 同时归零；三个 held 任务在 60 秒观察期间保持 queued 且没有 runner；
resume 后三个 held 全部成功，runner group 正确，资源恢复并清理成功。退出码必须为 0。
本地预览、离线单测或容量 probe 通过，都不代表真实 E2E 通过。

上游 slow-tests 会等待 load、compactor、janitor 结束才运行 drain-test。
本地 `kind-slow-test` 是另一个集群（`osdc-slow-local`）的 load/compactor/janitor 测试入口，
没有自动包含 `kind-drain`；本次目标使用上面独立的 drain 环境与命令。

中断后检查恢复结果；强杀进程无法保证 finally 执行，需手工检查临时 PR、分支和工作流。
恢复本地容量无需 PAT：

```bash
just kind-drain restore
```

## 验证状态与已知限制（2026-10-21）

本次已修复迁移后路径失效、旧工作流模块导入、consumer 配置路径和独立 slow-tests
入口的路径假设，并加入实际执行上游配置解析脚本的回归测试。

正式 `ci-infra` 测试现已按用户选择的方案 A 增加 #4715 绕过：保留原始 inflight/held
任务，仅在启动及 resume 后、目标容量为正时提交有界的新任务流量唤醒旧队列。
每阶段最多 8 次，间隔至少 15 秒；drain 观察窗口和恢复清理不发送唤醒。

最新一次远程全链路复验已通过：ECS 全量 `just test`、213 项相关单测、ARC 部署、容量
probe 与真实 drain E2E 均成功；启动用了 3 次唤醒，resume 后用了 2 次，四个原任务均
success、job ID 不变。原始 [workflow 39584402848](https://github.com/kuring/ci-infra/actions/runs/39584402848)
成功，脚本退出 0，配置与容量恢复正常，临时 GitHub 资源已清理。
这验证了有新增流量时的恢复，不代表 #4715 的容量单独恢复路径已修复，也不替代 AWS staging 验收。

本机 registry 403 在远程未复现，OpenTofu 初始化与校验通过。远程全量 lint 13/14 通过，
仅剩未修改的 Dockerfile DL3066；修改文件 Ruff、格式与生成工作流 actionlint 均通过。
详细时间线、环境修复、证据与清理结果见 [本轮验证记录](docs/current-validation.md)。

历史材料中的旧绝对路径和执行命令仅作排障参考，请使用本页当前入口：
[验证历史](docs/validation-history.md)、[路由诊断](docs/routing-report.zh.md)、
[broker 复现记录](docs/broker-capacity-repro.md)。
