# Atelier 中央检查与合并协调

`atelier-sync` 接收 Atelier 子仓库发送的 PR 通知，在受影响的变体中检查待合并版本。检查通过后，它会合并子仓库 PR，并同步变体仓库的子模块指针。

## 子仓库 PR 流程

收到 PR 通知后，工作流会核验来源仓库、PR 状态、目标分支和通知中的提交 SHA。PR 必须来自 Atelier 名下的同一仓库、不是草稿，并以该仓库的 `master` 为目标；已合并的 PR 也可用于安全重试。工作流使用 GitHub 为 PR 生成的合并候选版本进行检查。

检查时，工作流将待合并的子仓库版本装入每个受影响的变体，再运行：

```sh
python -I -m atelier_core.discipline <variant>/src
python -m src.main --check
```

任一检查失败，子仓库 PR 不会被自动合并，变体指针也不会更新。检查结果会写入中央 Actions 摘要；通知带有有效的来源仓库和 PR 编号时，也会评论回该 PR。

全部检查通过后，工作流合并子仓库 PR，为受影响的变体更新子模块指针，并合并生成的指针 PR。最后会核实变体 `master` 指向刚合并的子仓库提交。

## 触发方式

- `atelier-submodule-pr`（兼容旧事件名 `atelier_submodule_pr`）：核验并检查通知中的 PR；通过后自动合并并同步变体指针。
- `atelier_check`：只运行检查，不合并 PR，也不更新变体指针。
- `workflow_dispatch`：手动指定仓库和完整提交 SHA 运行检查，不执行合并。

默认分支是 `main`。子仓库通过 `repository_dispatch` 发送通知；GitHub 不会自动把其他仓库的 PR 事件转发到这里。工作流仅接受仓库所有者或 `github-actions[bot]` 发起的运行。

## Token 与运行环境

工作流使用 `GH_SYNC_TOKEN` Actions secret 读取参与检查的仓库，并执行 PR 评论、子仓库 PR 合并和变体指针更新。该 Token 需要具备参与仓库所需的访问权限。

检查运行在 GitHub-hosted runner 上，面向可信的 Atelier 仓库变更，不是运行任意外部代码的沙箱。工作流不会上传源码 checkout 制品；Actions 日志和摘要会包含检查输出与状态。
