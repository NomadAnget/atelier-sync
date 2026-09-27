# atelier-sync

Forgejo(`forgejo.hurcaguari.top/Carnation`)⇄ GitHub(`github.com/NomadAnget`)**双向 git 同步**。
取代 Forgejo 的单向推送镜像(`git push --mirror` 会覆盖/删除 GitHub 侧的改动)。

## 规则

| 情况 | 处理 |
|---|---|
| 分支只在一边 | 复制到另一边 |
| 一边领先(另一边是其祖先) | 快进落后的一边 |
| 两边分叉 | 两边都不动;Forgejo 版推到 GitHub `bisync/forgejo/<分支>` 并**开 PR** 合入 `<分支>`。合并 PR 后下次同步快进回 Forgejo |
| tag 只在一边 / 同名不同指向 | 复制 / 标红 |
| 删除 | 不跟随 |

所有推送都是普通 push(不 `--force`),最坏情况是被拒,不会覆盖任何提交。
唯一例外是工具自有的 GitHub 分支 `bisync/*`(PR 源分支),不参与同步,按需强制更新。

## 组成

- `bisync.py` —— 同步脚本(标准库 + git);`repos.toml` —— 仓库清单
- `.github/workflows/bisync.yml` —— 运行入口:通知触发 / 手动 / 每小时兜底
- `templates/forgejo-notify.yml` —— 放进各仓 `.forgejo/workflows/`,Forgejo 推送后即时触发
- `templates/github-notify.yml` —— 放进各仓 `.github/workflows/`,GitHub 推送后即时触发(需该仓配 secret)

## 配置

| 位置 | 名称 | 内容 |
|---|---|---|
| 本仓 Actions secret | `FORGEJO_SYNC_TOKEN` | Forgejo token(repository 读写) |
| 本仓 Actions secret | `GH_SYNC_TOKEN` | GitHub fine-grained token(Contents + Workflows + Pull requests 读写,含所有同步仓与本仓) |
| 本仓 Actions variable | `BISYNC_ENABLED` | `true` 才真正推送;否则自动运行只做 dry-run |
| Forgejo 用户 secret | `GH_SYNC_TOKEN` | 同上 GitHub token(通知用) |

WAF:Forgejo 站点 URL 规则"目录过滤"改为 `(^|/)\.(htaccess|…|git|…)`,放行 `<repo>.git/…` 的 git 智能 HTTP。

## 本地

```bash
python3 -m unittest discover -s tests       # 本地裸仓模拟两端的测试
FORGEJO_SYNC_TOKEN=… GH_SYNC_TOKEN=… python3 bisync.py --dry-run
```
