# 本地 slow-tests：kind + GitHub Actions

对应 `.github/workflows/_osdc-slow-tests.yml` 的 load-test、compactor 和 janitor 三项。
无需 AWS；测试编排、控制器和任务 Pod 在本机运行，真实任务分配使用 GitHub Actions。

所有命令从 `osdc/kind/` 执行。本目录随 `osdc/kind/` 一起由当前 checkout 的
`.git/info/exclude` 排除，不修改共享 CI、`clusters.yaml` 或生产部署入口。

## 范围

| 项目 | 实际验证 | 不覆盖 |
| --- | --- | --- |
| load | 原生 CPU 架构检查、checkout pytorch、真实 ARC runner 执行、所有矩阵任务完成；通过 Pod 和调度事件确认 runner 属于本地 worker | 默认 400 任务的生产规模、多种 CPU/GPU 类型 |
| compactor | 真实控制器的污点及不驱逐运行中 Pod、节点年龄门槛、限速、Pending Pod 引发的解污点与 fleet 冷却、容量预留、SIGTERM 清理 | Karpenter 自动创建/删除节点、WhenEmpty、EC2 |
| janitor | 直接运行上游五项 E2E：阈值内不清理、触发清理、优先清理大镜像、运行中镜像保留、指标 | EKS 节点磁盘和 Harbor/S3 |

compactor 用最小 `NodePool` CRD 和标签发现三个固定 worker；该 CRD 只有发现元数据，
不提供节点供给或回收能力。测试使用 `10m CPU / 16Mi` 的常驻 Pod，不伪造 Node 的资源容量。
运行中 Pod 的验证明确绑定 worker；Pending Pod 场景使用真实调度器。

## 本地配置

| 配置 | 值 |
| --- | --- |
| kind 集群 / context | `osdc-slow-local` / `kind-osdc-slow-local` |
| 拓扑 | 1 control-plane + 3 固定 worker |
| NodePool / fleet | `kind-slow` |
| GitHub 测试仓库 | `kuring/ci-infra` |
| ARC scale set | `osdc-kind-slow`，最大 runner 数 3，容量感知开启 |
| 默认负载 | 12 个任务，最多 3 个并发；支持 1–100 个任务 |
| 凭证 | `GH_TOKEN` 优先，否则读取既有 `.state/github-token`（普通文件，权限 600） |
| 状态 | `.state/slow-tests/`（目录权限 700） |
| kubeconfig | `.state/slow-tests/kubeconfig`（文件权限 600） |

使用独立 kubeconfig，每次操作检查 context、全部节点名称、归属标签和 Ready 状态。
不切换全局 kubeconfig，不改变既有 `osdc-drain-local` 集群。
同一集群的入口使用文件锁，一次只运行一个操作。

## 准备和部署

已安装 Docker/OrbStack、kind、mise、just，并准备上级 `uv` 依赖。
沿用现有 ARC chart、镜像版本和 `.state/proxy.yaml`；不修改工具或依赖版本。
本地 compactor 的 uv 构建镜像使用
`mirror.aone.alibaba-inc.com/mirrors/ghcr.io/astral-sh/uv:0.10.12`。

```zsh
cd /Users/kuring/github/kuring/ci-infra/osdc/kind

# 离线检查：不访问 Kubernetes 或 GitHub
uv run --frozen --offline pytest slow-tests/scripts/python -q

# 生成配置：读取上游 kustomize，不访问集群
just kind-slow-render

# 创建专用集群；仅重用同名且通过归属检查的集群
just kind-slow-create

# 本地构建真实 compactor / janitor 镜像并加载到 kind，部署控制器
just kind-slow-deploy

# 在专用集群安装 ARC，并在 kuring/ci-infra 注册 osdc-kind-slow
just kind-slow-install-arc
```

在 `just kind-slow-create` 成功后，如果要手动运行 `kubectl` 查看集群，
从 `osdc/kind/` 设置当前终端的 `KUBECONFIG`：

```zsh
export KUBECONFIG="$(pwd)/.state/slow-tests/kubeconfig"

# 应输出 kind-osdc-slow-local
kubectl config current-context

# 应看到 1 个 control-plane 和 3 个 worker，均为 Ready
kubectl get nodes
```

这里使用绝对路径，切换目录后仍然有效；新开终端时需要重新设置。
该设置只影响当前终端及其子进程，不写入 `~/.kube/config`。
`just kind-slow-*` 入口会自动使用专用 kubeconfig，无需手动设置环境变量；
设置 `KUBECONFIG` 也不会改变这些入口的目标集群。

镜像构建和镜像拉取需要网络。如果使用本机 `127.0.0.1` HTTP 代理，创建步骤自动把
节点代理地址转换为 `host.docker.internal`，保留原 NO_PROXY 并加入内部域名。
已有 `.state/proxy.yaml` 用于 ARC 的 GitHub 请求。使用此前的 13660 转接配置时，
确保转接仍运行；本入口不启动或停止该共享代理进程。

