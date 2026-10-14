> 当前 studio 初步部署：仅启用 studio 的 CI 登记；自动合并及协调器写入保持关闭。控制代码与 registry 均保留在私有 atelier-sync，公开结果发布工作流改为检出固定私有控制版本。私有控制仓复用现有仓库级 Secret，不依赖当前套餐不可用的环境保护。Docker/App 已删除，隔离闭环已通过，见 LOOP_ACCEPTANCE.md。

> v1.2 执行与准入范围更新见 [VARIANT_GATE.md](VARIANT_GATE.md)。本文件原“所有私有仓均须平台保护/只在私有控制仓测试”的要求已由用户确认的新范围取代；独立 App 授权与新隔离执行仍未部署。

# 本地交付与切换操作清单

日期：2026-10-14。当前只完成本地代码、测试、备份和待审补丁；线上旧流程仍在运行，未提交、推送、修改 secret/保护规则、发布包或触碰 gcp。

## 交付位置

- 控制仓：/home/Architecture/atelier-sync，基于原默认分支 9686ec8bfea42107175d745aeefaa1ac5b1cbe73。
- 业务仓待审补丁：/home/Architecture/atelier-studio/tmp/automation-rebuild/rollout。manifest.json 记录 16 个补丁的基线与 SHA256；其中独立 checkout 不与日常开发目录共享工作树。
- 对象备份：/home/Architecture/atelier-studio/tmp/automation-rebuild/backups/{github,forgejo}/<name>.git；GitHub 16 个、Forgejo 17 个。不要移动/删除，staging checkout 使用 shared clone 引用这些对象。
- 盘点：docs/inventory.json（本次快照），tmp/inventory.json（原始已补充盘点）。正式切换前必须重新盘点，不能把旧快照当当前配置。
- 原方案与实施补充：plan-v1.1.md、architecture.md、VERIFICATION.md。

## 已确认的现状与待处理事项

| 项目 | 本次发现 | 切换前处理 |
|---|---|---|
| 主线关系 | 已存在的 16 对中，15 对相同，atelier-pipeline-video 分叉 | 保留双方对象；审核现有 video bisync PR，或另建明确源分支 PR，把独有历史纳入 GitHub 后再普通快进 Forgejo；不强推覆盖 |
| 缺失对端 | publish-engine 的 GitHub 读取得到 404 | 核对仓库是否缺失/未授权；未确认前保持禁用，不自动建仓 |
| 可见性 | Forgejo 全部私有；三个父仓 GitHub 公开 | 显式批准允许导入的公开范围，才修改 allow_public_import；私有域源码仍只在私有控制仓测试 |
| GitHub 配置 | 已读取仓库的 allow_auto_merge 为 false；保护接口当前访问被 403 拒绝 | 用已有有权账号人工核对/配置，或提供可读的现有身份；不擅自扩展 token 权限，不把 403 当“无保护” |
| Forgejo 保护 | 本次快照中的 branch_protections 均为空 | 建立镜像默认分支保护，仅镜像身份允许普通快进；禁强推、删除与人直接提交 |
| 现有任务 | 多个自动化 PR 未合并，包括 studio #3 子模块监测、#4 bisync | 保留 PR；#3 的纪律阻断变更与本方案相冲突，应人工取舍；#4 目标是开发分支，不能默认当主线任务采用 |
| 发包 | 原 Forgejo publish 使用 Ateliers 和 LAN URL，同一作业构建/发布 | 保留 token 权限与包 backend；迁移到私有控制仓独立发包环境，验证 HTTPS 包上传 WAF |
| 前端部署 | 存在 Forgejo deploy 入口 | patch 将旧自动入口退役；部署仍由明确用户指令单独执行，不随镜像/发布自动部署 |
| 规范检查 | 当前准确 studio gitlink 组合发现 publishers OCR 的一条已有 M7 finding | 保持告警；不在自动化重构中顺带修业务代码 |

旧 bisync 实现是快进/分叉 PR，不会自动生成文本冲突标记。真正需移除的是双向默认分支推进与自有 bisync 分支的强制更新路径，不能把“重复冲突”推测当已查明根因。

## 待审平台配置

下面是配置清单，尚未执行。权限 scope 保持原有；账号套餐是否支持这些保护与 environment 分支限制必须实际核实。不支持或现有 token 无法读取门禁时，该仓保持暂停，不能降级放行。

### 私有 atelier-sync

1. 保护实际默认分支，仅 PR merge commit，人工审查控制代码变化；禁止强推/删除。
2. 建立 automation-state 独立分支，并保护其写入身份；先导入经过审阅的状态文件，不能在首次运行时隐式创建。
3. coordination-control environment 只允许默认分支：
   - secrets：现有 GH_SYNC_TOKEN、FORGEJO_SYNC_TOKEN（同 token，同 scope）。
   - variables：COORDINATOR_TRUSTED_SHA = 实际合并后的控制版本完整 SHA；COORDINATOR_ENABLED 初始 false。
4. core-publication environment 同样限制默认分支：
   - secrets：GH_SYNC_TOKEN、FORGEJO_SYNC_TOKEN、现有 Ateliers。
   - variables：同一个 COORDINATOR_TRUSTED_SHA；RELEASE_ENABLED 初始 false。
   - 发布审核独立于同步启用；本次没有请求发版。
