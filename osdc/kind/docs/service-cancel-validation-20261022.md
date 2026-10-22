# GitHub Actions 服务端取消验收（2026-10-22）

结果：通过。通过普通 GitHub workflow cancel API 取消真实运行，GitHub runner 收到取消消息，
drain step 被取消，同一个 job 的 `always()` restore step 成功，后续 job 在恢复结束后获得锁。
未使用本机 kill 触发验收，没有借助外部 fallback 恢复来通过测试。

## 环境与执行入口

用户选择 ECS 独立 runner + 真实 Kind，额外选择在专用 ECS 上安装系统 libicu。

- ECS：`i-t4ne3fqx0h2huvw840vm`，Alibaba Cloud Linux 4，x86_64。
- Kind：`kind-osdc-drain-local`；scale set：`osdc-kind-drain`。
- 独立 native runner 两个，使用本轮唯一标签，位于被 drain 的 ARC scale set 之外。
- Runner：官方 API 返回的 `2.337.0`；下载包 SHA-256 为
  `70920811a4f8ad4328818682bca5c6469c1c942fab52448868071d0063816613`，校验通过。
- 按用户选择安装且保留 `libicu-72.1-3.alnx4.x86_64`；该次事务仅新增这一个包。

从 `ci-infra-test/osdc/kind/` 执行，输出目录必须不存在：

```bash
just kind-drain-service-cancel --output "$PWD/.state/service-cancel-acceptance"
```

实现从当前上游 `_osdc-slow-tests.yml` 读取 job 取消条件、drain/restore step 条件及超时，
沿用 `cancel-in-progress: false`，只替换专用 runner、测试锁、Kind 连接与测试入口。
恢复文件继续通过 step-level env 使用 `runner.temp` / run ID / attempt。
恢复 step 完成容量验证后额外等待 30 秒，用于观察该 job 是否继续持有并发锁。

第二台 native runner 在取消前已确认 online 且不 busy，因此后续 job 排队不能归因于
runner 不可用。GitHub workflow 只在本轮临时 PR 分支上生成，没有修改 main、共享锁或
AWS staging，也没有修改 `ci-infra` 源码。

## 真实运行结果