Linux 多节点 kind 可能耗尽宿主机 inotify 实例上限，kubelet 日志会出现
`inotify_init: too many open files`。只有需要并明确决定修改宿主机限制时才执行：

```zsh
# root / Linux：只修改运行时值，ECS 重启后恢复；不降低已有的更高上限
just kind-slow-host-limits

# 明确需要重启后保留时，才使用此选项
just kind-slow-host-limits --persistent
```

原值保存在 `.state/slow-tests/host-limits-before.json`。上限至少为 512；
默认不写系统配置，`--persistent` 写入专用 `/etc/sysctl.d/99-osdc-kind-slow.conf`，
拒绝覆盖内容不同的既有文件。`create` 不会自动修改宿主机设置。
创建失败时保留本轮节点用于诊断；已有节点恢复健康后可以重跑 `create`。

`deploy` 先由宿主机客户端预载构建镜像，再交给 Docker 构建。
缓存归档必须只有预期镜像且架构一致；已缓存的上游 uv `0.10.12` 可作为
指定内网名称的本地别名，日志会明确显示来源，未缓存时从指定源下载。

ECS 无法访问内网镜像域名时，可以显式执行以下配置；不改变镜像版本：

```zsh
# 下载官方 uv 0.10.12，校验后缓存为 Dockerfile 指定的内网名称
just kind-slow-cache-uv-public

# 只对本次安装使用官方 ARC controller / runner 镜像
OSDC_KIND_ARC_PUBLIC_IMAGES=true just kind-slow-install-arc
```

该环境变量不改写 `helm/controller.yaml`，生成的 ECS values 保存在专用状态目录。

需要下载的镜像如下（按 worker 的原生架构获取）：

| 用途 | 镜像 |
| --- | --- |
| kind 节点 | `kindest/node:v1.33.1`（ECS 现有 kind 0.29.0 的默认版本） |
| compactor 构建 | `python:3.12.9-slim`、`ghcr.io/astral-sh/uv:0.10.12`（缓存为指定内网名称） |
| janitor 构建 | `public.ecr.aws/docker/library/python:3.12-alpine` |
| ARC controller / listener | `ghcr.io/jeanschmidt/gha-runner-scale-set-controller:0.14.1-jeanschmidt.20` |
| ARC runner / load job | `ghcr.io/actions/actions-runner:2.338.0` |
| compactor 测试 Pod | `public.ecr.aws/docker/library/alpine:3.21` |
| janitor 保活 Pod | `registry.k8s.io/pause:3.10` |
| janitor 清理样本 | `docker.io/library/alpine:3.18`、`:3.19`、`:3.20`，`docker.io/library/busybox:1.36`、`:1.37` |

kind 自带的系统组件镜像随节点镜像提供。另需下载固定版本的 ARC OCI Helm charts，
以及 janitor 构建使用的 crictl v1.32.0 二进制。compactor / janitor 最终镜像由本地构建，
通过 `kind load` 导入，不需要公网推送。

`deploy` 会根据 worker 架构构建镜像、预载 E2E 的 Alpine 镜像。
compactor 常态为 `dry-run=true`，janitor 常态阈值为 `9999Gi`，防止闲置时干预后续实验。
E2E 临时修改配置，结束后恢复本地基线。
compactor 测试沿用上游的隔离方式：暂时将专用 `osdc-kind-slow` 的 min/max runners
设为 0，等待该 scale set 的 runner 和占位 Pod 退出，结束后恢复原值。
原值保存在 `.state/slow-tests/arc-pause-before.json`；若进程被强杀，可重新执行
ARC 安装入口恢复该 profile。

janitor 保留上游五项测试，通过本地 pytest 插件显式载入 kind 的 CA 和客户端证书。
这解决当前 lightkube/httpx 组合在证书认证路径上的兼容问题，保留服务器证书校验，
不创建额外 ServiceAccount 或修改共享依赖。
janitor 测试结束后，入口先恢复高阈值、停止该 DaemonSet 的旧 Pod，再导入本地
janitor / Alpine 镜像并恢复 worker 选择器，避免 containerd 清理缓存后无法拉取本地镜像。

## 执行

```zsh
# 预览 load 工作流；不访问 GitHub、不创建 PR，需要专用 kind 节点已 Ready
just kind-slow-load --dry-run

# 完整本地测试：load → compactor → janitor，串行执行
just kind-slow-test --jobs 12 --timeout 1800

# 也可以单独运行
just kind-slow-load --jobs 6 --timeout 900
just kind-slow-compactor
just kind-slow-janitor
```

`--timeout` 是 load 的整体等待期限，单位秒；每个 GitHub job 的执行期限为 10 分钟。
`--dry-run` 只适用于 load，预览状态不会计为通过。
任何 E2E 被跳过、任务数不符、任务失败或 runner 归属无法确认，入口都会失败。

