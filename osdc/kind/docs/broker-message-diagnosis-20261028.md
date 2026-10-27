# GitHub broker / ARC listener 消息链路诊断（2026-10-28）

**可观测阻塞点已定位到 GitHub 侧旧任务重新匹配、向 broker 消息队列投递这一段。**
独立客户端确实发送容量 3，broker 连续返回 HTTP 202 空响应，assigned=0；
新增同标签任务后，原来的三个旧任务在约 0.318 秒内收到 JobAssigned。
该复现不经过 ARC 或 Kubernetes，因此不能归因于 listener 收到消息后没有创建 runner。
尚不能识别 GitHub 内部具体的调度器、缓存或 broker 代码缺陷。

## 执行范围与证据来源

主机为 `i-t4ne3fqx0h2huvw840vm`，保留专用 Kind / GitHub scale set ID `26` 的配置和会话。
复用 `/root/drain-current-20261027/` 的固定源码；989 个文件前后哈希一致。
本轮没有改动上游、适配器源码、镜像、版本或正式 drain-test 断言，没有执行 drain/resume。

依据部署中二进制核对协议版本：

- listener 镜像 digest：`sha256:1f7fd830a98836ce793091499c80a1ea10187761a954e6a37e09dd8ff55c006e`。
- 从运行容器只读复制 `/ghalistener`，保存文件哈希与构建信息；依赖为
  `github.com/actions/scaleset v0.3.0`，模块校验和
  `h1:y5/ClYLJXFuGCikzILOOPhaCShAcL6K0mnUtjDKFxVw=`。
- SDK 源码固定为 tag v0.3.0 / commit `22bae1217e2048c4187352e0be5481edd758f863`。
- 二进制没有 VCS revision；fork 源码 commit `d391394fec89aab61aeecc6c445ff744ef25fcc3`
  仅作初始化机制参考，不能称为已匹配部署二进制的精确提交。

