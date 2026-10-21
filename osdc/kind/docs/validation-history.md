# 本地验证与诊断记录

以下为 2026-10-13 的历史记录，不代表当前环境状态。所有命令均从 `osdc/kind/` 执行。
返回[部署指南](../README.md)。

## 已验证记录（2026-10-13）

| 项目 | 结果 |
| --- | --- |
| 相关离线用例 | 105 个通过，覆盖率 99.59% |
| 生成工作流与相关 Python 检查 | 通过 |
| 容量 probe | 通过 |
| 运行中的真实任务在 drain 后完成 | 通过 |
| drain 期间三个后续任务保持排队 | 通过 |
| undrain 后三个任务完成 | **未通过：600 秒内未获分配** |
| 跳过 drain 的反向控制 | 符合预期，上限未归零而失败 |
| 真实任务运行且上限归零后发送 SIGTERM | 通过，退出码 143，上限恢复为 3，工作流取消，PR/分支清理 |

等待脚本已支持响应体截断、读取超时和连接重置的有限重试；权限错误仍直接失败。
本地三组预留容量与 uw1 的冷启动、队列分片及 AWS 节点供给不同，
本指南的结果不能替代 staging 验收。所有本地实现修改均未提交。

### 旧排队任务的路由诊断

2026-10-13 在 `kuring/ci-infra` 进行了以下对照实验：

| 实验 | 观察结果 |
| --- | --- |
| 相同工作流不执行 drain | 运行中的任务和三个矩阵任务均成功 |
| drain 后恢复容量 | 三个旧任务仍排队；scale-set 的 available/acquired/assigned 统计为 0 |
| 重建 listener、重新注册 scale set | 均未恢复旧任务分配 |
| 恢复后创建一个新任务 | 新任务成功，说明恢复后的 ARC 可以接收并执行任务 |
| 注册同标签的临时普通 self-hosted runner | 三个旧任务随后全部由 **ARC runner** 执行成功，普通 runner 未执行任务 |

