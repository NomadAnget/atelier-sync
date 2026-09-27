"""Forgejo ⇄ GitHub 双向 git 同步(只快进、只增不删、分叉即报错)。

规则(每个仓库、每个 ref 独立判断):
  分支  两边相同                → 跳过
        只在一边有              → 复制到另一边
        一边是另一边的祖先       → 把落后的一边快进到新的一边
        两边分叉                → 两边分支都不动;把 Forgejo 版推到 GitHub 的 bisync/forgejo/<分支>,
                                  并在 GitHub 开 PR 合入 <分支>。PR 合并后下次同步即快进回 Forgejo
  tag   只在一边有              → 复制到另一边
        两边同名但指向不同       → 不动,记为冲突
  删除  一律不跟随(某侧删掉的分支会从另一侧补回来)

推送全部是普通 push(不 --force):即便判断与推送之间有人抢先推送,服务器也只会拒绝,不会覆盖。
唯一例外是本工具自有的 GitHub 分支 bisync/*(PR 源分支),它不参与同步,按需强制更新。

用法:
  python3 bisync.py --dry-run          # 只打印计划
  python3 bisync.py                    # 执行
  python3 bisync.py --only atelier-core

环境变量:FORGEJO_SYNC_TOKEN、GH_SYNC_TOKEN(缺一即失败)。
只依赖标准库 + git 命令行。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib import error, request

HERE = Path(__file__).resolve().parent
PR_PREFIX = "bisync/forgejo/"          # GitHub 上的 PR 源分支前缀(不参与同步)
OWN_REFS = "refs/heads/bisync/"


# ---------- git 调用 ----------

def git_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def cred_args(fj_host: str, fj_user: str) -> list[str]:
    """按主机注入凭据:token 只经环境变量交给 credential helper,不进 URL、不进日志。"""
    fj = f'!f() {{ echo username={fj_user}; echo "password=$FORGEJO_SYNC_TOKEN"; }}; f'
    gh = '!f() { echo username=x-access-token; echo "password=$GH_SYNC_TOKEN"; }; f'
    return [
        "-c", "credential.helper=",
        "-c", f"credential.https://{fj_host}.helper={fj}",
        "-c", f"credential.https://github.com.helper={gh}",
    ]


class Git:
    def __init__(self, cred: list[str]):
        self.cred = cred

    def run(self, *args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
        r = subprocess.run(["git", *self.cred, *args], cwd=cwd, env=git_env(),
                           capture_output=True, text=True)
        if check and r.returncode != 0:
            raise GitError(" ".join(args[:2]), r.stderr.strip())
        return r


class GitError(Exception):
    def __init__(self, what: str, stderr: str):
        super().__init__(f"{what}: {stderr.splitlines()[-1] if stderr else '(无输出)'}")


def ls_remote(g: Git, url: str) -> dict[str, str]:
    """返回 {refs/heads/x | refs/tags/x: sha}。仓库不存在/无权限抛 GitError。"""
    out = g.run("ls-remote", "--heads", "--tags", url).stdout
    refs = {}
    for line in out.splitlines():
        sha, ref = line.split("\t", 1)
        if ref.endswith("^{}"):
            continue  # 附注 tag 的剥皮行,比较用 tag 对象本身
        refs[ref] = sha
    return {r: s for r, s in refs.items() if not r.startswith(OWN_REFS)}


# ---------- 同步计划 ----------

@dataclass
class Plan:
    to_github: dict[str, str] = field(default_factory=dict)   # ref -> sha
    to_forgejo: dict[str, str] = field(default_factory=dict)
    diverged: dict[str, tuple[str, str]] = field(default_factory=dict)   # ref -> (forgejo, github)
    conflicts: list[str] = field(default_factory=list)                   # tag 冲突(PR 解决不了)


def make_plan(g: Git, work: Path, fj: dict[str, str], gh: dict[str, str]) -> Plan:
    p = Plan()
    for ref in sorted(set(fj) | set(gh)):
        a, b = fj.get(ref), gh.get(ref)
        if a == b:
            continue
        if a is None:
            p.to_forgejo[ref] = b
        elif b is None:
            p.to_github[ref] = a
        elif ref.startswith("refs/tags/"):
            p.conflicts.append(f"{ref}: Forgejo={a[:8]} GitHub={b[:8]}(tag 同名不同指向)")
        elif is_ancestor(g, work, a, b):
            p.to_forgejo[ref] = b
        elif is_ancestor(g, work, b, a):
            p.to_github[ref] = a
        else:
            p.diverged[ref] = (a, b)
    return p


def is_ancestor(g: Git, work: Path, a: str, b: str) -> bool:
    return g.run("merge-base", "--is-ancestor", a, b, cwd=work, check=False).returncode == 0


def fetch_both(g: Git, work: Path, fj_url: str, gh_url: str) -> None:
    """把两边的分支/tag 抓进本地裸仓(各自命名空间),供祖先判断与推送。缓存复用,只增量抓取。"""
    if not (work / "HEAD").exists():
        work.mkdir(parents=True, exist_ok=True)
        g.run("init", "--bare", "-q", cwd=work)
    for name, url in (("fj", fj_url), ("gh", gh_url)):
        g.run("fetch", "-q", "--no-tags", "--prune", url,
              f"+refs/heads/*:refs/{name}/heads/*", f"+refs/tags/*:refs/{name}/tags/*", cwd=work)


def push(g: Git, work: Path, url: str, updates: dict[str, str]) -> list[str]:
    """逐 ref 普通推送(非 force、非 atomic),返回失败列表。"""
    failed = []
    for ref, sha in updates.items():
        r = g.run("push", "-q", "--no-verify", url, f"{sha}:{ref}", cwd=work, check=False)
        if r.returncode != 0:
            last = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "(无输出)"
            failed.append(f"{ref}: {last}")
    return failed


# ---------- 分叉 → GitHub PR ----------

def gh_api(method: str, path: str, body: dict | None = None):
    req = request.Request(f"https://api.github.com{path}", method=method,
                          data=json.dumps(body).encode() if body is not None else None,
                          headers={"Authorization": f"Bearer {os.environ['GH_SYNC_TOKEN']}",
                                   "Accept": "application/vnd.github+json",
                                   "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except error.HTTPError as e:
        raise GitError(f"GitHub API {method} {path}", f"HTTP {e.code} {e.read().decode(errors='replace')[:200]}")


def ensure_pr(owner: str, repo: str, head: str, base: str, fj_sha: str, gh_sha: str) -> str:
    """已有同 head→base 的 open PR 就复用(源分支已更新,PR 自动刷新),否则新建。返回 PR 链接。"""
    existing = gh_api("GET", f"/repos/{owner}/{repo}/pulls?state=open&head={owner}:{head}&base={base}")
    if existing:
        return existing[0]["html_url"]
    body = (f"Forgejo 与 GitHub 上的 `{base}` 分叉了,bisync 无法快进。\n\n"
            f"- Forgejo `{base}` = `{fj_sha[:8]}`(即本 PR 的源分支 `{head}`)\n"
            f"- GitHub `{base}` = `{gh_sha[:8]}`\n\n"
            f"合并本 PR(有冲突就在这里解决)后,下一次同步会把合并结果快进回 Forgejo。\n"
            f"合并前 Forgejo 上 `{base}` 若有新提交,源分支会被自动更新。")
    pr = gh_api("POST", f"/repos/{owner}/{repo}/pulls",
                {"title": f"bisync: 合并 Forgejo 上 {base} 的分叉提交", "head": head, "base": base, "body": body})
    return pr["html_url"]


def open_prs(g: Git, work: Path, gh_url: str, owner: str, repo: str,
             diverged: dict[str, tuple[str, str]]) -> tuple[list[str], list[str]]:
    ok, failed = [], []
    for ref, (a, b) in diverged.items():
        base = ref.removeprefix("refs/heads/")
        head = PR_PREFIX + base
        r = g.run("push", "-q", "--no-verify", gh_url, f"+{a}:refs/heads/{head}", cwd=work, check=False)
        if r.returncode != 0:
            failed.append(f"{ref}: 推送 PR 源分支失败:{r.stderr.strip().splitlines()[-1:]}")
            continue
        try:
            ok.append(f"🔀 {ref} 分叉 → PR {ensure_pr(owner, repo, head, base, a, b)}")
        except GitError as e:
            failed.append(f"{ref}: 开 PR 失败:{e}")
    return ok, failed


# ---------- 主流程 ----------

@dataclass
class Result:
    name: str
    status: str                     # in-sync / synced / planned / pr / skipped / error / conflict
    lines: list[str] = field(default_factory=list)


def repo_urls(cfg: dict, name: str) -> tuple[str, str]:
    return (f"https://{cfg['forgejo']['host']}/{cfg['forgejo']['owner']}/{name}.git",
            f"https://github.com/{cfg['github']['owner']}/{name}.git")


def sync_repo(g: Git, cfg: dict, name: str, work_root: Path, dry: bool) -> Result:
    fj_url, gh_url = repo_urls(cfg, name)
    try:
        fj = ls_remote(g, fj_url)
    except GitError as e:
        return Result(name, "skipped", [f"Forgejo 侧不可访问:{e}"])
    try:
        gh = ls_remote(g, gh_url)
    except GitError as e:
        return Result(name, "skipped", [f"GitHub 侧不可访问(未建仓或 token 未授权):{e}"])
    if fj == gh:
        return Result(name, "in-sync")

    work = work_root / f"{name}.git"
    try:
        fetch_both(g, work, fj_url, gh_url)
    except GitError as e:
        return Result(name, "error", [f"抓取失败:{e}"])
    p = make_plan(g, work, fj, gh)

    lines = [f"→ GitHub  {ref} = {sha[:8]}" for ref, sha in p.to_github.items()]
    lines += [f"→ Forgejo {ref} = {sha[:8]}" for ref, sha in p.to_forgejo.items()]
    lines += [f"⚠ 冲突 {c}" for c in p.conflicts]
    if dry:
        lines += [f"🔀 {ref} 分叉 Forgejo={a[:8]} GitHub={b[:8]} → 将开 PR" for ref, (a, b) in p.diverged.items()]
        return Result(name, "conflict" if p.conflicts else "pr" if p.diverged else "planned", lines)

    failed = push(g, work, gh_url, p.to_github) + push(g, work, fj_url, p.to_forgejo)
    prs, pr_failed = open_prs(g, work, gh_url, cfg["github"]["owner"], name, p.diverged)
    lines += prs
    lines += [f"✗ 推送失败 {f}" for f in failed + pr_failed]
    if failed or pr_failed:
        return Result(name, "error", lines)
    return Result(name, "conflict" if p.conflicts else "pr" if p.diverged else "synced", lines)


ICON = {"in-sync": "✅", "synced": "🔄", "planned": "📝", "pr": "🔀", "skipped": "⏭️", "error": "❌", "conflict": "⚠️"}


def report(results: list[Result], dry: bool) -> str:
    md = [f"## 双向同步{'(dry-run,未推送)' if dry else ''}", "", "| 仓库 | 状态 | 明细 |", "|---|---|---|"]
    for r in results:
        detail = "<br>".join(x.replace("|", "\\|") for x in r.lines) or "—"
        md.append(f"| `{r.name}` | {ICON[r.status]} {r.status} | {detail} |")
    return "\n".join(md) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="只打印计划,不推送")
    ap.add_argument("--only", nargs="*", help="只同步这些仓库")
    ap.add_argument("--config", default=str(HERE / "repos.toml"))
    ap.add_argument("--work", default=str(HERE / "work"), help="本地裸仓缓存目录")
    a = ap.parse_args()

    missing = [k for k in ("FORGEJO_SYNC_TOKEN", "GH_SYNC_TOKEN") if not os.environ.get(k)]
    if missing:
        print(f"缺少环境变量:{', '.join(missing)}", file=sys.stderr)
        return 2

    cfg = tomllib.loads(Path(a.config).read_text(encoding="utf-8"))
    names = a.only or cfg["repos"]
    unknown = [n for n in names if n not in cfg["repos"]]
    if unknown:
        print(f"不在 repos.toml 里:{unknown}", file=sys.stderr)
        return 2

    g = Git(cred_args(cfg["forgejo"]["host"], cfg["forgejo"]["user"]))
    results = []
    for n in names:
        r = sync_repo(g, cfg, n, Path(a.work), a.dry_run)
        print(f"{ICON[r.status]} {n}: {r.status}")
        for line in r.lines:
            print(f"    {line}")
        results.append(r)

    md = report(results, a.dry_run)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as f:
            f.write(md)
    bad = [r.name for r in results if r.status in ("error", "conflict")]
    if bad:
        print(f"\n需人工处理:{bad}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
