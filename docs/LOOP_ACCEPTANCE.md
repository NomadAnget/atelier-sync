# 开发流程真实闭环验收

日期：2026-10-14。用户要求删除 Docker、独立检查 App，仅补接现有开发流程。

结论：真实 GitHub / Forgejo 隔离仓的单域、单公开变体完整功能闭环通过。正式 Atelier 仓库尚未切换，生产未改动；T30、T37、T42 按用户要求暂缓重验，本报告不将其改记通过。

## 实际完成的链路

1. 私有域候选 `acceptance/loop-domain-change` 自动进入 [私有域 PR #4](https://github.com/NomadAnget/atelier-automation-acceptance-20261014/pull/4)。
2. 协调器自动建立 [公开草稿组合 PR #13](https://github.com/NomadAnget/atelier-automation-fixture-20261014/pull/13)，只发布私有候选的 gitlink。
3. [公开单作业 CI 38518056742](https://github.com/NomadAnget/atelier-automation-fixture-20261014/actions/runs/38518056742) 准备准确源码及子模块，安装固定 core 0.2.6，并执行组合入口及两项真实合成域测试。[结果发布 38518080275](https://github.com/NomadAnget/atelier-automation-fixture-20261014/actions/runs/38518080275) 成功。
4. 协调器根据准确组合回执自动合并私有域 PR #4，随后准确回流 Forgejo。
5. 协调器自动创建 [产品指针 PR #16](https://github.com/NomadAnget/atelier-automation-fixture-20261014/pull/16)，重新执行 [CI 38518203477](https://github.com/NomadAnget/atelier-automation-fixture-20261014/actions/runs/38518203477) 及 [结果发布 38518229900](https://github.com/NomadAnget/atelier-automation-fixture-20261014/actions/runs/38518229900)。
6. 协调器经现有严格 Ruleset 和准确回执准入自动合并产品 PR #16，并准确回流 Forgejo。

[完整协调器作业 38518013492](https://github.com/NomadAnget/atelier-automation-fixture-20261014/actions/runs/38518013492) 成功，最终 accepted=true。域和产品的两次功能合并均由运行中的协调器调用带期望 head SHA 的平台合并 API 完成，未由助手手动代替。安装和修正测试工作流的 bootstrap PR 是单独人工审核合并，不能计作自动功能合并。

## 最终独立核对

| 对象 | GitHub 与 Forgejo 共同 SHA |
|---|---|
| 私有域主线 | `2cddadb3eff25f7826172579e8ed88adc1c14308` |
| 公开产品主线 | `4a35d1a22762bc7cf9f253f8569fbcb25db6fa9d` |
| 产品 `src/domain` gitlink | `2cddadb3eff25f7826172579e8ed88adc1c14308` |

Forgejo 镜像为 `Carnation/atelier-loop-domain-20261014`（ID 30）与 `Carnation/atelier-loop-product-20261014`（ID 31），均为私有隔离合成仓。没有改变原保护测试仓或业务仓的主线。

## 源码与凭据范围

- 公开产品树中 `src/domain` 仅为 mode 160000 的 gitlink，没有域文件正文；公开仓不存在 `.github/loop-control` 控制代码。
- 控制代码固定于私有测试仓提交 `dcf9269e308eca055a27fde339760552e1b5fdc7`，源码准备及测试位于临时托管 runner。没有源码 artifact 或源码缓存；五个成功的协调、CI、发布 run 的 GitHub artifact 数均为 0。
- 复用用户添加的既有 GH_SYNC_TOKEN / FORGEJO_SYNC_TOKEN，没有改变 token scope，没有创建 App。测试子进程清空凭据环境变量，不声称具备容器或操作系统级隔离。
- 自动审批曾拒绝完整协调器代码向公开测试仓外发；该上传未执行，改为私有保存。公开仓仅接收合成夹具、工作流与指针。

## 本轮修复与验证

- 删除 Docker 构建、执行、清理代码及独立 App 模块、配置、专用测试，清理旧容器源码副本和实现脚本；历史失败日志保留。
- 补接私有域前置变体组合审查、回执消费与受检查约束的合并；沿用现有主线回流和父仓指针流程。
- 修正全绿后原生 auto-merge 请求被拒的问题，使用已通过准入的带 head SHA 合并 API。
- 修正隔离测试适配器错误使用只读 GITHUB_TOKEN 执行写操作。
- 真实运行发现不同文件同时提交同一状态分支会触发 GitHub Contents 409：仅在目标文件 SHA 未变化时有限重试；实际同文件 CAS 冲突仍拒绝。新增回归覆盖两种情况。
- 当前 123 项本地回归、公开单作业模板 actionlint 和 git diff --check 通过。测试范围为合成域和真实固定 core 安装，不等同于三正式变体的全部业务配方或多子仓交错验收。

证据保存在 `tmp/live-loop/accepted.json`、`accepted.log`、域与产品 ledger 快照。首次权限失败、状态并发失败和测试适配器部署遗漏的运行记录保留，没有覆盖或改记成功。

