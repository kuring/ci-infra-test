# 取消恢复与固定分支回收验收（2026-10-22）

本轮只在 `ci-infra-test` 增加测试、入口与说明，没有修改 `ci-infra` 源码。
用户选择方案 B：Linux 跨进程集成回归，加真实 Kind / GitHub 验证。

## 入口与结果

从 `osdc/kind/` 执行：

```bash
just kind-drain-recovery-unit
just kind-drain-recovery --output "$PWD/.state/recovery-acceptance"
```

输出目录必须不存在。真实验收要求 Linux、已有专用 Kind 集群及 PAT；macOS 的第一条
命令自动使用 Docker。请勿同时执行其他 drain/probe/install/reset 操作。

| 检查 | 结果 |
| --- | --- |
| 新增 Linux 集成回归 | 6/6 通过；本机 Docker 与 ECS 原生各运行一次，无跳过 |
| 原有上游 drain + Kind adapter 单测 | ECS 247/247 通过；本机最终 247/247 通过，5.97 秒 |
| 真实 SIGTERM 后独立进程恢复及固定分支复用 | 通过 |
| 真实 SIGKILL 留下 recipe 后，跨进程 fence / 恢复 / 分支复用 | 通过 |
| 真实“有固定分支、无 PR”残留回收及新 canary 创建 | 通过 |
| Ruff check / format | 新增三个 Python 文件均通过 |
| 独立终态审计 | 通过；源码快照、GitHub、容量、writer 与 Pod 均核对 |

六项集成回归分别覆盖 SIGINT、SIGTERM、忽略 TERM 后强制停止、recipe leader 已退出
的孤儿后代，以及有 PR / 无 PR 两种分支残留。进程、信号、Linux `/proc` 与恢复文件
均为真实对象；仅外部 GitHub / Kubernetes 使用持久化测试替身。还检查了无关进程组存活、
区域排除项保持零、恢复后不被存活 writer 再次写零、相似名称的分支不被删除，以及
取消运行 → 关闭旧 PR → 删除旧 ref → 创建新 canary 的顺序。

## 真实环境与最终复验

ECS：`i-t4ne3fqx0h2huvw840vm`。复用专用 `kind-osdc-drain-local`，两个节点均 Ready，
scale set 为 `osdc-kind-drain`。未部署或替换 ARC 镜像，没有修改共享 staging。

使用当前上游 `DrainTest.run`、`run_controller`、恢复实现和 drain/resume recipes；
适配层只固定专用测试分支，并在确认 drained 后暂停主线程，供父进程发送取消信号。
恢复进程独立启动，读取相同恢复文件；没有模拟 `/proc` 或拦截 `killpg`。

首轮三个真实场景全部通过。随后补充“最终清理后再写 passed”及 runner/job Pod 退出检查，
对最终代码重新完整执行，17:50（北京时间）完成。以下只列最终复验：

| 场景 | 控制器 PID → 恢复 PID | 原 PR / workflow | 回收后新 PR / workflow |
| --- | --- | --- | --- |
| SIGTERM | 4069754 → 4073784 | [63](https://github.com/kuring/ci-infra/pull/63) / [39757630939](https://github.com/kuring/ci-infra/actions/runs/39757630939) | [64](https://github.com/kuring/ci-infra/pull/64) / [39757774282](https://github.com/kuring/ci-infra/actions/runs/39757774282) |
| SIGKILL | 4082020 → 4085507 | [65](https://github.com/kuring/ci-infra/pull/65) / [39757968369](https://github.com/kuring/ci-infra/actions/runs/39757968369) | [66](https://github.com/kuring/ci-infra/pull/66) / [39758126150](https://github.com/kuring/ci-infra/actions/runs/39758126150) |
| 无 PR 的孤立分支 | 新进程 4091960 | 仅创建 ref，未创建 PR | [67](https://github.com/kuring/ci-infra/pull/67) / [39758333060](https://github.com/kuring/ci-infra/actions/runs/39758333060) |

取消前确认 limits 与 advertised capacity 均为零；取消后确认没有 inline resume，旧 PR
仍打开。SIGKILL 后还确认真实带 token 的 recipe 进程组仍存在。新恢复进程停止全部 writer
后恢复 limits=3、advertised capacity=3，并持续复查容量未被写回零。restore-only 保留旧
PR/ref；再由下一进程调用上游清理方法，验证旧资源回收后可以在同一分支创建新 canary。

两个完整轮次的最终独立审计：

- 临时 PR 58–67 共 10 个，全部 closed。
- 专用固定分支 `codex/osdc-drain-recovery-kind` 已删除。
- 对照分支 `codex/osdc-drain-recovery-kind-other` 在测试中内容未变，结束后删除。
- 13 个 owned workflow 全部 completed：11 cancelled、2 success。取消场景按预期中止原任务，
  不能把这些结果表述为正常 drain E2E 的原始四任务全部成功。
- 四份恢复文件对应的存活 writer 进程组均为零；实际 runner/job Pod 为零。
- limits=3、advertised capacity=3；minRunners 与 listenerTemplate 保持原值。

## 源码与异常记录

验证源自 `ci-infra` 的 `feature/drain-test`，HEAD `50af5160eeca3c546f07098f1817bd5b9d3fb29d`。
最终快照逐文件校验 659 个源文件，三个新增测试 Python 文件与本机最终版本一致。
运行期间本机 `ci-infra/osdc/integration-tests/scripts/python/phases.py` 出现任务之外的改动，
本任务未写入或恢复它。下载后与本机逐文件对比，快照中仅该文件有差异；远程验收使用
固定快照，本机最终 247 项则在保留该额外改动的工作区执行。

本机额外一轮重跑曾出现 246 passed / 1 failed：原有
`test_stop_drain_terminates_real_descendant_after_leader_exit[True]` 中，`ps` 调用只剩约
0.026 秒预算，超时后的 `killpg` 返回 macOS `PermissionError: Operation not permitted`。
同一用例定向重跑 1/1 通过，随后完整 247/247 通过；未修改上游用例、实现、超时或断言。
ECS 的同套 247 项通过。初次远程启动还遇到非 login shell 缺少 `uv` 的 PATH 问题，
补齐工具路径后原生六项集成回归通过。

## 证据与边界

[证据目录](../.state/recovery-20261022/evidence/) 包含首轮及最终轮次的进程日志、
恢复文件、容量快照、PR/run ID、源码 manifest、退出码和 `final-audit.json`。
完整归档位于 `.state/recovery-20261022/evidence.tar.gz`，下载后 SHA-256 与远端一致：

```text
a6807d145f6fb6942500189e7ebea5b6e7877661579949c370da37779d8fd019
```

归档未包含 PAT 文件，已扫描 `github_pat_` / `ghp_` 模式。
本机最终单测日志：`.state/recovery-20261022/local-final-unit.log`。

本轮验证 runner 发信号后的真实进程交接和专用 Kind / GitHub 资源回收，不验证 GitHub
Actions 服务端取消调度、job concurrency 锁释放或 AWS staging。没有提交或推送实现代码。
