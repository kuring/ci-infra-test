# 验证记录（2026-10-14）

执行节点：`i-t4ne3fqx0h2huvw840vm`，新加坡 `ap-southeast-1`。
使用独立目录 `/root/osdc-slow-tests-20261014-01a139b2/ci-infra`，源码基线
`ddaf80a70579457d0ee577d699f77873c7e04ab1`，叠加本地 slow-tests 适配。
既有 `/root/ci-infra`、`/root/arc-test` 和 `arc-test-79ffd92df47f` 集群保留。

## 已完成

- ECS 上最新完整 `just test` 通过，退出码 0，整体覆盖率 98.84%。
- 新适配代码的 Ruff check / format check 通过。
- `recipes.just` 已成为独立入口，上层 import 仍兼容。32 项真实 just 调用检查通过，
  覆盖两种入口、路径含空格和参数转发；ECS 的独立 load 预览通过，未触发 GitHub 任务。
- 本地 compactor Dockerfile 的 hadolint 通过。
- GitHub 仓库读取成功，现有 token 的 `permissions.admin=true`。
  修复了仓库接口末尾多余 `/` 导致的 404，并增加回归检查。
- uv 构建引用已改为
  `mirror.aone.alibaba-inc.com/mirrors/ghcr.io/astral-sh/uv:0.10.12`。
- 全仓 `just lint` 13/14 通过；上游
  `base/docker/runner-base/Dockerfile:36` 的 `DL3066` 提示导致 hadolint 失败。
  该 Dockerfile 未修改，不能将默认完整 lint 记为通过。

## 部署进度（真实 E2E 尚未完成）

- 专用集群为 `osdc-slow-local`，1 control-plane + 3 worker。
- kubelet 的 `inotify_init: too many open files` 已解决：按用户选择持久设置
  `fs.inotify.max_user_instances=512`，原值 128 已备份。四个节点全部 Ready。
- ECS 无法访问内网镜像域名。按用户选择，官方 uv 同版本 0.10.12 已导入并
  缓存为指定内网名称，Dockerfile 保留内网引用。
- ECS 的 ARC 安装使用 `OSDC_KIND_ARC_PUBLIC_IMAGES=true`，仅生成 ECS 专用
  公网镜像 values，controller 和 runner 版本不变，本机模板不修改。
- ECS 上 88 项离线检查通过，覆盖率 98.65%；最新恢复修复在本机 90 项检查通过，
  总覆盖率 98.67%，逐文件均超过 95%。
  Ruff check / format check 通过。
- compactor 真实 E2E：5/5 通过，耗时 101.12 秒。按上游方式暂停专用 ARC，
  消除占位 Pod 对容量预留场景的干扰，测试后恢复原值。
- load：PR #45 的初轮已取消并清理；PR #46 的第二轮 7/12 成功，剩余 5 个
  停在 GitHub 队列，listener 上报容量 3，无活动 runner，重建空闲 listener 后仍未恢复。
  本轮也已取消并清理；不能记为 load 通过。部分证据保存在
  `.state/slow-tests/partial-load-evidence.json`。
- 保留 matrix max-parallel=3 后的第三轮（PR #47）：10/12 成功，剩余 2 个持续排队，
  最终达到 15 分钟超时。本轮 load 按失败记录，后续串行项未运行。
  超时后已取消剩余任务、关闭 PR 并删除分支。
- 用户选择暂不优化；保留原矩阵并发、镜像获取和超时策略。
- janitor 真实 E2E：5/5 通过，耗时 46.85 秒，恢复步骤也已完成，三个 Pod 全部 Running。
  显式客户端证书 transport 解决认证问题；清理后的恢复入口先停旧 GC Pod、导入本地
  镜像，再恢复选择器和高阈值，解决 containerd 清理缓存后的 ImagePullBackOff。
- 宿主机观测：使用约 3.4 GiB、可用约 11 GiB，无扩容证据。

## 当前进度

Workbench 访问已恢复，最新 janitor 恢复修复已同步并通过 ECS 验证。
ECS 上 90 项适配检查通过，覆盖率 98.67%，Ruff check / format check 通过。
真实 compactor 和 janitor 的单独 E2E 均已通过，load 验证仍未完整通过。
未更换 ECS，未修改矩阵并发方案或实施耗时优化。
卡点和候选方案见 [performance.md](./performance.md)。

最终状态检查：四个节点全部 Ready，compactor 为 dry-run，janitor 为高阈值且三个 Pod
均 Running / Ready，ARC minRunners=0 / maxRunners=3。PR #47 已关闭，分支已删除，
Actions run 已取消。证据保存在 `.state/slow-tests/ecs-evidence/`，不包含 kubeconfig 或 token。

远端完整日志位于 `/root/osdc-slow-tests-20261014-01a139b2/`：
`test-full-output.log`、`lint-full-output.log`、`prepare.log`、`create-retained.log`。
独立入口变更的复核日志：`entry-check.log`、`test-entry-output.log`、`lint-entry-output.log`。

本目录是 checkout 内的本地适配，仍由 `.git/info/exclude` 的 `/osdc/kind/` 排除。
未提交或推送本地实现代码。