5. 移除其他位置候选作业可读取的这些高权限 secret 副本；先确认迁移成功，再移除旧存放。不能把完整 token 值写入配置文档或日志。

### 16 个业务 GitHub 仓

- 默认采用 repository Ruleset：active，匹配默认分支，bypass_actors=[]；同时要求 PR、atelier/trusted-ci 的 strict up-to-date、non_fast_forward 与 deletion 限制，allowed_merge_methods=[merge]。禁止以 evaluate 规则或隐藏 bypass 列表代替真实保护。传统保护兼容模式仍要求 enforce_admins，但默认方案不依赖 Administration 读取 scope。
- 仅允许 merge commit；enable auto-merge；审批人数按现有协作策略明确设定（单人仓不可无条件要求第二个人而造成永久死锁）。
- coordination-metadata environment 只允许默认分支，存放现有 GH_SYNC_TOKEN 和受信控制 SHA。
- 只安装元数据 publisher，不在业务仓高权限作业检出或执行候选代码。
- 保留历史 PR、分支、tag；不批量关闭或删除。
- workflow/security/依赖锁/.gitmodules 变更即使检查通过，也保持人工合并。

### 仓库登记与旧任务采用

默认新分支可采用 work/forgejo/、work/github/ 等互斥前缀，需先写入登记文件；目前前缀为空，不能自动推测现有分支来源。

旧任务由人工填写 selections.json，例如：

    [{
      "repository": "atelier-web",
      "source_ref": "refs/heads/开发源分支",
      "mapped_head": "bisync/forgejo/开发源分支",
      "pr": 1,
      "ownership_confirmed": true
    }]

然后只生成本地待审状态：

    /home/Architecture/.venv/bin/python migration.py \
      --inventory docs/inventory.json --selections tmp/selections.json \
      --output tmp/adopted-state

活动 PR 的源、映射 head SHA 必须一致；已合并任务可导入 completed 记录，从而识别历史 squash/rebase。不存在的源分支、非默认 base 或来源未知任务不强行采用。重新创建同名分支、历史重写后的恢复另立代次，不擦除关闭记录。

## 安装顺序与验收

1. 用户批准具体提交/PR 后，提交控制仓改动及 16 个 patch；先部署禁用配置。不要把本地 staged checkout 直接当生产代码。
2. 在独立 GitHub/Forgejo 测试仓测试平台保护、Actions、artifact、credential 边界与 WAF。完整 T01–T42 见 VERIFICATION.md；没有证据的项目仍是未验证。
3. 停止旧 bisync 写入、Forgejo push mirror 与 bump-variants，等待在途作业；重新建立双方对象与引用备份。
4. 审核 video 分叉与旧 PR，配置保护、environment、状态导入、明确源所有权。
5. 原始 patch 使旧通知/指针/发包/部署作业仅保留无凭据的手动退役说明；正式安装前确认发布接替流程准备完毕，避免旧自动发包停掉却无可用接替。
6. 全部禁用时用 audit.py 只读核对登记、active Ruleset 和配置；需要预览具体对账时先在临时登记中开启该仓 enabled，COORDINATOR_ENABLED 仍为 false。随后逐仓确认正式 enabled、protection_ready，并记录实际测试证据。公开导入的 allow_public_import 必须单独确认。
7. COORDINATOR_ENABLED=true 后仅启用源分支/PR/主线镜像；各 repo.auto_merge 初始 false，验证人工 PR 闭环后再逐仓开启。
8. 验证两个开发方向各一个闭环、main 相同 SHA、子仓回流后父仓 PR、base 变化重查，再按独立指令启用发版。
9. gcp/生产与本次改造无自动连接；生产部署等用户独立指令。

注意：GitHub 分支保护会检查 head 的 required status。本实现额外绑定最新 base 和合并候选；仅看到绿色状态或工作流结束并不足以自动合并。

## 回滚

立即将 COORDINATOR_ENABLED 与 RELEASE_ENABLED 设为 false，暂停新写入；保留既有合并、状态分支、发布回执与全部备份。回到人工 PR + 人工普通快进镜像。禁止直接重启旧双向同步。

恢复文件可从备份准确提交读取；恢复引用先重新盘点。默认不 force-push、不删除 tag、不降版本。任何非快进恢复或删除必须另行明确确认。旧公开/私有源范围也不可在回滚时静默改变。

## 重新生成

stage.py 只接受 tmp 内新的目的目录；不会提交或写远端：

    /home/Architecture/.venv/bin/python stage.py \
      --backups /home/Architecture/atelier-studio/tmp/automation-rebuild/backups \
      --destination /home/Architecture/atelier-studio/tmp/automation-rebuild/rollout-next

audit.py 使用现有 Git/gh/netrc 身份，只读访问 API 和公网 HTTPS，并逐侧尝试完整对象备份。API 403、refs、PR 或保护读取失败会分别标记；完整 API 盘点可用已连接的 GitHub connector 补充，但保护仍需真实有权身份核对。--backup 必须是新的备份目录，不能覆盖检查点。

