# 本地 ARC drain/undrain 调度排查记录

日期：2026-10-13。所有时间为北京时间。
目标仓库：kuring/ci-infra。未操作 AWS 或 staging；本地实现修改未提交。

## 已确认的事实

- kind 两个节点为 Ready，runner 2.338.0，ARC fork 0.14.1-jeanschmidt.20。
- 同样的 in-flight → 三个矩阵任务，在不执行 drain 时可以正常运行。
- drain 期间，已运行任务可以成功结束，三个后续任务均未被分配。
- undrain 恢复 maxRunners=3、对外容量指标=3 后，旧任务仍不分配。
- 重新创建 listener、重新注册 scale set，没有恢复旧任务；恢复后创建的新任务可以执行。
- PR18：同标签普通 runner 上线后，旧任务全部由 ARC runner 执行，普通 runner 未执行任务。
- PR19：普通 runner 仅注册、保持 offline，唯一标签不匹配，观察 60 秒没有恢复。
- PR19：启动该 runner 后，保持不匹配标签，旧任务全部由 ARC runner 执行成功。

## PR19 的关键时间线

| 时间 | 事件 |
| --- | --- |
| 11:49:24 | undrain 恢复 maxRunners=3 |
| 11:50:33 | 三个原任务仍 queued，runner_id=0 |
| 11:50:51 | 普通 runner 注册为 offline |
| 11:51:57 | offline 阶段观察 60 秒，无任务分配 |
| 11:52:09 | 普通 runner 开始 Listening for Jobs |
| 11:52:09.955 | ARC listener 得到 assigned job=3，并创建三个 ARC runner |
| 11:52:21–22 | 三个原任务开始执行 |
| 11:52:39–42 | 三个任务全部 success |

普通 runner ID=43，唯一标签 `osdc-unmatched-46189f8245784fbc8c3e8a5f610cad21`。
实际执行任务的 ARC runner ID 为 44、45、46，普通 runner 全程 busy=false。
原任务 ID：115119233706、115119233744、115119233806；没有重跑或替换任务。

运行：https://github.com/kuring/ci-infra/actions/runs/38343322991
PR：https://github.com/kuring/ci-infra/pull/19

## 结论的边界

已经识别到具体触发阶段：不匹配标签的普通 runner 上线，与 ARC 旧任务恢复分配同秒发生。
仅注册且 offline 没有恢复。这否定了“必须让普通 runner 接手旧任务”和“必须新增匹配标签”的解释。

这支持 GitHub 队列重新评估/唤醒路径存在问题的判断，但尚不能断言服务端内部是哪一段代码有缺陷。
容量指标为 3，不等于已经抓取并验证每个 HTTPS 请求中的 X-ScaleSetMaxCapacity 头部。
需要同版本关闭 capacity-aware 的对照，进一步区分动态容量路径与基础 scale-set 路径。
关闭容量感知后无法使用动态容量指标作验收证据；该实验有独立脚本，不改变正式测试断言。

## 部署镜像的可验证信息

镜像：ghcr.io/jeanschmidt/gha-runner-scale-set-controller:0.14.1-jeanschmidt.20
Digest：sha256:1f7fd830a98836ce793091499c80a1ea10187761a954e6a37e09dd8ff55c006e
Source label：https://github.com/jeanschmidt/actions-runner-controller
没有 revision label；未发现 vcs.revision 构建信息。
从正在运行的 listener 二进制构建信息确认依赖 actions/scaleset v0.3.0，
校验和 h1:y5/ClYLJXFuGCikzILOOPhaCShAcL6K0mnUtjDKFxVw=。

## 命令复现

先保持 SOCKS5 13659 和 HTTP 转接 13660 运行。在 osdc/ 中执行：

```zsh
HTTPS_PROXY=http://127.0.0.1:13660 NO_PROXY=localhost,127.0.0.1 \
  mise exec -- just --justfile kind/diagnosis/recipes/classic-operations.just probe-routing
```

该命令操作真实 GitHub 队列，自动创建并清理临时 PR、分支、runner、Pod 和 Secret。
同一 kind 集群一次只运行一项测试。

常规验收使用 `just --justfile kind/justfile kind-drain test`；仅容量验证使用 `just --justfile kind/justfile kind-drain probe`。
`act` 在本地 Docker 中执行工作流步骤，不会经过真实 GitHub runner 匹配和 broker；
无法替代这里的队列分配验证。当前临时工作流使用 pull_request，没有 workflow_dispatch，
不能直接使用 gh workflow run 启动。

