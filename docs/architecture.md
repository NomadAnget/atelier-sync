> 当前合并约定：公开主线受既有严格平台规则约束，协调器在准确组合检查通过后调用带 head SHA 的合并 API；私有域采用公开变体组合回执作为协调器准入。原文“仅原生 auto-merge”和私有域必须平台保护的约定已被用户后续确认的范围替代。Docker 和独立 App 已删除。

> 2026-10-14 更新：公开变体准入与同作业执行以 [VARIANT_GATE.md](VARIANT_GATE.md) 为准；下文原私有 CI 为旧执行路径与兼容实现。新路径尚未部署，子仓前置组合授权与独立检查发布身份仍待完成。

# 自动化重构实现契约

本文件补充 plan-v1.1.md，描述本次本地实现。发布和正式切换需要另行确认；当前所有登记仓库的写入与自动合并均默认禁用。

## 唯一权威与执行边界

GitHub 默认分支是权威；Forgejo 默认分支只接收其普通快进与准确版本 tag 对象。协调器没有把 Forgejo 主线推入 GitHub 主线的代码路径。独有或分叉提交、tag 冲突、引用回退均明确停止，不删除历史。

Forgejo 开发分支必须显式登记所有权，映射为 forgejo/<Forgejo ID>/g<代次>/<源分支 UTF-8 十六进制>。映射分支保持源提交 SHA，由协调器单独写入。GitHub 原生开发分支独立拥有，只创建 PR，不复制回 Forgejo。关闭、合并、删除、历史重写分别处理；旧事件只作提示，真实引用是写入依据。

所有主线改动经过 PR。第一版只申请 GitHub 原生 merge commit 自动合并；源分支必须包含最新 base。自动合并前重读适用于当前默认分支的 Ruleset、当前审批、私有受信 CI/发布回执与当前 head/base；改动工作流、配方、依赖锁、发版或 .gitmodules 的 PR 留给人工。权限不足以读取保护规则时停止自动合并，不假设保护存在。

## 私有 CI 的实施调整

三个公开父仓包含私有子模块。候选源码与测试日志放在私有 atelier-sync 中，避免把私有域源码上传为公开仓库的 Actions artifact。业务仓库只运行默认分支上的检查结果发布工作流，接收 PR 编号、SHA 和结果等元数据。

1. prepare：受保护默认分支的控制代码读取最新 PR/base 与平台合并候选；验证其父提交恰为 base、head；导出源码及每个准确 gitlink，固定 core SHA，移除 Git 元数据，拒绝危险归档路径、链接及环境文件。不会执行候选代码。
2. candidate：另一台临时托管 runner，作业无仓库权限、无同步/发包 secret。使用空环境、独立 HOME、tmp 和缓存运行受信配方。候选代码及构建脚本只在此执行。
3. receipt：第三个作业下载 prepare 的不可变 artifact ID，使用平台 needs.candidate.result 形成受信回执。不会读取候选自行生成的“成功”文件。
4. publish：业务仓默认分支上的受保护元数据作业核对私有回执，发布绑定 head/base/merge/控制版本的状态。协调器再次验证受信回执及由默认分支 publisher 在状态 POST 成功后签发的私有发布记录；记录绑定平台返回的 status ID、发布 run、准确 head/base/merge 与控制版本。

artifact 只保留一天；状态与回执永久保存在私有独立 automation-state 分支。控制分支不由状态写入推进。回执写入冲突明确失败；周期对账重新请求检查，不能当作通过。

现有 token 权限范围保留：GH_SYNC_TOKEN、FORGEJO_SYNC_TOKEN 用于受信作业，Ateliers 用于独立 core-publication 环境。候选作业不接收这些 token。迁移必须移除候选可触及的旧仓库级 secret 存放，并设置 environment 默认分支限制；仅新增 environment 同名 secret、却保留旧仓库级副本，不算完成隔离。

## 保留检查语义

discipline 的真实 findings 返回 1 只作告警；导入失败、异常、命令无法执行和其他非零结果阻断。组装 self_test 与明确登记的单测阻断，未配置阻断检查亦失败。core 本身使用其 74 项标准库单测；studio 保留组装检查及两项不依赖私有样本的 S06 单测。生产样本路径绑定的测试不伪装为托管 CI 已运行。

