"""用两个本地裸仓模拟 Forgejo / GitHub,覆盖:快进两个方向、单边新分支、tag 复制、分叉、tag 冲突、dry-run、不删除。"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bisync  # noqa: E402


def sh(*a, cwd=None):
    return subprocess.run(a, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class BisyncTest(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parent.parent / "tmp"
        root.mkdir(exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=root))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fj = self.tmp / "fj.git"
        self.gh = self.tmp / "gh.git"
        sh("git", "init", "--bare", "-q", str(self.fj))
        sh("git", "init", "--bare", "-q", str(self.gh))
        self.wc = self.tmp / "wc"
        sh("git", "init", "-q", "-b", "master", str(self.wc))
        sh("git", "config", "user.email", "t@t", cwd=self.wc)
        sh("git", "config", "user.name", "t", cwd=self.wc)
        self.commit("base")
        for r in (self.fj, self.gh):
            sh("git", "push", "-q", str(r), "master", cwd=self.wc)
        bisync.repo_urls = lambda cfg, name: (str(self.fj), str(self.gh))
        self.g = bisync.Git([])
        self.cfg = {"github": {"owner": "o"}}

    def commit(self, msg):
        (self.wc / "f").write_text(msg)
        sh("git", "add", "f", cwd=self.wc)
        sh("git", "commit", "-q", "-m", msg, cwd=self.wc)
        return sh("git", "rev-parse", "HEAD", cwd=self.wc)

    def refs(self, repo):
        return bisync.ls_remote(self.g, str(repo))

    def run_sync(self, dry=False):
        return bisync.sync_repo(self.g, self.cfg, "x", self.tmp / "work", dry)

    def test_in_sync(self):
        self.assertEqual(self.run_sync().status, "in-sync")

    def test_fast_forward_both_directions_and_new_refs(self):
        a = self.commit("fj ahead")
        sh("git", "push", "-q", str(self.fj), "master", cwd=self.wc)          # Forgejo master 领先
        sh("git", "push", "-q", str(self.gh), "HEAD:refs/heads/feat", cwd=self.wc)  # GitHub 独有分支
        sh("git", "tag", "-a", "v1", "-m", "v1", cwd=self.wc)
        sh("git", "push", "-q", str(self.gh), "v1", cwd=self.wc)              # GitHub 独有附注 tag
        r = self.run_sync()
        self.assertEqual(r.status, "synced", r.lines)
        self.assertEqual(self.refs(self.fj), self.refs(self.gh))
        self.assertEqual(self.refs(self.gh)["refs/heads/master"], a)
        # 再跑一次应已一致
        self.assertEqual(self.run_sync().status, "in-sync")

    def test_github_ahead(self):
        b = self.commit("gh ahead")
        sh("git", "push", "-q", str(self.gh), "master", cwd=self.wc)
        self.assertEqual(self.run_sync().status, "synced")
        self.assertEqual(self.refs(self.fj)["refs/heads/master"], b)

    def test_diverged_opens_pr_then_fast_forwards_after_merge(self):
        base = sh("git", "rev-parse", "HEAD", cwd=self.wc)
        a = self.commit("fj side")
        sh("git", "push", "-q", str(self.fj), "master", cwd=self.wc)
        sh("git", "reset", "-q", "--hard", base, cwd=self.wc)
        b = self.commit("gh side")
        sh("git", "push", "-q", str(self.gh), "master", cwd=self.wc)
        calls = []
        bisync.ensure_pr = lambda owner, repo, head, base, fs, gs: calls.append((head, base, fs, gs)) or "PR-URL"
        r = self.run_sync()
        self.assertEqual(r.status, "pr", r.lines)
        # 两边 master 都不动
        self.assertEqual(self.refs(self.fj)["refs/heads/master"], a)
        self.assertEqual(self.refs(self.gh)["refs/heads/master"], b)
        # Forgejo 版推到 GitHub 的 PR 源分支,且开了 PR
        gh_all = sh("git", "ls-remote", str(self.gh))
        self.assertIn(f"{a}\trefs/heads/bisync/forgejo/master", gh_all)
        self.assertEqual(calls, [("bisync/forgejo/master", "master", a, b)])
        # bisync/* 不会被复制到 Forgejo
        self.assertNotIn("bisync/", sh("git", "ls-remote", str(self.fj)))
        # 在 GitHub 合并 PR 后,下次同步快进回 Forgejo
        sh("git", "merge", "-q", "--no-edit", "-X", "ours", a, cwd=self.wc)
        m = sh("git", "rev-parse", "HEAD", cwd=self.wc)
        sh("git", "push", "-q", str(self.gh), "master", cwd=self.wc)
        self.assertEqual(self.run_sync().status, "synced")
        self.assertEqual(self.refs(self.fj)["refs/heads/master"], m)

    def test_tag_conflict(self):
        sh("git", "tag", "v1", cwd=self.wc)
        sh("git", "push", "-q", str(self.fj), "v1", cwd=self.wc)
        self.commit("next")
        sh("git", "tag", "-f", "v1", cwd=self.wc)
        sh("git", "push", "-q", str(self.gh), "master", "v1", cwd=self.wc)
        r = self.run_sync()
        self.assertEqual(r.status, "conflict", r.lines)
        self.assertEqual(self.refs(self.fj)["refs/heads/master"], self.refs(self.gh)["refs/heads/master"])

    def test_dry_run_pushes_nothing(self):
        self.commit("ahead")
        sh("git", "push", "-q", str(self.fj), "master", cwd=self.wc)
        before = self.refs(self.gh)
        self.assertEqual(self.run_sync(dry=True).status, "planned")
        self.assertEqual(self.refs(self.gh), before)

    def test_empty_github_gets_default_branch_first(self):
        # Forgejo 有 master + 字母序更靠前的 feat/a;GitHub 是空仓
        sh("git", "push", "-q", str(self.fj), "HEAD:refs/heads/feat/a", cwd=self.wc)
        shutil.rmtree(self.gh)
        sh("git", "init", "--bare", "-q", str(self.gh))
        pushed = []
        orig = bisync.push_one
        bisync.push_one = lambda g, w, url, spec, tries=3: pushed.append(spec) or orig(g, w, url, spec, tries)
        try:
            self.assertEqual(self.run_sync().status, "synced")
        finally:
            bisync.push_one = orig
        self.assertTrue(pushed[0].endswith(":refs/heads/master"), pushed)
        self.assertEqual(self.run_sync().status, "in-sync")

    def test_missing_side_skipped(self):
        bisync.repo_urls = lambda cfg, name: (str(self.fj), str(self.tmp / "nope.git"))
        self.assertEqual(self.run_sync().status, "skipped")


if __name__ == "__main__":
    unittest.main()
