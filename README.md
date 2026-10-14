> 当前状态：Docker 与独立检查 App 已删除。公开变体同作业组合审查 → 私有域协调器合并/回流 → 产品指针重新检查/合并/回流，已在真实隔离仓跑通，见 [验收记录](docs/LOOP_ACCEPTANCE.md)。正式仓登记仍禁用，未切换生产。

# atelier-sync

Atelier 协作协调器：GitHub 默认分支唯一权威，Forgejo 主线镜像；双方开发通过 GitHub PR 汇入权威主线。

当前为本地重构交付，尚未正式切换。registry.json 的 enabled、protection_ready、auto_merge 全部默认 false，publish-engine 对端未确认。不要把一次绿色只读运行当作启用成功。

## 入口

| 文件 | 作用 |
|---|---|
| coordinator.py / coordination/ | 活引用对账、任务状态、主线/tag 回流、PR 与自动合并准入、子模块指针 PR |
| registry.json | 稳定仓库 ID、可见性、默认分支、所有权、受信 CI 配方及激活门禁 |
| .github/workflows/bisync.yml | 五分钟兜底、手动、事件提示；共同并发控制 |
| .github/workflows/trusted-ci.yml | 私有仓 prepare → 无凭据 candidate → 受信 receipt |
| templates/trusted-ci-publish.yml | 业务仓元数据状态发布 |
| .github/workflows/release.yml | 已审核 core tag 的隔离构建与独立发布 |
| audit.py | 双方引用/PR 与可恢复对象备份 |
| stage.py | 从备份生成隔离 checkout、完整 patch 和摘要，不提交、不推送 |
| migration.py | 显式旧任务采用，生成本地待审状态文件 |
| bisync.py | 兼容文件名入口，调用新协调器；旧双向主线算法已移除 |

模板旧通知入口不再使用高权限 token。完整流程和配置见 [实现契约](docs/architecture.md)、[迁移清单](docs/MIGRATION.md)、[验收报告](docs/VERIFICATION.md) 与 [已批准方案](docs/plan-v1.1.md)。

## 本地验证

使用现有 venv，不修改项目依赖锁：

    /home/Architecture/.venv/bin/python -m unittest discover -s tests -v
    /home/Architecture/.venv/bin/python coordinator.py --dry-run --local-auth

默认只读。--apply 需要 COORDINATOR_ENABLED=true 和远端持久状态，拒绝 --local-auth/--local-state。全部登记项禁用时报告 disabled，不代表真实仓库检查通过。

## 日常协作

新任务先登记源平台和分支前缀；历史任务显式采用。Forgejo 映射分支只由协调器写，冲突/base 更新回到 Forgejo 源分支解决。GitHub 原生分支自行更新。

PR 的规范告警不阻断；必要测试和组装失败阻断。最新 base 未进入源分支、缺少审批、受信状态过期或不能核对保护规则时自动合并保持暂停。只申请平台原生自动合并，不直接 merge 或 push GitHub 主线。

主线分叉、tag 冲突、源历史重写、可见性变化均要求人工迁移处理。关闭任务不循环重开，删除源分支不复活。发布和生产部署与同步切换分开授权。

repos.toml 仅保留为旧清单参考，新代码不读取它。

