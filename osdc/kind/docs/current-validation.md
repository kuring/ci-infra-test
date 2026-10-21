# drain-test ECS 验证（2026-10-20）

入口：`ci-infra-test/osdc/kind/justfile`。
上游：`ci-infra` HEAD `06c039c1de9eb7983f0b5415046a28deb28d612d`。
执行节点：`i-t4ne3fqx0h2huvw840vm`，通过 `workbench connect` 连接。
本轮范围为 drain 单测、独立 Kind 部署、容量 probe 和真实 GitHub E2E。

## 已完成的验证

| 检查 | 结果 |
| --- | --- |
| 上游 drain-test + 本地适配单测 | 127 通过；0 失败、0 跳过 |
| 上游真实进程组回收测试 | 包含在上述完整运行中，不再排除 |
| Ruff E/F/I 与格式 | 通过（采用上游 ruff.toml） |
| 专用 Kind 集群 | `osdc-drain-local`，两个节点 Ready，归属 guard 通过 |
| ARC controller / listener | `0.14.1-jeanschmidt.20`，使用 fork 发布的同版本公共镜像 |
| 容量 probe | 通过；maxRunners 与 advertised capacity 归零，再恢复到 3 |
| 工作流 dry-run | 通过 |
| 初始 GitHub E2E | 失败；inflight 启动等待 600 秒超时，退出 1 |
| 请求头对照后的 GitHub E2E | 进入 drain；inflight 成功，held 在容量 0 下排队；resume 后 180 秒仍未分配，退出 1 |
| #4715 方案 A 绕过后的 E2E | 通过；原 inflight 与三个原 held 均成功，任务 ID 不变；脚本退出 0 |
| restore 与资源清理 | 通过；maxRunners / advertised capacity 为 3，临时 PR/分支清理，owned Actions 均结束 |

单测机器可执行 `ps` 和进程组信号，覆盖了此前 macOS 沙箱中未执行成功的两项测试。
本轮没有修改 `ci-infra` 源码、AWS 集群或已有其他 Kind 集群。

## 本轮修复

仅修改 `ci-infra-test`：

- 增加 `OSDC_KIND_DIRECT_NETWORK=true`，取消 Kind 节点代理并忽略本地 proxy.yaml。
- 复用 `OSDC_KIND_ARC_PUBLIC_IMAGES=true` 开关，为 drain 环境选择 fork 发布的同版本 controller/listener 镜像，以及官方 runner 镜像。
- `kind-drain-unit` 加载上游 mise 工具环境。
- render 将上游 `mise.toml` 写入 consumer 目录，使原始 drain/resume recipe 在 `OSDC_ROOT` 下仍能激活相同工具；信任范围只传给该测试的子进程。
- 增加直连、镜像默认行为、consumer 工具配置及环境隔离回归。

未改变 capacity-aware 配置、上游测试步骤或通过标准。

## 执行方式

ECS 上的两个源码快照位于 `/root/drain-acceptance-20261020/`。

```bash
cd /root/drain-acceptance-20261020/ci-infra-test/osdc/kind
export OSDC_UPSTREAM=/root/drain-acceptance-20261020/ci-infra/osdc
export OSDC_KIND_DIRECT_NETWORK=true
export OSDC_KIND_ARC_PUBLIC_IMAGES=true

just kind-drain-unit
just kind-drain-create
just kind-drain-controller
just kind-drain install
just kind-drain probe
just kind-drain test --start-timeout 600 --finish-timeout 600
```

本轮复用 ECS 上已有 Python 3.12 虚拟环境，设置 `UV_PROJECT_ENVIRONMENT` 和
`UV_NO_SYNC=1`；上游带 PEP 723 元数据的脚本仍需在本项目独立 uv cache 中准备依赖。
不要在这个 cache 尚未准备时启用 `UV_OFFLINE=1`。
PAT 仅存于权限 600 的 `.state/github-token`，不出现在源码、命令参数或报告中。

## GitHub 观测