最后一项使用临时 [PR #18](https://github.com/kuring/ci-infra/pull/18)，
[运行记录](https://github.com/kuring/ci-infra/actions/runs/38342236702) 中保留了原任务 ID 和实际 runner 名称。
北京时间 11:37:28 普通 runner 开始监听，11:37:29 ARC listener 收到分配并扩容到 3，
11:37:40–41 三个旧任务开始执行，11:38:02 前全部成功。
期间没有修改工作流、重跑任务或再次调整 ARC 上限。

这个结果不支持“旧任务只能被普通 runner 领取”的假设；它支持“注册 runner 后触发了旧任务
重新匹配”的解释。GitHub 内部队列和匹配缓存不可见，因此尚不能断言具体是缓存失效、
队列唤醒缺陷，或仓库级注册与 scale-set 路由交互。增加普通 runner 是诊断干预，
不能把经过该干预的成功记录当作正常 undrain 验收通过，也不应直接作为生产修复。

另一个实验使用 `runs-on` 的 `group: Default` 时，GitHub 返回
`Required runner group 'Default' not found`。本地个人仓库继续使用 scalar scale-set 名称；
不能将这种配置直接等同于 staging 的组织级 runner group。

诊断快照与日志保存在权限受限且被 Git 忽略的 `.state/diagnosis/`。
临时普通 runner 已注销，其 Pod、注册 token Secret 和临时分支已删除，PR 已关闭；
本地上限与对外上报容量均恢复为 3，两个 kind 节点均为 Ready。

### 分阶段控制变量实验

随后通过临时 [PR #19](https://github.com/kuring/ci-infra/pull/19) 和
[运行记录](https://github.com/kuring/ci-infra/actions/runs/38343322991) 进一步区分注册与上线：

| 阶段 | 结果 |
| --- | --- |
| 容量恢复为 3，旧任务继续等待 | 三个任务仍未分配 |
| 只注册普通 runner，保持 offline，唯一标签为随机的不匹配标签 | 观察 60 秒仍未分配 |
| 启动这个 runner 的监听进程，标签保持不匹配 | 三个旧任务随即获得 ARC runner，并全部成功 |
| 为普通 runner 添加匹配标签 | 未执行；上一阶段已经触发分配，避免引入额外变量 |

普通 runner 的唯一标签是 `osdc-unmatched-46189f8245784fbc8c3e8a5f610cad21`，
没有默认标签，也没有 `osdc-kind-drain`。它全程空闲，没有执行测试任务。
北京时间 11:52:09 普通 runner 开始监听，同一秒 ARC 收到三个任务的分配；
11:52:21–22 三个旧任务开始执行，11:52:42 前全部成功。
工作流、任务 ID、ARC 版本、容量上限和预留配置均未改变，也没有重跑工作流。

这将触发条件进一步缩小到普通 runner **上线**这一阶段，而非匹配标签或仅注册。
GitHub 内部的队列唤醒机制仍不可见；容量指标为 3 也不能直接证明每次消息请求发送了什么头部。
同版本关闭容量感知的对照已完成，结果见下一节。
本轮临时 runner、Pod、Secret、PR 与分支均已清理。

### 同版本关闭容量感知对照：通过

经确认后，仅临时将 `CAPACITY_AWARE_ENABLED` 改为 `false`，ARC 镜像继续使用
`0.14.1-jeanschmidt.20`，runner 镜像、工作流、上限、标签和网络配置均保持原值。
没有注册普通 runner，也没有重跑原任务。

使用临时 [PR #20](https://github.com/kuring/ci-infra/pull/20)，
[运行记录](https://github.com/kuring/ci-infra/actions/runs/38344507395) 显示：

| 检查 | 结果 |
| --- | --- |
| 静态 listener 的容量感知开关关闭，上限为 3 | 通过 |
| drain 后 ARS 与 listener 上限归零 | 通过 |
| 已运行任务正常结束 | 通过 |
| 三个后续任务在 drain 期间保持排队，观察 60 秒 | 通过 |
| undrain 后自行获得 ARC runner 并成功完成 | **通过** |
| 恢复容量感知开关与原有正容量 | 通过 |

北京时间 12:04:31.772 新 listener 开始轮询，12:04:34.688 收到三个旧任务的分配；
12:04:44–46 任务开始执行，12:05:05 前全部成功。
静态模式不使用容量感知指标来证明 drain；它独立核对 ARS/listener 上限和真实任务状态，
没有放宽或修改正式容量感知测试的断言。

同一镜像下开启容量感知时旧任务阻塞，关闭后旧任务自行恢复，因而故障与容量感知路径
及其和 scale-set 消息轮询的交互有关，不能直接归结为普通 runner 标签或 kind 资源不足。
目前检索到的 fork 源码中，容量感知启用时 listener 初始上限为 0，监控器随后通过
`SetMaxRunners` 更新容量；SDK 在下一次 `GetMessage` 请求时读取该值并设置请求头。
静态模式则以配置上限直接启动。这个启动及容量更新差异是下一步重点，
但尚未抓取实际请求头，也没有将上述候选机制当作已证实的底层根因。

12:05:47 已验证恢复 `CAPACITY_AWARE_ENABLED=true`，指标再次为 3。
本对照只用于定位问题，正常容量感知模式的 undrain 验收仍未通过；不会永久关闭该功能。
实验命令（会临时修改配置，结束后恢复）：

```zsh
HTTPS_PROXY=http://127.0.0.1:13660 NO_PROXY=localhost,127.0.0.1 \
  mise exec -- just --justfile diagnosis/recipes/capacity-control.just run-control
```

### 命令行执行与本地模拟的边界

当前的 `just kind-drain test` 已经通过命令调用 GitHub API 创建临时 PR 和真实 Actions 任务，
不是浏览器自动化。`just kind-drain probe` 只验证真实 Kubernetes/ARC 的容量变化，
不会创建 GitHub 工作流，也不覆盖旧任务重新分配。

`act` 使用 Docker 在本地执行工作流步骤，适合验证脚本、容器和步骤依赖，
但不会经过 GitHub 的 runner 匹配与 scale-set broker，无法复现本次服务端队列行为。
离线单元测试则使用 mock 检查 drain/resume 控制逻辑与失败处理；它们不能代替真实调度验收。

`gh workflow run` 需要工作流声明 `workflow_dispatch`。
当前临时 drain 工作流使用 `pull_request`，应通过 `just kind-drain test` 创建并触发，
不能直接用 `gh workflow run drain-test.yaml` 启动。

参考：[act 工作原理](https://nektosact.com/introduction.html)、
[GitHub CLI workflow run](https://cli.github.com/manual/gh_workflow_run)。