参考：https://nektosact.com/introduction.html
参考：https://cli.github.com/manual/gh_workflow_run

## 清理

PR18、PR19 已关闭，临时分支删除，普通 runner 注销，Pod/注册 Secret 删除。
诊断脚本和脱敏快照仍保存在本目录（Git 忽略，权限仅当前用户）。
没有把普通 runner 作为正式恢复机制，也没有把干预后的成功算作正常 undrain 验收通过。

## 同版本关闭容量感知对照（PR20）

本轮仅临时设置 CAPACITY_AWARE_ENABLED=false，镜像、工作流和上限等参数保持原值。
没有普通 runner。静态实验独立验证 ARS 与 listener maxRunners，不伪造容量指标，
也没有修改正式测试中的容量断言。

- drain：上限归零，已运行任务成功，三个后续任务保持排队并观察 60 秒。
- resume：配置上限恢复为 3；三个旧任务自行分配给 ARC runner，全部成功。
- 12:04:31.772：静态 listener 开始运行，配置 maxRunners=3。
- 12:04:34.688：收到 assigned job=3，距离启动约 3 秒。
- 12:04:44–46：三个任务开始执行；12:05:05 前全部完成。
- 12:05:47：容量感知原配置已恢复，指标重新为 3。

运行：https://github.com/kuring/ci-infra/actions/runs/38344507395
PR：https://github.com/kuring/ci-infra/pull/20
原任务 ID：115122869576、115122869599、115122869639。
实际 ARC runner ID：49、50、48。

这把故障进一步缩小到 fork 的容量感知路径及它和 SDK/broker 轮询的交互。
当前检索的 fork 源码，开启容量感知时将 listener 初始上限设为 0，之后异步更新；
关闭时直接使用配置上限。SDK 的 SetMaxRunners 只存储一个原子值，GetMessage 下一次轮询
才会将新值设置到容量请求头。第一轮请求为 0、后续恢复正容量这一差异是候选机制，
尚未观察实际请求头，不能当作已确认的服务端/客户端底层缺陷。

此对照通过不等于原容量感知模式已经修复，也没有永久关闭功能。
静态对照命令（已获授权运行一次并恢复）：

```zsh
HTTPS_PROXY=http://127.0.0.1:13660 NO_PROXY=localhost,127.0.0.1 \
  mise exec -- just --justfile kind/diagnosis/recipes/capacity-control.just run-control
```


## 独立 broker 协议实验（PR22、PR23、PR24）

按照部署中 SDK v0.3.0 的协议，使用 Python 直接创建临时 scale set、session，
轮询 messageQueueUrl，并确认消息。发送前检查 PreparedRequest 中的
`X-ScaleSetMaxCapacity`，响应后再次检查 response.request，日志不记录授权头。
不创建 runner，不经由 ARC/Kubernetes，验证对象仅为任务向 broker 的投递。

| 实验 | 结果 |
| --- | --- |
| 首轮上报 3，然后创建三个任务（PR22） | 约 0.3 秒收到三个不同 jobId 的 JobAssigned；totalAssignedJobs=3 |
| 首轮上报 0，任务入队后切换为 3（PR23） | 已记录两个完整、约 50 秒的请求，均为 202；随后目录迁移中断记录 |
| 重做 0→3（PR24） | 两个正容量请求均为 202；三个原任务仍 queued，broker assigned=0 |
| PR24 删除旧 session，建立新 session，首轮直接上报 3 | 两个请求仍为 202；任务 ID 未改变，broker assigned=0 |

PR22 运行：https://github.com/kuring/ci-infra/actions/runs/38353100158
PR23 运行：https://github.com/kuring/ci-infra/actions/runs/38353263279
PR24 运行：https://github.com/kuring/ci-infra/actions/runs/38354470160

PR24 的正容量轮询于 14:02:48 开始，14:03:38 与 14:04:28 返回 202。
14:04:33 创建新 session，14:05:23 与 14:06:13 仍返回 202。
14:06:15 验证原任务 ID 115154018147、115154018240、115154018298 保持不变，
broker 的 available/acquired/assigned/running 统计均为 0。