- 临时 PR：[kuring/ci-infra#50](https://github.com/kuring/ci-infra/pull/50)。
- 原始工作流：[39420882723](https://github.com/kuring/ci-infra/actions/runs/39420882723)。
- 工作流标签为 `osdc-kind-drain`；首个 `drain-inflight` 任务排队，`runner_id=0`。
- listener Ready，`maxRunners=3`，advertised capacity=3，分配统计保持 0。
- 600 秒启动等待超时，测试以 1 退出；尚未进入带任务的 drain 阶段，held 排队和恢复后执行未验证。
- 本轮没有实际 runner Pod；已保存 controller/listener 完整日志及相关 Pod 描述，不存在 runner 执行日志。

与 [ARC #4715](https://github.com/actions/actions-runner-controller/issues/4715) 的表现相似，
但时序不同：该 issue 的关键步骤是在容量 0 时提交任务，再恢复容量；
本轮先通过 probe 确认容量恢复为 3，再提交首个工作流。
当前证据不能确认是同一根因。初次运行未追加唤醒实验、修改 minRunners 或重启 listener。后续独立对照见下文。

详细日志、JUnit XML、Pod JSON/describe 与持续日志流保留在 ECS 的
`/root/drain-acceptance-20261020/`。已核对 PR 关闭、临时分支删除，Actions 状态为 completed/cancelled；
restore 退出 0，maxRunners 和 advertised capacity 均为 3。专用 Kind 集群保留用于后续复现。

脱敏证据下载到本项目忽略目录 `.state/ecs-20261020/evidence.tar.gz`；
解包目录为 `.state/ecs-20261020/evidence/`，其中 `cleanup.json` 保存最终核对结果，
`e2e.log` 保存启动超时，`probe.log` 保存容量切换通过记录。


## 首个任务排队的后续排查

核对了实际注册仓库、标签、runner group、scale set ID 13、仓库 Actions 策略、
controller/listener 运行状态；没有发现这些配置不一致。
随后保持原 scale set、镜像、容量感知和 runner 模板不变，运行独立 smoke 对照：

- 临时 PR：[kuring/ci-infra#51](https://github.com/kuring/ci-infra/pull/51)。
- 工作流：[39423324834](https://github.com/kuring/ci-infra/actions/runs/39423324834)。
- 20:34:32 创建 PR，20:34:36 listener 收到 assigned job=1 并创建 runner。
- Runner 镜像首次下载约 49 秒；工作流容器 python:3.12.9-slim 下载约 6 秒。
- 20:36:42 工作流 success，执行 runner ID 100；脚本退出 0，临时 GitHub 资源清理成功。

该对照证明当前环境可以通过同一 scale set 完成真实任务，排查焦点转为启动时序。

### 初始 E2E 的启动时间线

| 北京时间 | 观测 |
| --- | --- |
| 20:14:53.409 | capacity monitor 记录 reportedCapacity=0 |
| 20:14:53.410 | listener 开始 Getting next message |
| 20:15:03.412 | monitor 记录 reportedCapacity=3 |
| 20:15:09.050 | 容量 probe 的恢复检查通过 |
| 20:15:26 | 首个 inflight 任务创建，随后一直 queued |
| 20:15:43.729 | listener 开始下一次 Getting next message |

fork 的容量感知启动代码将 listener 初始容量设为 0，等监控上报实际资源容量。
scaleset v0.3.0 在每次 GetMessage 调用时读取容量，并将该值写入
X-ScaleSetMaxCapacity 请求头；修改本地容量不会改变已经发出的 HTTP 请求。
因此，指标先变为 3，不足以证明 GitHub 在任务提交前已经收到容量 3 的请求。

目前最强解释是：本地测试的就绪判断早于首次正容量轮询，任务进入仍按容量 0
处理的窗口，之后表现为排队不派发。初次运行的请求头没有直接抓取；后续 A/B 对照已直接记录请求头，支持这一启动时序解释，
见下一节。正式测试代码的启动等待仍未修改。

源码参考：
- [fork listener 初始容量与并发启动](https://github.com/jeanschmidt/actions-runner-controller/blob/master/cmd/ghalistener/main.go)
- [scaleset v0.3.0 容量读取](https://github.com/actions/scaleset/blob/v0.3.0/listener/listener.go)
- [scaleset v0.3.0 请求头设置](https://github.com/actions/scaleset/blob/v0.3.0/session_client.go)

后续 smoke 与注册核对的脱敏证据已下载到 `.state/ecs-20261020/diagnosis/`；
归档为 `.state/ecs-20261020/diagnosis.tar.gz`。


## 实际请求头 A/B 对照及完整阶段验证

本节时间均为北京时间。用户授权继续验证后，在同一个专用 Kind 集群执行两组独立实验。
没有修改 `ci-infra` 或 `ci-infra-test` 的测试步骤、容量计算和断言。
临时替换专用 scale set 的 listener 二进制，只增加 HTTP 请求头、响应状态/耗时、
message ID 和任务统计日志。controller、runner、容量感知配置、注册仓库与 scale set ID 均保持不变。
两组之间仅重建该 listener；运行中的测试没有追加唤醒任务或人工创建 runner。

### 诊断版本来源与验证边界

- 源码固定为 fork commit `d391394fec89aab61aeecc6c445ff744ef25fcc3`。
- 使用原二进制记录的 Go `1.26.1`、scaleset SDK `v0.3.0`、版本和构建参数。
- 未修改源码重建的二进制与当前镜像的 `/ghalistener` 逐字节相同，SHA-256 均为
  `975bcb991a1af8666d02e57c659cbfb0121ddcac5967fab95aff8af3f3d24281`。
- 原镜像 index `1f7fd830...` 包含本节点使用的 AMD64 manifest `50b0dcb2...`，不是镜像版本漂移。
- 诊断镜像基于该不可变 AMD64 manifest，只替换 `/ghalistener`。
- SDK 协议日志测试、SDK listener 测试、ARC listener/config/scaler 测试通过。
  SDK 自带 `TestServerWithSelfSignedCertificates` 的证书在 2026-07-13 过期，未修改 SDK
  的同一测试也失败；保留原始失败日志，其余 SDK 测试通过。没有关闭真实连接的 TLS 校验。
- 两组诊断均通过原入口运行：`just kind-drain test --start-timeout 120 --finish-timeout 180`。
  `observe-seconds` 保持默认 60。这是缩短超时的协议诊断，不能表述为又完成一次 600 秒恢复观察。

### A：指标为 3，但首次容量 0 请求仍在途

PR：[52](https://github.com/kuring/ci-infra/pull/52)。
工作流：[39426237425](https://github.com/kuring/ci-infra/actions/runs/39426237425)。

| 时间 | 实测 |
| --- | --- |
| 20:56:21.843 | 发出 `X-ScaleSetMaxCapacity: 0` 的首次轮询 |
| 20:56:25.843 | 本地 reportedCapacity 变为 3 |
| 20:56:26.832 | 独立驱动确认指标为 3、首次容量 0 请求尚未返回，启动原测试 |
| 20:56:36 | inflight job `118639399686` 创建 |
| 20:57:12.127 | 首次容量 0 请求返回 HTTP 202，耗时约 50 秒 |
| 20:57:12.132 | 下一轮请求携带容量 3 |
| 20:58:02.430 | 容量 3 请求返回 HTTP 202；原任务仍 queued |
| 20:58:45 左右 | 启动等待 120 秒超时；测试退出 1，并取消工作流 |

ARS 在整个 A 组中均为 `minRunners=0,maxRunners=3`。
因此 `maxRunners=3` 与实际在途容量头为 0 可以同时成立；本地指标变化不会修改已发出的请求。
A 组直接确认了原先推断中的协议窗口，但不能补抓第一次运行的请求头，也不能单独证明服务端根因。

### B：容量 3 请求完成后，再运行原测试

PR：[53](https://github.com/kuring/ci-infra/pull/53)。
工作流：[39426812208](https://github.com/kuring/ci-infra/actions/runs/39426812208)。

| 时间 | 实测 |
| --- | --- |
| 21:00:38.531 | 容量 3 轮询返回 HTTP 202 |
| 21:00:39.459 | 独立驱动确认上述响应后启动原测试 |
| 21:00:48 | inflight job `118641328255` 创建 |
| 21:00:48.748 | listener 收到 HTTP 200、`assigned_events=1` |
| 21:01:28.706 | 原测试确认任务已在 runner ID 101 上执行等待步骤，开始 drain |
| 21:01:38.888 | 原测试确认 runner limits 与 advertised capacity 均为 0 |
| 21:01:48 | 三个 held job 创建，均未分配 runner |
| 21:01:51.054 | inflight 任务在 drain 后成功完成 |
| 21:01:59.615 | 确认 held 排队，随后完成原有 60 秒观察窗口 |
| 21:03:00.582 前 | live ARS 已恢复为 `minRunners=0,maxRunners=3` |
| 21:03:05.191 | resume 重建后的 listener 首次轮询携带 0 |
| 21:03:55.492 | 后续轮询开始携带容量 3 |
| 21:04:45.822 | 容量 3 轮询返回 HTTP 202 |
| 21:05:36.148 | 第二次容量 3 轮询返回 HTTP 202，紧接着发出第三次容量 3 请求 |
| 21:06:01 | 最后采样中三个原 held job 仍为 queued，runner_id=0 |
| 21:06:05 前 | `held jobs succeed after undrain` 等待 180 秒超时，测试退出 1 并清理 |

三个 held job ID 为 `118641799569`、`118641799613`、`118641800034`。
resume 后未收到新的 assigned 通知，未创建实际 runner Pod。
HTTP 202 是空消息，不能把 listener 重复使用的旧统计误写成服务端每次重新返回 assigned=0。

本轮已经复现 [ARC #4715](https://github.com/actions/actions-runner-controller/issues/4715)
描述的关键失败序列：任务在容量 0 时排队，容量恢复为 3 后仍不分配。
本轮恢复观察窗口为 180 秒，且未执行额外任务唤醒实验；与 issue 的 600 秒观察及唤醒证据范围不同。
因此可确认恢复失败现象一致，不能据此断言底层根因已被证明相同。
按用户的停止边界，不继续追加唤醒、minRunners 变更或 ARC 修复实验。

### 对正式 ci-infra 测试的影响

启动就绪条件过早与恢复失败是两个独立的验证结果。
本轮外部等待条件让原测试成功进入 drain，但没有解决 resume 后旧任务不被分配的问题。
仅在 `ci-infra-test` 加启动等待，不足以让正式 `ci-infra` 的 drain/resume 验收通过。
截至该协议对照，完整 E2E 仍为失败；当时没有修改正式测试或 ARC 行为。后续经用户选择方案 A，正式测试的修改及复测见下一节。

### 恢复与证据

21:06:20 验证完成：live ARS 的原始 listenerTemplate 已精确恢复，原 fork listener Ready，
指标为 3，`minRunners=0,maxRunners=3`，没有实际 runner Pod；容量预留 placeholder Pod 属于正常配置。
controller UID 未变化。PR 52/53 均关闭、分支删除，Actions 均 completed/cancelled。

证据下载在本项目忽略目录 `.state/ecs-20261020/header-check/`，包括逐时刻 Kubernetes 快照、
完整 listener/实际 runner 日志与描述、controller 日志、GitHub job 采样、构建来源、诊断补丁和清理结果。
归档 `.state/ecs-20261020/header-check-evidence.tar.gz` 的 SHA-256 已核对：
`53a89772613a88ec84320b1504e93aff923a09d54cd4c451d6cc3827efa9dcbc`。


## 方案 A 实现及最终复测（21:36—21:42）

用户选择在正式 `ci-infra` 测试中绕过 #4715：保留原任务，在启动及 resume 后通过新增任务唤醒队列。
正式项目修改范围为 drain controller、对应单测、工作流模板和 `ci-pre-merge.md`。
本项目适配层只透传 API timeout、返回 probe scale set 名称，并保留上游的事件分流条件。

实现行为：

- 临时 PR 的 `opened` 事件运行原 inflight/held；`synchronize` 只运行 `drain-wakeup`。
- 更新同一临时分支的 `.osdc-drain-wakeup` 序号文件产生新任务；runner group 和 label 与原任务一致。
- 发出唤醒前核对完整配置 limit map，且目标 scale set 的 advertised capacity 必须大于 0；区域排除项仍可为 0。
- 每阶段最多 8 次尝试、间隔至少 15 秒，沿用原 start/finish deadline；API 调用也受剩余 deadline 约束。
- 原任务出现 runner 分配后停止补发，继续等待原任务完成。固定初始 run ID 和各阶段原 job ID，禁止用新任务替代。
- drain/观察窗口、异常清理和 `--restore-only` 不发送唤醒；清理覆盖该临时分支的原始与唤醒 runs。

首次试跑 PR 54 中原任务已全部通过，但新增唤醒命令里的 `#4715` 被 YAML plain scalar
当作注释截断，唤醒 job 自身失败。已改用 block scalar，并增加“解析 YAML 后真实执行 shell”回归。
下面以修正后 PR 55 的复测作为最终结果，不把 PR 54 当成最终全链路通过记录。

### 最终结果

- PR：[55](https://github.com/kuring/ci-infra/pull/55)。
- 原始 workflow：[39431940363](https://github.com/kuring/ci-infra/actions/runs/39431940363)，最终 success。
- 使用原始 `0.14.1-jeanschmidt.20` 发布镜像，无诊断二进制。
- 重建专用 listener 后直接启动测试，未增加外部启动等待。
- 执行原入口：`just kind-drain test --start-timeout 600 --finish-timeout 600`，观察窗口仍为 60 秒。

| 北京时间 | 实测 |
| --- | --- |
| 21:36:50.049 | 原测试确认 listener metrics available |
| 21:37:09.605 / 21:37:29.869 | 启动阶段发送 2 次唤醒；这两个 wakeup job 均 success |
| 21:38:28.862 | 原 inflight 在 runner ID 108 上执行等待步骤，开始 drain |
| 21:38:39.043 | runner limits 与 advertised capacity 均为 0 |
| 21:38:51.299 | 原 inflight 正常完成 |
| 21:38:59.794 | 三个原 held 任务保持 queued，随后完成 60 秒观察窗口 |
| 21:40:13.515 / 21:40:33.406 / 21:40:53.366 | resume 后发送 3 次唤醒 |
| 21:41:58.121 | 三个原 held 全部成功，未改变 job ID；脚本退出 0 |
| 21:42:17.206 | 最终资源与状态核对通过 |

原任务映射：

| Job | Job ID | Runner ID | 结论 |
| --- | --- | --- | --- |
| inflight | 118658922008 | 108 | success |
| held (3) | 118659806862 | 111 | success |
| held (1) | 118659806875 | 112 | success |
| held (2) | 118659807046 | 113 | success |

resume 阶段的 3 个唤醒任务在原 held 成功后由清理流程取消，均未分配 runner；
启动阶段的两个唤醒任务已成功执行。新增流量足以产生原队列的分配通知，唤醒任务本身不必先执行。
本次通过结论明确包含 workaround，不代表“没有新增流量时仅恢复容量”已经修复。

### 单测、静态检查与清理

- ECS drain controller + adapter：152 项单测全部通过，包含真实进程组回收与 YAML/shell 回归。
- 本地全量 `just test`：通过（43 秒），每文件 95% coverage gate 通过；drain 两模块合计覆盖率 99.75%。
- Ruff 与生成工作流 actionlint：通过；actionlint 明确声明已有自定义 runner label。
- 全量 `just lint` 已执行，但未全过：OpenTofu provider registry 返回 403；已有 runner-base Dockerfile
  的 `DL3066` 提示在未修改的 HEAD 文件上也能复现。本轮未修改无关 Dockerfile、工具版本或 lint 规则。
- live listenerTemplate 与 controller UID 未改变；原发布镜像保持不变，min=0/max=3、advertised capacity=3，无实际 runner Pod。
- PR 54/55 均关闭、临时分支均删除；最终 6 个 owned runs 均 completed（原始 run 与两个启动 wakeup success，三个恢复 wakeup cancelled）。
- 最终测试的 ECS 源码 SHA-256 与本地源码一致，逐次 GitHub 采样确认四个原任务 ID 没有变化。

证据归档：`.state/ecs-20261020/workaround-A-evidence.tar.gz`；SHA-256：
`63be431eee797112a23685595b0b746cbf7c7efbe81c222c977ea738e85fe319`。
本地完整测试/lint 日志、源文件 hash 和正式代码 diff 另存于
`.state/ecs-20261020/workaround-A-local-quality/`。
本轮为 ECS 上的专用 Kind 验证，尚未运行 AWS staging 集群验收。


## 远程全链路复验（23:48 完成）

用户要求跳过本机 lint，直接在 ECS 进行全链路验证。本次完整检查均在
`i-t4ne3fqx0h2huvw840vm` 执行，使用独立 `ci-infra/osdc/.venv` 和原有专用 Kind 集群。
同步了当前工作区的 OSDC 与 GitHub workflow 源码（包含最新的 #4715 链接注释），
连同两个本地适配文件共核对 621 个文件的 SHA-256；最终核对时源码仍与本地一致。

| 远程检查 | 结果 |
| --- | --- |
| OpenTofu registry 服务发现 | HTTP 200，本机的 403 未复现 |
| `uv sync --frozen` | 通过，独立 Python 3.12.15 环境，未修改依赖锁定 |
| 上游全量 `just test` | 通过，1 分 46 秒，覆盖率门槛通过 |
| `just kind-drain-unit` | 152 项通过 |
| 全量 `just lint` | 13/14 通过；provider 初始化与 tofu validate 通过；仅已有 Dockerfile DL3066 失败 |
| 修改文件 Ruff / 格式 | 通过 |
| 生成工作流 actionlint | 通过，声明已有自定义 runner label |
| `just kind-drain-controller` | 部署成功，同版本发布镜像 |
| `just kind-drain install` | 安装成功 |
| `just kind-drain probe` | 通过，容量 3 → 0 → 3 |
| `just kind-drain test --start-timeout 600 --finish-timeout 600` | 通过，原任务 ID 不变，脚本退出 0 |

远程全量单测首次运行的 3 项失败来自验证环境：快照缺少 Git index，且启动命令继承了
`OSDC_ROOT`，影响路径发现测试。补齐验证快照的索引、移除该环境覆盖后，全量测试通过；
没有修改源码或断言来绕过失败。首轮失败与最终通过日志均保留。

全量 lint 唯一剩余失败为未修改的 `base/docker/runner-base/Dockerfile:36`：
`DL3066: Non-numeric user-id may not be resolvable by host system`。
本轮未修改无关 Dockerfile、工具版本或 lint 规则。

### 真实 GitHub 结果

PR：[56](https://github.com/kuring/ci-infra/pull/56)。
原始 workflow：[39450923160](https://github.com/kuring/ci-infra/actions/runs/39450923160)，最终 success。

| 北京时间 | 观测 |
| --- | --- |
| 23:42:46.288 / 23:43:05.855 | 启动阶段 2 次唤醒，两个 wakeup job 最终均 success |
| 23:44:04.806 | 原 inflight 进入等待步骤，启动 drain |
| 23:44:15.002 | limits 与 advertised capacity 均为 0 |
| 23:44:37.580 | 原 inflight 正常完成 |
| 23:44:38.102 | 三个原 held 已排队，随后保持完整 60 秒观察窗口 |
| 23:45:51.273 / 23:46:11.186 / 23:46:31.162 | resume 后 3 次唤醒 |
| 23:47:36.753 | 三个原 held 均成功 |
| 23:47:52.886 | E2E collector 返回 0，所有部署/probe/E2E 阶段均退出 0 |
| 23:48:00.479 | 最终核对通过 |

| 原任务 | Job ID | Runner ID | 结果 |
| --- | --- | --- | --- |
| inflight | 118725178412 | 114 | success |
| held (1) | 118726156137 | 117 | success |
| held (3) | 118726156156 | 118 | success |
| held (2) | 118726156212 | 119 | success |

最终核对：PR 56 关闭、分支删除，6 个 owned runs 均 completed；三个恢复 wakeup 在原任务
成功后取消，均未分配 runner。listenerTemplate 与 controller UID 未改变，发布镜像保持
`0.14.1-jeanschmidt.20`，min=0/max=3、advertised capacity=3，没有实际 runner Pod。

完整远程证据：`.state/ecs-20261020/remote-chain-evidence.tar.gz`，SHA-256：
`e44da8244c47dd3a020ae4bf534be59af76725cd4208ad0bb2ccb2e718560d69`。
包含部署、probe、全量单测、lint、静态检查、GitHub 采样、完整 Pod 日志、源码 hash 与最终核对。
本轮是 ECS Kind 全链路验证，仍不等同于 AWS staging 集群验收。


## 2026-10-21 远程全链路复验（18:30 完成）

本轮重新同步当前工作区的 621 个源码文件，逐文件核对 SHA-256，并重新执行远程全链路。
未修改正式 ci-infra 代码；ci-infra-test 的 Kind 入口补齐正式控制器新增的 run/cleanup/restore
超时参数，默认值直接引用正式代码。适配期间一次漏导入导致的单测失败已修正，失败日志保留。

| 检查 | 本轮结果 |
| --- | --- |
| 上游全量 `just test` | 通过，日志 `OK (1m10s)` |
| `just kind-drain-unit` | 213 项通过 |
| 全量 `just lint` | 13/14 通过；唯一失败仍为未修改 Dockerfile 第 36 行 DL3066 |
| 修改 Python 的 Ruff / 格式 | 通过，包含 Kind 适配层 |
| 生成工作流及两个修改的 GitHub workflow actionlint | 通过 |
| ARC controller 部署、runner 安装 | 均退出 0，同版本发布镜像 |
| 容量 probe | 通过，3 → 0 → 3 |
| 真实 drain/resume E2E | 退出 0，四个原始任务全部 success，job ID 不变 |

临时 PR [57](https://github.com/kuring/ci-infra/pull/57)，原始
[workflow 39584402848](https://github.com/kuring/ci-infra/actions/runs/39584402848) 最终 success。

| 北京时间 | 观测 |
| --- | --- |
| 18:25:01 / 18:25:16 / 18:25:35 | 启动阶段 3 次唤醒 |
| 18:26:34 | 原 inflight 进入等待步骤，开始 drain |
| 18:26:44 | limits 与 advertised capacity 均为 0 |
| 18:26:56 | 原 inflight 正常完成 |
| 18:27:05—18:28:05 | 三个原 held 连续保持排队，完整 60 秒观察通过 |
| 18:28:18 / 18:28:38 | resume 阶段 2 次唤醒 |
| 18:29:48 | 三个原 held 均成功 |
| 18:30:08 | 最终独立核对通过 |

| 原任务 | Job ID | Runner ID | 结果 |
| --- | --- | --- | --- |
| drain-inflight | 119169605166 | 121 | success |
| drain-held (3) | 119170450016 | 126 | success |
| drain-held (2) | 119170450119 | 125 | success |
| drain-held (1) | 119170450194 | 127 | success |

最终状态：PR 57 关闭、临时分支删除，6 个本轮 workflow 全部 completed；三个启动 wakeup
成功，两个 resume wakeup 在原任务成功后被取消（其中一个已获得 runner）。min=0/max=3、
advertised capacity=3，无实际 runner Pod；listenerTemplate 和部署后 controller UID 未改变。

启动时共享 stale-PR 清理助手尝试查询默认 canary 仓库，但当前测试 PAT 无该仓库权限，记录
warning 后继续。因此本轮没有验证默认仓库的历史 PR 清理；本轮临时 PR、分支和所有运行的
清理均通过独立 GitHub API 核对。ECS Kind 验证不等同于 AWS staging 验收，也不证明
#4715 的容量单独恢复路径已修复；本轮验证的是保留原任务的有界新流量唤醒方案。

证据：`.state/ecs-20261021-retest/evidence.tar.gz`，SHA-256：
`e5748be28f78d817b425b52302174efbf6a5d2e98801bdb0dee64c7dca7e258c`。
本地已核对下载文件 hash、全部远程阶段退出码、四个原始 job ID、源码快照一致性，并扫描令牌模式。
