# Listener 会话与 scale set 注册对照（2026-10-27）

**两轮均未通过完整 E2E：重建 listener 后，held 恢复超时；重新注册 scale set 后，inflight 启动超时。**
单纯更换 listener 会话或注册身份，没有消除本轮任务分配异常。
这些结果不能确认是 GitHub bug，也不能证明上一轮测试污染了某个特定对象。

## 固定条件和操作顺序

执行主机为 ECS `i-t4ne3fqx0h2huvw840vm`，仅使用专用 `kind-osdc-drain-local`。
复用上一轮 `/root/drain-current-20261027/` 的固定快照：上游 `a09b8a5`，
适配器包含 `f47c140` 的取消信号透传修复。前后核对 989 个文件，未发现源码漂移。
本轮没有更改测试代码、镜像、工具版本、runner label 或容量配置。

两轮均使用：

```bash
just kind-drain test --start-timeout 600 --finish-timeout 600 --drain-timeout 300
```

没有开启 `--startup-wakeup`，没有额外手工发送启动或 resume 流量。
第一轮仅使用上游内置的一次 held wakeup；第二轮没有进入 resume。
两轮测试准备后的 worker 标签、taint 和节点 UID 一致。

按用户授权顺序执行：

1. 保存基线、listener 日志和注册身份，执行 `just kind-drain-restart-listener`。
2. 新 listener Ready 且 advertised capacity 为正后，执行第一轮普通 E2E。
3. 第一轮失败后核对运行、PR、精确分支、容量及实际 Pod 清理，再执行 `just kind-drain-reset`。
4. 核对新的注册身份、相同 ARS 配置、Ready listener 和正容量，再执行第二轮相同 E2E。

## 对照结果

| 对照 | 身份变化 | 普通 E2E 结果 |
| --- | --- | --- |
| 只重建 listener | listener UID 更换，ARS UID 不变，GitHub scale set ID 保持 `25` | 退出 1：启动约 9 分 28 秒后成功，但 held 在 resume 后 600 秒内未完成 |
| 重新注册 scale set | ARS UID 更换，GitHub scale set ID `25 → 26`，listener 再次更换 | 退出 1：inflight 在 600 秒内未就绪，没有进入 drain |

重新注册前后完整 ARS `spec` 相同，见 `scale-set-reregister/identity-check.json`。
第一轮新 listener 的启动日志已出现 broker `Getting next message` 轮询，没有 ERROR 记录。

### 第一轮：重建 listener，保留 ID 25

PR [93](https://github.com/kuring/ci-infra/pull/93)，原始
[run 40529015817](https://github.com/kuring/ci-infra/actions/runs/40529015817)。

| 北京时间 | 证据 |
| --- | --- |
| 23:14:10 | 专用 AutoscalingListener 删除，随后重建 |
| 23:14:43 | 找到原始 run；inflight 保持 queued |
| 23:24:11 | inflight 就绪，进入 drain，距发现 run 约 9 分 28 秒 |
| 23:24:21 | limits 和 advertised capacity 同时归零 |
| 23:24:42—23:25:42 | 三个 held 完成完整 60 秒排队观察 |
| 23:25:43—23:25:44 | 原 worker 加 refresh taint，启用预留 worker，恢复容量，taint 保留检查通过 |
| 23:25:56 | 上游发送一次内置 held wakeup |
| 23:35:44 附近 | 600 秒完成预算耗尽，随后清理并退出 1 |
| 23:35:46 | 最后一次连续采样中，三个原 held 仍 queued、runner_id=0，无实际 runner Pod |
| 23:35:57 | 结束快照出现 3 个 Running runner Pod，均在预留 worker；容量暂为 0，之后回收并恢复 |

原始 inflight job `122276364408` 使用 runner `175`，最终 success。
三个原 held job `122282217888`、`122282218015`、`122282218096` 最终 cancelled、runner_id=0。
它们没有被重跑或替换，也没有完成严格的 held runner/workflow Pod 放置与成功断言。
超时/取消附近才出现的 runner Pod 不能计为通过，也不足以证明取消动作唤醒了旧队列。

采样还显示，drain 和 resume 期间 listener 自动重建：

- 测试前：`72141e5e-b36d-4b2e-a4d3-98992bf3f4a8`。
- drain 后：`c8a61a49-d422-4f74-ad2f-4ca5ae670698`。
- resume 后：`388cf5f5-e21f-48bd-b05d-e9aa9193daa8`。

整个第一轮 GitHub scale set ID 均保持 `25`。
共保存 119 次成功采样；23:24:14 的一次采样因 listener 切换缺少 Ready listener 而失败，
该缺口保存在 `sample-0055-error.json` 中。

### 第二轮：重新注册为 ID 26

PR [94](https://github.com/kuring/ci-infra/pull/94)，原始
[run 40532833009](https://github.com/kuring/ci-infra/actions/runs/40532833009)。

23:37 完成专用 release 注销与重装，新 ARS UID 为
`8899a493-1b46-4f54-b60f-6c33e1b4d0d0`，GitHub scale set ID 为 `26`，
新 listener UID 为 `efb63ae4-0961-4710-91d4-0704ccef46a9`。
23:37:44 找到原始 run，23:47:44 启动预算耗尽；控制器没有进入 drain。

59 次成功采样中，原 inflight job `122290006889` 始终 queued、runner_id 为空或 0，
limits 和 advertised capacity 均为 3，没有实际 runner/workflow Pod，没有采样错误。
原始运行最终 completed/cancelled。

## 清理、证据与解释限制

两轮 PR 均关闭，精确分支均删除，3 个本轮 workflow run 均 completed/cancelled。
终态审计在 23:49:15 通过：当前注册 ID 为 `26`，maxRunners=3、advertised capacity=3，
没有实际 runner/workflow Pod 或后台测试进程。原 worker 无 refresh taint，预留 worker 回到
`kind-reserve`；重置发生在第二轮新测试的准备阶段。

第一轮结束时，对照脚本最初将瞬时零容量断言为立即失败，未继续等待。
保留该中间失败后，修正临时编排脚本为有界健康等待，再确认容量和资源清理完成，才开始第二轮。
普通 E2E 的断言、超时与失败结果均未修改；`compare.py`、`resume.py` 和原始失败日志均归档。

远端：`/root/drain-session-compare-20261027/`。
本地：`.state/session-compare-20261027/evidence/`，含每步命令、前后身份、listener 日志、
逐次容量/任务/Pod/节点快照、终态 job 状态、清理检查和 `terminal-audit.json`。
归档已下载并核对哈希，未包含 PAT 或环境凭证。

```text
完整 evidence.tar.gz:
51dab4149dae85298a4e5716326be23b19102c277ca04257b87c60928d417844

第一轮独立 first-stage-evidence.tar.gz:
a5c3b69c611ac26359411056184e5f4be1f6c3a65825ffa9c1ad1025f739ae4d
```

本轮表明，新 listener 和新注册身份下仍可出现长时间排队，不能用“旧 listener/旧注册 ID 残留”
作为唯一解释。没有定位到 GitHub broker、ARC listener 或其他任务分配环节的具体根因；
单轮顺序对照也不构成严格的因果证明。本轮没有执行 AWS staging 验收。