主要协议定义见
[SDK listener](https://github.com/actions/scaleset/blob/v0.3.0/listener/listener.go)、
[SDK session client](https://github.com/actions/scaleset/blob/v0.3.0/session_client.go)。
GetMessage 通过 `X-ScaleSetMaxCapacity` 传递容量，HTTP 202 返回 nil message；
listener 在 nil message 时继续使用上次的 assigned 统计计算目标 runner 数。
只有收到消息后才推进 message ID、处理分配并更新 scaler。

## 第一步：复核实际 listener 的停滞位置

上一轮 ID `26` 的日志显示：

- broker GET 每约 50 秒完成一次，随后继续下一次 GET，未出现认证或解析错误退出。
- `lastMessageID` 在整个启动等待中保持 0；没有消息确认、JobAvailable acquisition 或 JIT 创建链路。
- scaler 持续看到 assigned job=0，decision=0，ERS replicas=0。
- 同期本地容量指标和 placeholder 调度容量为 3，GitHub job 仍 queued、runner_id=0。

结合已匹配版本的 SDK，这与 HTTP 202 → nil message → assigned=0 的路径吻合。
这是日志与源码推断；没有解密抓取部署 listener 的逐请求 HTTP 头和响应。
后面的隔离实验直接记录实际请求头与 HTTP 响应，补齐这个证据缺口。

## 第二步：隔离协议对照，排除 ARC / Kubernetes

使用独立 Python HTTP 客户端，遵循 SDK v0.3.0 的注册、会话、轮询与消息确认协议。
每个实验使用唯一名称、标签和临时 scale set，runner group 均为 1。
不创建 runner、不执行 AcquireJobs 或 JIT；仅验证 broker 对任务的分配消息。
请求发送前核对 PreparedRequest，响应后再次核对 response.request 中的容量头。
仅保存容量、状态码、响应追踪 ID、消息/任务 ID、统计、耗时及消息体哈希，不保存授权头或令牌。

| 实验 | 直接观测 |
| --- | --- |
| 首轮即报 3，之后创建三个任务 | 下一次 GET 为 HTTP 200，耗时 0.297 秒，收到三个 JobAssigned，assigned=3 |
| 首轮报 0，任务在 0 时入队，再改报 3 | 两个正容量 GET 分别耗时 50.296 / 50.298 秒，均为 HTTP 202、空消息体；assigned=0 |
| 保持旧任务、会话、scale set、容量 3 不变，新增一个同标签任务 | 下一次 GET 为 HTTP 200，耗时 0.318 秒；三个 JobAssigned 全部属于原来的旧运行，assigned=3 |

正容量基线：PR [95](https://github.com/kuring/ci-infra/pull/95)，
[run 40538148179](https://github.com/kuring/ci-infra/actions/runs/40538148179)，临时 scale set `27`。
任务 REST ID 为 `122308912101`、`122308912244`、`122308912466`。

零转正旧任务：PR [96](https://github.com/kuring/ci-infra/pull/96)，
[run 40538385941](https://github.com/kuring/ci-infra/actions/runs/40538385941)，临时 scale set `28`。
原任务 REST ID 保持 `122309752691`、`122309752968`、`122309752969`。
新任务：PR [97](https://github.com/kuring/ci-infra/pull/97)，
[run 40538840728](https://github.com/kuring/ci-infra/actions/runs/40538840728)。
新任务没有先获得三个容量，收到的三个消息 workflowRunId 均为旧运行 `40538385941`。

消息中的 `jobId` 是 UUID，与 REST job 数字 ID 属于不同标识域；归档同时保存运行 ID、
jobDisplayName、消息 UUID 和 REST ID，未混用。runnerRequestId 均为 0，未拿它作去重键。
消息确认返回 HTTP 204。HTTP 202 的 request ID 和空响应哈希也保存在事件文件中。

以上证明：正容量请求有效、旧任务仍能匹配该 scale set；问题出现在容量变化没有单独带来旧任务投递，
新入队事件却能触发投递。它不证明 GitHub 内部哪一段实现出错，也不构成真实 runner E2E success。

## 第三步：验证实际 ARC 的消息接收及 runner 创建

在原 ID `26` 的稳定正容量会话下创建单任务探针，不重启 listener、不改变容量、不执行 drain。
有效探针为 PR [99](https://github.com/kuring/ci-infra/pull/99)，
[run 40539235318](https://github.com/kuring/ci-infra/actions/runs/40539235318)。

- 00:16:11，listener 的 scaler 看到 assigned job=1、decision=1，ERS replicas 更新为 1，
  lastMessageID 前进到 `100000003`。
- 原始 job `122312764768` 分配给 runner `180`，runner group 为 Default。
- 00:16:31，Initialize containers 成功，Wait for drain 步骤进入 in_progress。
- 保存快照时，runner Pod 与 workflow Pod 均 Running，位于 `osdc-drain-local-worker`。
- 随后主动取消并清理，未把这个探针记作普通 drain E2E success。

首个探针 PR [98](https://github.com/kuring/ci-infra/pull/98) 已经获得 runner `179`，
但临时探针漏写 job container，setup 报错。错误日志明确要求增加 `container:`。
保留该失败后，补齐正式测试同款 `python:3.12.9-slim` 容器重跑为 PR99。
配置错误与有效重跑分别保存，没有删除失败证据或修改正式测试。

## 第四步：初始化时序候选，解释为何一次 wakeup 也可能过早

上一轮实际日志与 GitHub created_at 对齐如下，均为 2026-10-27 北京时间：

| 阶段 | 首个 GET 开始 | 本地容量变为 3 | canary / wakeup 运行创建 | 下一轮 GET 开始 |
| --- | --- | --- | --- | --- |
| listener 重建后启动 | 23:14:16 | 23:14:22 | canary 23:14:34 | 短启动日志未覆盖下一轮 |
| resume 后新 listener | 23:25:48 | 23:25:54 | wakeup 23:26:03 | 23:26:39 |
| 新注册 ID 26 启动 | 23:37:18 | 23:37:20 | canary 23:37:35 | 23:38:08 |

SDK 的 SetMaxRunners 仅更新原子值；正在进行的 GetMessage 已捕获旧容量，
不会因本地指标变成 3 而重发，下一轮才读取新值。
所核对的 fork 参考源码在 capacity-aware 模式下初始容量为 0，再由异步容量监控更新。
实际日志也显示首次 GET 与本地 reportedCapacity=0 几乎同时发生，稍后才报告 3。

因此存在一个具体候选窗口：本地容量指标已经为 3，但首轮零容量长轮询尚未结束，
任务或唯一一次 wakeup 已经入队；之后正容量轮询没有触发旧任务重新匹配。
这能解释“本地容量为 3，却连一次 wakeup 也无效”的现象。
**这是时序与源码支持的候选触发机制，尚未直接捕获部署 listener 首轮请求头，不能当作全部因果链已证实。**
候选修复需要同时考虑容量承诺准确性和投递唤醒，不能简单在 placeholder 未就绪时一律先报 3。
本轮没有实施修复、延长正式测试超时或增加正式测试唤醒次数。

## 清理与归档

00:19:12 最终审计通过：临时 scale set `27`、`28` 和各自 session 已删除，按 ID 与名称双重核验；
PR95—99 全部关闭，精确分支删除，相关运行全部终止。
原 scale set ID `26` 和 listener UID 保持不变，limits=3、advertised capacity=3，
broker assigned/running/registered 统计归零，无实际 runner/workflow Pod，无后台诊断进程。
989 个源码文件无漂移；没有操作 AWS staging。

远端：`/root/drain-broker-diagnosis-20261028/`。
本地：`.state/broker-diagnosis-20261028/evidence/`。
归档保留诊断脚本、每次协议事件、HTTP 追踪 ID、会话/资源清理回执、实际 ARC 日志、
Pod 与 job 快照、二进制哈希与构建信息、固定版本参考源码、前轮运行时间、失败探针和最终审计。
二进制原文件仅在远端留存；导出归档不含 PAT、会话 token、环境凭证或二进制文件。

```text
evidence.tar.gz
SHA-256: 54d2f6bb3926e306b8c1a6aaf46ea79c9f388ed8c3131dbcb7ce70fa14c19f05
```
