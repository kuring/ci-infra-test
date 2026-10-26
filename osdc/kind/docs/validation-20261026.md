# drain-test 完整性验证（2026-10-26）

上游固定为 `ci-infra` 提交 `5b3f952bca92b268a566d3b9f2a61436aa4e89e1` 的源码快照。
执行主机为 ECS `i-t4ne3fqx0h2huvw840vm`（Linux x86_64），复用专用
`kind-osdc-drain-local`。远端工作目录为 `/root/drain-integrity-20261026`。

## 已执行结果

| 检查 | 结果 |
| --- | --- |
| 上游 drain + Kind 适配单测、工作流 dry-run | 通过 |
| Linux 原生进程恢复集成 | 6/6 通过 |
| 真实容量 probe | 上限与 advertised capacity 完成 3 → 0 → 3 |
| 真实恢复验收 | SIGTERM、SIGKILL 孤儿 writer、仅分支残留均通过；对照分支保留 |
| GitHub 服务端取消 | 同 job 恢复步骤成功；successor 在恢复后获得 concurrency 锁 |
| 普通 E2E 首轮 | inflight 等待 600 秒未调度，启动失败 |
| 普通 E2E 第二轮 | drain、inflight 完成、60 秒 held 排队、相同 UID 节点的 taint 保留均通过；held 完成超时 |

首轮 PR 为 [70](https://github.com/kuring/ci-infra/pull/70)，run 为
[40298397010](https://github.com/kuring/ci-infra/actions/runs/40298397010)。
第二轮 PR 为 [78](https://github.com/kuring/ci-infra/pull/78)，run 为
[40301488037](https://github.com/kuring/ci-infra/actions/runs/40301488037)。
第二轮 scheduler 事件明确记录 placeholder Pod 无法容忍 worker 的
`deploy.osdc.io/refresh-pending=true:NoSchedule`。Kind 唯一 worker 保留 taint，
且没有自动节点供给，恢复后的本地容量无法重新提供。
失败后测试新增 taint 已清除，上限恢复为 3，临时 PR/分支已清理。

服务端取消 PR 为 [76](https://github.com/kuring/ci-infra/pull/76)，原 run 为
[40300916534](https://github.com/kuring/ci-infra/actions/runs/40300916534)，successor 为
[40301066604](https://github.com/kuring/ci-infra/actions/runs/40301066604)。
恢复 step 于北京时间 17:38:33 开始、17:39:14 完成；successor job 于 17:39:21 开始。
临时 native runners 已注销，owned runner/job Pods 已回收。

## 本地适配与覆盖边界

本项目补齐新的 `cluster_id`、workflow label/group 接口以及恢复测试替身的准备阶段。
经用户选择方案 2，本地 runner 与 workflow Pod 增加精确 refresh-taint toleration。
共享控制器的 taint 保留、UID 所有权与清理断言继续执行。
该方案允许任务在保留 taint 的原 worker 上继续运行，不验证 taint 的调度隔离、
Karpenter 供给或节点替换，不替代 AWS staging 验收。

toleration 部署后的复测使用 PR [79](https://github.com/kuring/ci-infra/pull/79)，run 为
[40321113166](https://github.com/kuring/ci-infra/actions/runs/40321113166)。
本地另有并发修改，因此远端继续使用上述固定上游快照。
本轮于北京时间 20:15:25 找到原始 run，inflight 在 600 秒内仍未分配，
测试以启动超时退出 1，未进入 drain/taint 阶段。
因此 toleration 已部署并经单测验证，但完整普通 E2E 仍未通过，
本轮不能证明 taint 后的任务恢复已被该适配解决。
退出后审计确认：上限为 3，worker 无 refresh taint，PR 79 已关闭，
临时分支不存在，原始 run 已 completed/cancelled。

原始日志位于远端 `evidence/`；恢复和服务端取消结果分别位于
`ci-infra-test/osdc/kind/.state/recovery-acceptance-20261026/` 与
`ci-infra-test/osdc/kind/.state/service-cancel-acceptance-20261026/`。
