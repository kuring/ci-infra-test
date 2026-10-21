# 本地 slow-tests 耗时分析（2026-10-14）

执行节点 `i-t4ne3fqx0h2huvw840vm`，8 核 / 16 GiB，保持 12 个任务、
GitHub matrix max-parallel=3、ARC maxRunners=3。
分析依据：[本轮 Actions run](https://github.com/kuring/ci-infra/actions/runs/38564167657)
的 jobs / steps 时间戳、Kubernetes Events、listener 指标和宿主机用量。

| 环节 | 实测 | 判断 |
| --- | --- | --- |
| GitHub 任务分配 | 10/12 成功后，2 个任务持续 queued，最终达到 15 分钟超时 | 当前最大的等待点；根因尚未确定 |
| runner 镜像冷拉取 | 543,065,893 字节，首次约 96 秒 | janitor 清理节点缓存后，下一轮会再次公网拉取 |
| 单任务执行 | 30–61 秒，中位数 41.5 秒 | 工作本身用时相对较短 |
| Initialize containers | 8–34 秒 | 首批较慢，后续多为 8–10 秒 |
| Checkout pytorch | 9–24 秒 | 可以优化，但优先级低于分配等待和镜像拉取 |
| compactor 功能 E2E | 5 项，101.12 秒 | 包含真实冷却和控制器重启，不宜随意缩短覆盖 |
| janitor 功能 E2E | 5 项，46.85 秒 | 已通过；镜像重新导入的恢复步骤也通过 |
| 完整 unit tests | 约 60 秒，覆盖率 98.84% | 后台独立运行，没有阻塞 load 分配 |

当前 `gha_capacity_advertised_max_runners=3`、`gha_capacity_running_pairs=3`、
`gha_assigned_jobs=0`、`gha_capacity_unschedulable_placeholders=0`。三个 worker 的
CPU 使用率约 0.5%–1.8%，宿主机内存约 3.6 GiB 已用、11 GiB 可用。
这组证据不支持通过更换更大 ECS 来解决当前排队停滞。

整段调试还包含宿主机 inotify 修复、Python 文件权限修复、ARC 测试隔离、
客户端证书适配、janitor 本地镜像恢复，以及 Workbench 临时访问中断。
这些一次性的适配成本应与每轮测试耗时分开判断。

## 候选优化方案（未实施）

两条路线均保留 12 个真实任务、ARC maxRunners=3 和原来的结果校验：

1. 在宿主机缓存固定版本 runner 镜像，load 前向 kind 预载；janitor 仍真实清理节点缓存。
2. 记录每个阶段的耗时，及时显示进度；对已无活动 runner、容量可用、GitHub 仍 queued
   且持续无进展的状态提前报错和清理，避免等待整个 15 分钟超时。
3. 可选择再做一次取消 matrix.max-parallel 的诊断实验，仅由 ARC 控制 runner 并发。
   这用于验证矩阵调度与 ARC 动态容量交互的假设，尚未证实是根因或最终修复。

用户选择暂不优化。保持原并发方案，以上运行配置优化尚未应用。
本轮 load 已按失败记录，未执行的串行后续项没有记为通过；compactor / janitor 的
通过证据来自单独 E2E 执行。超时后已取消剩余任务、关闭 PR #47 并删除临时分支。
原始 jobs / steps 计时保存在 `.state/slow-tests/ecs-load-timings.json`。