load 会在 `kuring/ci-infra` 创建唯一临时分支、提交和 PR，在该**临时分支**上仅保留
本轮测试工作流，避免同时触发其他工作流。不会提交或推送本地实现代码，也不会修改 main。
正常结束、失败和中断都会取消未完成运行、关闭 PR 并删除临时分支；清理失败会让命令失败。

### 单独执行 load-test

`osdc/kind/slow-tests/recipes.just` 是独立入口，直接调用 Python 测试脚本，
并通过 mise 加载 `osdc/mise.toml` 中的工具。进入 `osdc/kind/` 后，
可用 `just --justfile slow-tests/recipes.just --list` 查看全部 recipe。
上层 `osdc/kind/justfile` 继续导入同一份 recipe，已有命令也可使用。

以下步骤沿用环境上已经准备好的 kind 集群、ARC 和 GitHub 凭证。
先确认节点状态：

```sh
export KUBECONFIG="$(pwd)/.state/slow-tests/kubeconfig"

# 确认 context 为 kind-osdc-slow-local，4 个节点均为 Ready
kubectl config current-context
kubectl get nodes
```

然后执行 load：

```sh
just --justfile slow-tests/recipes.just kind-slow-load --jobs 12 --timeout 900
```

`--jobs 12` 表示运行 12 个真实 GitHub Actions 任务，最多 3 个并发；
`--timeout 900` 表示整体等待期限为 900 秒（15 分钟）。
测试开始前恢复本地控制器基线，结束或超时后自动清理临时 GitHub 资源。

结果查看 `report.json`：`results.load` 为 `passed` 才表示全部任务成功
并通过本地 runner 归属校验；超时或部分任务未完成会记录为 `failed`。
如果提示上一轮 GitHub 资源尚未清理，先执行 `just kind-slow-cleanup-github`，再重跑。

### 手工执行 compactor

compactor E2E 需要专用 kind 集群已部署控制器（先完成
`kind-slow-render` → `kind-slow-create` → `kind-slow-deploy` →
`kind-slow-install-arc`），且当前没有未清理的 load 测试 GitHub 资源。

```sh
# 1. 确认集群和节点 Ready
export KUBECONFIG="$(pwd)/.state/slow-tests/kubeconfig"
kubectl get nodes

# 2. 运行 compactor E2E（临时修改控制器配置并验证污点/解污点等场景，
#    结束后自动恢复 dry-run=true 基线）
just --justfile slow-tests/recipes.just kind-slow-compactor

# 3. 查看结果：results.compactor 为 passed 表示通过
cat .state/slow-tests/report.json

# 详细 pytest 输出见 .state/slow-tests/compactor-e2e.log
```

E2E 过程会临时把专用 `osdc-kind-slow` 的 min/max runners 设为 0，
原值保存在 `.state/slow-tests/arc-pause-before.json`；若进程被强杀导致
runner 未恢复，重新执行 `just kind-slow-install-arc` 恢复该 profile。

### 手工执行 janitor

janitor E2E 同样依赖已部署的集群和控制器，与 compactor 一样建议在
load 测试之后、串行执行。

```sh
# 1. 确认集群和节点 Ready
export KUBECONFIG="$(pwd)/.state/slow-tests/kubeconfig"
kubectl get nodes

# 2. 运行 janitor E2E（上游五项测试：阈值内不清理、触发清理、
#    优先清理大镜像、运行中镜像保留、指标；结束后自动恢复 9999Gi 高阈值基线）
just --justfile slow-tests/recipes.just kind-slow-janitor

# 3. 查看结果：results.janitor 为 passed 表示通过
cat .state/slow-tests/report.json

# 详细 pytest 输出见 .state/slow-tests/janitor-e2e.log
```

janitor 测试会真实触发镜像 GC；结束后入口自动停止旧 GC Pod 并重新导入
本地 janitor / Alpine 镜像。如果手工干预过缓存，可执行
`just kind-slow-restore --reload-janitor` 恢复。

## 报告和恢复

```zsh
cat .state/slow-tests/report.json

# 恢复本地控制器的安全基线
just kind-slow-restore

# janitor 清理缓存后，停止旧 GC Pod 并重新导入本地镜像再恢复
just kind-slow-restore --reload-janitor

# 网络中断或强杀后，根据持久记录重试 GitHub 清理
just kind-slow-cleanup-github
```

`report.json` 保存每项的 `passed / failed / not_run / preview`，同时列出不覆盖范围。
`compactor-e2e.log`、`janitor-e2e.log` 保存 pytest 输出；
`github-run.json` 保存临时分支、PR、Actions run、任务结果、观测到的 runner 和清理状态。
存在尚未清理的 GitHub 记录时，新的 load 会拒绝启动。

测试保留专用 kind 集群，便于重复执行。退出码 0 只表示上述本地范围通过，
不能替代 AWS staging 的完整 `osdc-pre-merge.yml` 验收。