这排除了仅由 ARC 的内存容量未更新或 Kubernetes 资源不足造成该现象的解释：
独立客户端确实构造并发送了容量 3 的请求，仍没有收到旧任务。
可观察的触发条件是任务在容量 0 时入队后，后续容量变更和 session 重建没有重新投递它们。
GitHub 内部具体的缓存/队列实现不可见，目前不声称已确认某段服务端代码的缺陷。

PR21 是早期协议计数辅助脚本问题：消息中的 runnerRequestId 都为 0，导致误去重。
已改为按 jobId 计数并以 PR22 重跑，不能把 PR21 的脚本退出状态当作协议行为失败。

PR23 在另一会话迁移目录时停止，已检查清理：PR21–PR24、临时分支与 scale set 7–10
均已关闭/删除。按 ID 查询删除记录可能返回 HTTP 200 + null，而非 404；
验证还须检查返回对象和按名称查询结果，不能只看状态码。

迁移后的命令入口：

```zsh
HTTPS_PROXY=http://127.0.0.1:13660 NO_PROXY=localhost,127.0.0.1 \
  mise exec -- just --justfile kind/diagnosis/recipes/broker-protocol.just probe 3

HTTPS_PROXY=http://127.0.0.1:13660 NO_PROXY=localhost,127.0.0.1 \
  mise exec -- just --justfile kind/diagnosis/recipes/broker-protocol.just probe 0

HTTPS_PROXY=http://127.0.0.1:13660 NO_PROXY=localhost,127.0.0.1 \
  mise exec -- just --justfile kind/diagnosis/recipes/broker-protocol.just recover-session
```

这些是消息投递诊断，不是任务成功执行验收；取消工作流是预期清理步骤。
参考 SDK 协议：https://github.com/actions/scaleset/tree/v0.3.0


### 元数据刷新对照（PR25）

运行：https://github.com/kuring/ci-infra/actions/runs/38355224043
旧任务 ID：115156423504、115156423830、115156424045。

在同一临时 scale set 上，先重现容量 0→3、broker 始终 assigned=0：

1. 保持 ID、名称、runner group、标签与 runnerSetting 不变，执行 UpdateRunnerScaleSet。
   请求成功返回，随后容量 3 的长轮询仍为 202。
2. 保留原匹配标签，提交添加一个未被任务使用的别名标签的更新请求。
   请求成功返回，随后仍为 202，原任务 ID 保持不变。

14:14:41 查询统计仍全部为 0。该实验不支持用一次元数据刷新作为恢复方案。
临时 scale set 11 已删除，PR25/分支已清理；没有修改现有 ARC 的 scale set 6。

### 新任务与旧任务并存：找到唤醒触发（PR26、PR27）

同一个临时 scale set 12、同一个 session、同一个匹配标签、同一个容量 3：

- PR26 的三个任务在容量 0 时入队。切换为 3 后，两个完整长轮询均为 202，统计 assigned=0。
- 保持原任务不变，创建 PR27 的一个新任务，匹配标签仍为同一个 scale set。
- 14:24:38 的轮询约 0.313 秒后收到三个 JobAssigned，**全部属于原来的 PR26**。
- 原任务 ID 保持 115159684817、115159684936、115159684994；统计 assigned 从 0 变为 3。
- PR27 的新任务没有先获分配，三个容量被旧任务占用。新任务的入队事件唤醒了旧队列。

旧任务运行：https://github.com/kuring/ci-infra/actions/runs/38356239517
新任务运行：https://github.com/kuring/ci-infra/actions/runs/38356513110

因此，正容量有效，旧任务也仍然可匹配；缺口在于容量变更没有单独触发旧队列重新匹配，
而新的任务入队或此前观察到的普通 runner 上线可以触发。这个行为已脱离 ARC/Kubernetes
复现，不能只用“fork 没发出新容量”解释。GitHub 内部具体实现仍不可见。

上述统计是 broker 消息分配，不是任务执行成功：本实验不创建实际 runner，GitHub jobs
仍会 queued 等待 runner；最后取消两轮工作流、关闭 PR、删除临时分支和 scale set。
原容量感知模式的正式 E2E 仍未通过，未把新任务唤醒加入正式断言。

迁移后复现命令：

```zsh
HTTPS_PROXY=http://127.0.0.1:13660 NO_PROXY=localhost,127.0.0.1 \
  mise exec -- just --justfile kind/diagnosis/recipes/broker-protocol.just compare-fresh-job
```