| 对象 | 结果 |
| --- | --- |
| 服务端取消的 [workflow 39762231436](https://github.com/kuring/ci-infra/actions/runs/39762231436) | completed / cancelled |
| 原 job `Cancel drain and recover`（119768958515） | cancelled |
| 该 job 的 `Run drain/undrain test` step | cancelled |
| 该 job 的 `Resume and verify staging runners` step | success |
| 等待锁的 [workflow 39762382243](https://github.com/kuring/ci-infra/actions/runs/39762382243) | completed / success |
| 后续 job `Acquire released lock`（119769460360） | success，使用另一台专用 native runner |
| 真实 ARC probe [workflow 39762263921](https://github.com/kuring/ci-infra/actions/runs/39762263921) | 清理时取消；不能表述为正常 drain E2E 原任务全部成功 |

服务端工作流临时 PR 为 [68](https://github.com/kuring/ci-infra/pull/68)，ARC probe 临时 PR
为 [69](https://github.com/kuring/ci-infra/pull/69)，两者均已关闭、分支均已删除。

以下时间均为北京时间：

| 时间 | 实测证据 |
| --- | --- |
| 18:20:31 | GitHub job 内的真实控制器达到 drained checkpoint；limits / advertised capacity 均为零 |
| 18:20:52.458 | 从外部编排器调用原 workflow 的普通 `/cancel` API |
| 18:21:31 | native runner 的 JobDispatcher 记录收到服务端取消请求；ProcessInvoker 向 step PID 4153144 发送 SIGINT |
| 18:21:38 | ProcessInvoker 向同一 step PID 发送 SIGTERM |
| 18:21:43 | GitHub API 将 drain step 标记为 cancelled，并开始同 job 的 restore step |
| 18:21:55.427 | restore 验证 limits=3、advertised capacity=1（已恢复为正值） |
| 18:22:25.427 | 包含 30 秒锁观察窗口的恢复步骤结束；GitHub API 记录该 step success |
| 18:22:34.422 | 后续 job 在第二台 runner 启动并成功，比恢复结束晚 8.995 秒 |
| 18:23:23 | probe runs / Pods 清理完成，验收入口退出 0 |

恢复步骤总计约 41.37 秒。恢复窗口内 11 次独立 API 采样中，后续 job 均为 pending；
始终没有在恢复完成前启动。顺序比较使用同一 ECS 主机的 monotonic clock，不依赖不同
主机之间的时钟同步。取消请求到 runner 收到消息存在实测延迟，不能将这些时间解释为固定 SLA。

这与 GitHub 文档描述的服务端取消消息及 runner 信号升级机制相符；具体结论以本轮
服务器日志、step 结果和实际容量为依据：[官方取消机制](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-cancellation)。

## 终态与独立审计

- `observed-before-fallback.json` 记录验收在外部清场前已 passed，未执行 fallback 恢复。
- 最终 limits=3、advertised capacity=3；minRunners 与 listenerTemplate 保持原值。
- 两个临时 native runner 注册均已删除，监听进程为零，`.runner` / `.credentials` /
  `.credentials_rsaparams` 均已移除。安装包和测试日志保留用于复验。
- ownership token 对应的存活 writer 进程组为零，实际 runner/job Pod 为零。
- 两个 PR 均 closed，服务端测试分支和 probe 固定分支均不存在，相关运行已终止。
- 下载了两个服务端工作流的完整日志，并保留 native runner 的取消消息、SIGINT / SIGTERM 摘要。

## 质量检查与执行中修正

ECS 最终快照下上游 drain + Kind adapter + 新增工作流回归合计 250 项全部通过。
新增三项生成器/锁前置检查在本机通过；修改 Python 的 Ruff check / format，以及生成
工作流的 actionlint 均通过。生成器回归核对正式条件、超时、step-level recovery env、
两台不同 runner 与相同并发锁，并验证缺少恢复完成证据时后续 job 会失败。

初次环境准备因缺少 libicu 失败，尚未注册 runner 或启动实际工作流；安装用户选择的
依赖后重新执行。actionlint 还发现初稿把 `runner.temp` 放在 job-level env，已修正为
正式工作流使用的 step-level env，再次检查及真实运行通过。没有为通过测试放宽上游断言。

源码来自 `ci-infra` 的 `feature/drain-test`，HEAD 为
`50af5160eeca3c546f07098f1817bd5b9d3fb29d`，快照包含当时工作区的额外 `phases.py` 改动。
远端逐文件核对 663 个源文件；测试实现与本机最终代码一致。运行期间本机上游
`test_drain_test_run.py` 又出现任务之外的两行删除，已保留，下载后与测试快照比较仅该文件
有差异。本任务未写入这两个上游文件，没有提交或推送实现代码。

## 证据与范围

[本轮证据目录](../.state/service-cancel-20261022/evidence/) 包含：

- `acceptance-fixed/`：实际 step 结果、调度采样、恢复文件、时间标记、容量及清理结果。
- `audit/`：GitHub run/jobs API 响应、完整服务器日志、runner 信号摘要及独立审计结果。
- 原始/修正后的源码 manifest、单测与实际运行日志，以及 libicu 安装日志。

归档 `.state/service-cancel-20261022/evidence.tar.gz` 已核对远端与本机 SHA-256 一致：

```text
84f2772c13ce843f8a6d52e6db052e67b9073e7d4db5b54e5619cf6837084dd7
```

归档不含 PAT、runner 认证文件或完整 runner 私有目录，并已扫描实际 PAT 及 GitHub token 模式。

本轮覆盖 native self-hosted runner 上、drained 状态后的普通服务端取消、同 job 恢复和
并发锁释放。未测试 force-cancel、其他取消时机、GitHub-hosted runner、完整 reusable
pre-merge 编排或 AWS staging 验收。