各域当前仓库没有独立 tests 套件时，登记编译和规范检查，不自动创造“通过”的单测统计。对域更新的完整组装门禁在准确子模块组合的父仓 PR 上执行。

## 父仓与外部子模块

子仓 GitHub 主线只有确认准确 SHA 已回流 Forgejo 后，才能进入父仓指针 PR。基于父仓最新 main 和一组明确的子仓 SHA 创建 gitlink 更新；自有分支只快进，基线变化通过保留前序提交和新 base 的 merge commit 继续历史，不 rebase/force-push。关闭未合并时保持暂停。

只解析 .gitmodules 中登记的仓库，不猜测域路径。唯一明确外部依赖 bgutil-ytdlp-pot-provider 按父仓 gitlink 导出准确快照，不纳入跨平台镜像或自动升级；新增外部 URL 必须审查控制登记。未知子模块、对象缺失、部分对账丢失已选子仓更新均停止。

## 发布

版本准备继续由人工审核的版本 PR 完成，本次没有运行 cz bump，也未改业务仓 pyproject.toml/uv.lock。正式 vX.Y.Z tag 建在已批准 GitHub 默认分支历史上，先完成 tag 对象回流，再人工触发私有控制仓 release 工作流。

准备、构建、发布三个作业分离。构建无发布凭据；发布不安装或执行候选包，只用受信 Twine 客户端解析、上传 wheel/sdist。上传前后核对包名、版本、准确 tag 提交、文件清单及 SHA256。已有同名版本不同文件不覆盖、不删除；部分成功可按相同摘要续传；成功后写入私有发布回执。

继续使用同一个 Forgejo PyPI backend；托管 GitHub runner 通过公网 HTTPS 访问 /api/packages/Carnation/pypi，不依赖 debian.lan 或凭据 URL。该发包路径的 WAF 和写入需真实隔离包验收，尚未实发。

官方格式依据：[Forgejo PyPI registry](https://forgejo.org/docs/v15.0/user/packages/pypi/)。实例只读文件接口已确认返回 SHA256；没有发布任何新包。

## 状态、重试与迁移

本地测试使用 MemoryStore/FileStore；真实写入必须使用独立私有状态分支的文件 SHA compare-and-swap。先保存分支所有权与待发布 SHA，后推送，失败重读引用判断是否实际成功。相同状态不重复创建状态提交；并发版本变化不隐藏。

同一个 GitHub Actions concurrency group 串行协调全部仓库的引用写入；本地目录还有 fcntl 文件锁。CI 只写各自回执/状态，不推进主线。普通 Git push 的服务器快进检查是最终保障，绝无远端强推。

旧 PR 的采用只能由 migration.py 按明确 selections 生成本地待审状态，不能推测源平台。非主线目标 PR、未知源分支、历史分叉保留现场。部署 patch 不等于状态已导入，也不等于配置已就绪。

## 保持原 token scope 的 API 选择

默认登记 protection_mode=ruleset。读取适用于当前 branch 的 active rules 及对应 ruleset 只需要 Metadata: read；由现有管理员在界面配置 active 的 PR、严格受信状态、禁止强推和删除，merge-only，且 bypass_actors 为空。bypass 列表被权限隐藏时保持暂停，不假设管理员受约束。

CI 回执按 repository ID / PR / head / base 建立组合索引；迟到的旧 run 不覆盖更新 run。默认分支元数据作业用自身 GITHUB_TOKEN 发布状态，再用现有 GH_SYNC_TOKEN 的 Contents 写权限补私有发布凭证。协调器通过 Contents 读这些记录，不依赖额外 Actions-read、Commit-statuses-read 或 Administration-read scope。失败/缺失的发布凭证不能作为 required check 已完成。

legacy 兼容模式仍支持传统保护和平台 Actions provenance 读取，只有已有身份具备所需读取能力时使用；不会遇到 403 后自动改成较弱门禁。实际新登记均选 ruleset。

官方权限依据：[适用分支 Rules 与 Ruleset 读取](https://docs.github.com/en/rest/repos/rules)、[传统保护读取](https://docs.github.com/en/rest/branches/branch-protection)。Ruleset 的 bypass_actors 是否可见还取决于请求身份实际写规则的能力，必须隔离验收，不能只凭 token 配置判断。

