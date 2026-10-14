"""Real local Git repositories exercise the complete reconciliation path, not only planner mocks."""
from __future__ import annotations
from copy import deepcopy
from dataclasses import replace
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from coordination.api import ApiError
from coordination.engine import Reconciler, validate_event
from coordination.gitops import Git, CoordinationError, redact
from coordination.registry import Repo, Registry, branch_name, original_branch
from coordination.state import MemoryStore
from coordination.submodules import pointer_commit

ROOT = Path(__file__).resolve().parents[1]

class FakeApi:
    def __init__(self, fixture, side):
        self.fixture, self.side = fixture, side
        self.pull_requests = []
        self.calls = []
    def request(self, method, path, body=None):
        self.calls.append((method, path, deepcopy(body)))
        repo = self.fixture.repo
        if path == f"/repos/{repo.github_name}" or path == f"/repos/{repo.forgejo_name}":
            return {"id": repo.github_id if self.side == "github" else repo.forgejo_id,
                    "full_name": repo.github_name if self.side == "github" else repo.forgejo_name,
                    "default_branch": "master", "private": True, "archived": False, "mirror": False}
        if method == "POST" and path.endswith("/dispatches"):
            return None
        if "/pulls/" in path:
            number = int(path.rsplit("/", 1)[1])
            pr = next(p for p in self.pull_requests if p["number"] == number)
            pr["head"]["sha"] = self.fixture.refs("github").get("refs/heads/" + pr["head"]["ref"], pr["head"]["sha"])
            return deepcopy(pr)
        raise AssertionError((method, path, body))
    def prs(self, repository):
        for pr in self.pull_requests:
            pr["head"]["sha"] = self.fixture.refs("github").get("refs/heads/" + pr["head"]["ref"], pr["head"]["sha"])
        return deepcopy(self.pull_requests)
    def create_pr(self, repository, head, base, title, body):
        self.calls.append(("CREATE_PR", head, base))
        pr = {"number": len(self.pull_requests) + 1, "state": "open", "draft": False,
              "mergeable": True, "auto_merge": None, "merged_at": None,
              "head": {"ref": head, "sha": self.fixture.refs("github")["refs/heads/" + head],
                       "repo": {"id": self.fixture.repo.github_id}},
              "base": {"ref": base}, "user": {"login": "author"}}
        self.pull_requests.append(pr)
        return deepcopy(pr)
    def auto_merge(self, *args):
        self.calls.append(("AUTO_MERGE", args))

class LocalGitTest(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=ROOT / "tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.git = Git(local_auth=True)
        self.urls = {s: self.root / (s + ".git") for s in ("github", "forgejo")}
        for path in self.urls.values():
            self.git.run("init", "--bare", "-q", "-b", "master", str(path))
        self.wc = self.root / "wc"
        self.git.run("init", "-q", "-b", "master", str(self.wc))
        self.git.run("config", "user.name", "test", cwd=self.wc)
        self.git.run("config", "user.email", "test@localhost", cwd=self.wc)
        self.base = self.commit("base")
        for url in self.urls.values():
            self.git.run("push", "-q", str(url), "master", cwd=self.wc)
        self.repo = Repo("test", 1, 2, "o/test", "f/test", enabled=True,
                         forgejo_prefixes=("dev/",), github_prefixes=("github/",), protection_ready=True)
        self.registry = Registry((self.repo,))
        fixture = self
        self.addCleanup(patch.stopall)
        patch.object(Repo, "url", lambda r, side, host=None: str(fixture.urls[side])).start()
        self.gh = FakeApi(self, "github")
        self.fj = FakeApi(self, "forgejo")
        self.store = MemoryStore()
        self.runner = Reconciler(self.registry, self.git, self.gh, self.fj, self.store,
                                 self.root / "objects", apply=True)

    def commit(self, text, name="f"):
        (self.wc / name).write_text(text)
        self.git.run("add", name, cwd=self.wc)
        self.git.run("commit", "-q", "-m", text, cwd=self.wc)
        return self.git.run("rev-parse", "HEAD", cwd=self.wc).stdout.strip()

    def push(self, side, sha, ref, force=False):
        self.git.run("push", "-q", str(self.urls[side]), ("+" if force else "") + sha + ":" + ref, cwd=self.wc)

    def refs(self, side):
        return self.git.refs(str(self.urls[side]))

    def feature(self, text="feature", ref="refs/heads/dev/a"):
        sha = self.commit(text)
        self.push("forgejo", sha, ref)
        return sha

    def run_sync(self, apply=True):
        self.runner.apply = apply
        return self.runner.reconcile(self.repo)

    def assert_ok(self, result):
        self.assertTrue(result.ok, result.errors)

    def test_equal_main_is_idempotent(self):
        self.assert_ok(self.run_sync())
        before = self.refs("github")
        self.assert_ok(self.run_sync())
        self.assertEqual(before, self.refs("github"))
        self.assertFalse(self.gh.pull_requests)

    def test_github_main_fast_forwards_only_forgejo(self):
        sha = self.commit("approved")
        self.push("github", sha, "refs/heads/master")
        self.assert_ok(self.run_sync())
        self.assertEqual(self.refs("forgejo")["refs/heads/master"], sha)

    def test_forgejo_ahead_never_pushes_github_main(self):
        sha = self.commit("unreviewed")
        self.push("forgejo", sha, "refs/heads/master")
        result = self.run_sync()
        self.assertIn("independent", " ".join(result.errors))
        self.assertEqual(self.refs("github")["refs/heads/master"], self.base)
        self.assertEqual(self.refs("forgejo")["refs/heads/master"], sha)

    def test_diverged_mains_preserve_both(self):
        fj = self.commit("forgejo side")
        self.push("forgejo", fj, "refs/heads/master")
        self.git.run("reset", "--hard", self.base, cwd=self.wc)
        gh = self.commit("github side")
        self.push("github", gh, "refs/heads/master")
        self.assertFalse(self.run_sync().ok)
        self.assertEqual(self.refs("github")["refs/heads/master"], gh)
        self.assertEqual(self.refs("forgejo")["refs/heads/master"], fj)

    def test_authority_rewind_is_rejected(self):
        sha = self.commit("approved")
        self.push("github", sha, "refs/heads/master")
        self.assert_ok(self.run_sync())
        self.push("github", self.base, "refs/heads/master", True)
        self.assertIn("rewound", " ".join(self.run_sync().errors))
        self.assertEqual(self.refs("forgejo")["refs/heads/master"], sha)

    def test_new_forgejo_feature_preserves_sha_and_creates_pr(self):
        sha = self.feature()
        self.assert_ok(self.run_sync())
        target = "refs/heads/" + branch_name(2, "refs/heads/dev/a")
        self.assertEqual(self.refs("github")[target], sha)
        self.assertEqual(self.refs("github")["refs/heads/master"], self.base)
        self.assertEqual(len(self.gh.pull_requests), 1)

    def test_three_commits_update_same_pr(self):
        for n in range(3):
            sha = self.feature("change" + str(n))
            self.assert_ok(self.run_sync())
        self.assertEqual(len(self.gh.pull_requests), 1)
        self.assertEqual(self.gh.prs("o/test")[0]["head"]["sha"], sha)

    def test_duplicate_event_does_not_duplicate_pr(self):
        self.feature()
        for _ in range(3):
            self.assert_ok(self.run_sync())
        self.assertEqual(len(self.gh.pull_requests), 1)

    def test_github_same_name_is_not_overwritten(self):
        sha = self.feature()
        self.git.run("reset", "--hard", self.base, cwd=self.wc)
        own = self.commit("native")
        self.push("github", own, "refs/heads/dev/a")
        self.assert_ok(self.run_sync())
        self.assertEqual(self.refs("github")["refs/heads/dev/a"], own)
        self.assertEqual(self.refs("github")["refs/heads/" + branch_name(2, "refs/heads/dev/a")], sha)

    def test_source_rewrite_does_not_force_update_mapped_ref(self):
        sha = self.feature("first")
        self.assert_ok(self.run_sync())
        self.git.run("reset", "--hard", self.base, cwd=self.wc)
        replacement = self.commit("rewrite")
        self.push("forgejo", replacement, "refs/heads/dev/a", True)
        self.assertFalse(self.run_sync().ok)
        self.assertEqual(self.refs("github")["refs/heads/" + branch_name(2, "refs/heads/dev/a")], sha)

    def test_mapped_ref_edit_is_rejected(self):
        self.feature()
        self.assert_ok(self.run_sync())
        extra = self.commit("github edit")
        self.push("github", extra, "refs/heads/" + branch_name(2, "refs/heads/dev/a"))
        self.assertIn("outside", " ".join(self.run_sync().errors))

    def test_delete_does_not_resurrect_source(self):
        self.feature()
        self.assert_ok(self.run_sync())
        self.git.run("push", "-q", str(self.urls["forgejo"]), ":refs/heads/dev/a", cwd=self.wc)
        self.assert_ok(self.run_sync())
        self.assertNotIn("refs/heads/dev/a", self.refs("forgejo"))
        state, _ = self.store.load(self.repo.key)
        self.assertEqual(state["tasks"]["refs/heads/dev/a"]["phase"], "source-deleted")
        self.assertEqual(len(self.gh.pull_requests), 1)

    def test_recreated_branch_has_new_generation(self):
        self.feature()
        self.assert_ok(self.run_sync())
        self.git.run("push", "-q", str(self.urls["forgejo"]), ":refs/heads/dev/a", cwd=self.wc)
        self.assert_ok(self.run_sync())
        newer = self.feature("new task")
        self.assert_ok(self.run_sync())
        self.assertIn("refs/heads/" + branch_name(2, "refs/heads/dev/a", 2), self.refs("github"))
        self.assertEqual(len(self.gh.pull_requests), 2)

    def test_closed_unmerged_pr_is_not_reopened(self):
        self.feature()
        self.assert_ok(self.run_sync())
        self.gh.pull_requests[0]["state"] = "closed"
        self.assert_ok(self.run_sync())
        self.feature("later")
        self.assert_ok(self.run_sync())
        self.assertEqual(len(self.gh.pull_requests), 1)

    def test_squash_merged_pr_old_work_is_consumed(self):
        sha = self.feature()
        self.assert_ok(self.run_sync())
        self.gh.pull_requests[0].update(state="closed", merged_at="now", merge_commit_sha=self.base)
        self.assert_ok(self.run_sync())
        self.assert_ok(self.run_sync())
        self.assertEqual(len(self.gh.pull_requests), 1)

    def test_merged_source_new_commit_starts_new_task(self):
        old = self.feature()
        self.assert_ok(self.run_sync())
        self.gh.pull_requests[0].update(state="closed", merged_at="now", merge_commit_sha=self.base)
        self.assert_ok(self.run_sync())
        newer = self.feature("new work")
        self.assert_ok(self.run_sync())
        self.assertEqual(len(self.gh.pull_requests), 2)
        self.assertEqual(self.gh.pull_requests[-1]["head"]["sha"], newer)

    def test_merge_and_source_advance_between_polls_keep_new_work(self):
        old = self.feature()
        self.assert_ok(self.run_sync())
        self.gh.pull_requests[0].update(state="closed", merged_at="now", merge_commit_sha=self.base)
        newer = self.feature("post-merge work before next poll")
        self.assert_ok(self.run_sync())
        self.assertEqual(len(self.gh.pull_requests), 2)
        self.assertEqual(self.refs("github")["refs/heads/" + branch_name(2, "refs/heads/dev/a")], old)
        self.assertEqual(self.gh.pull_requests[-1]["head"]["sha"], newer)

    def test_close_and_source_advance_between_polls_do_not_update_closed_head(self):
        old = self.feature()
        self.assert_ok(self.run_sync())
        self.gh.pull_requests[0].update(state="closed")
        self.feature("work after manual close")
        self.assert_ok(self.run_sync())
        self.assertEqual(self.refs("github")["refs/heads/" + branch_name(2, "refs/heads/dev/a")], old)
        self.assertEqual(len(self.gh.pull_requests), 1)

    def test_conflicting_pr_is_held_without_main_changes(self):
        self.feature()
        self.assert_ok(self.run_sync())
        self.gh.pull_requests[0]["mergeable"] = False
        result = self.run_sync()
        self.assert_ok(result)
        self.assertIn("conflict", " ".join(result.actions))
        self.assertEqual(self.refs("github")["refs/heads/master"], self.base)
        self.assertFalse(any(c[0] == "AUTO_MERGE" for c in self.gh.calls))

    def test_dry_run_changes_no_ref_or_ledger(self):
        self.feature()
        before = self.refs("github")
        self.assert_ok(self.run_sync(False))
        self.assertEqual(before, self.refs("github"))
        self.assertEqual(self.store.values, {})
        self.assertFalse(self.gh.pull_requests)

    def test_unowned_mapped_branch_is_not_adopted(self):
        sha = self.feature()
        self.push("github", sha, "refs/heads/" + branch_name(2, "refs/heads/dev/a"))
        self.assertIn("Unowned", " ".join(self.run_sync().errors))
        self.assertFalse(self.gh.pull_requests)

    def test_crash_after_push_recovers_without_duplicate_pr(self):
        self.feature()
        original = self.store.save
        once = [False]
        def fail(key, state, expected):
            if any(t["phase"] == "synced" for t in state["tasks"].values()) and not once[0]:
                once[0] = True
                raise CoordinationError("simulated interruption")
            return original(key, state, expected)
        self.store.save = fail
        self.assertFalse(self.run_sync().ok)
        self.assert_ok(self.run_sync())
        self.assertEqual(len(self.gh.pull_requests), 1)

    def test_annotated_version_tag_keeps_tag_object(self):
        self.git.run("tag", "-a", "v1.0.0", "-m", "version", cwd=self.wc)
        self.git.run("push", "-q", str(self.urls["github"]), "v1.0.0", cwd=self.wc)
        self.assert_ok(self.run_sync())
        self.assertEqual(self.refs("github")["refs/tags/v1.0.0"], self.refs("forgejo")["refs/tags/v1.0.0"])

    def test_conflicting_tag_is_not_replaced(self):
        self.git.run("tag", "v1.0.0", cwd=self.wc)
        self.git.run("push", "-q", str(self.urls["forgejo"]), "v1.0.0", cwd=self.wc)
        sha = self.commit("approved")
        self.push("github", sha, "refs/heads/master")
        self.git.run("tag", "-f", "v1.0.0", cwd=self.wc)
        self.git.run("push", "-q", str(self.urls["github"]), "v1.0.0", cwd=self.wc)
        self.assertIn("tag object conflict", " ".join(self.run_sync().errors))
        self.assertEqual(self.refs("forgejo")["refs/tags/v1.0.0"], self.base)

    def test_state_compare_and_swap_conflict(self):
        state, revision = self.store.load("key")
        self.store.save("key", state, revision)
        with self.assertRaises(CoordinationError):
            self.store.save("key", state, revision)

    def test_missing_objects_is_error_not_divergence(self):
        self.runner.git.fetch(self.root / "objects/test.git", {"github": str(self.urls["github"])})
        with self.assertRaises(CoordinationError):
            self.git.ancestor(self.root / "objects/test.git", "a" * 40, self.base)

    def test_native_branch_gets_pr_without_forgejo_copy(self):
        sha = self.commit("native")
        self.push("github", sha, "refs/heads/github/task")
        self.assert_ok(self.run_sync())
        self.assertEqual(len(self.gh.pull_requests), 1)
        self.assertNotIn("refs/heads/github/task", self.refs("forgejo"))

    def test_private_public_import_requires_explicit_approval(self):
        repo = replace(self.repo, github_private=False)
        self.repo = repo
        original = self.gh.request
        def metadata(method, path, body=None):
            data = original(method, path, body)
            if path == "/repos/o/test":
                data["private"] = False
            return data
        self.gh.request = metadata
        self.assertIn("not approved", " ".join(self.runner.reconcile(repo).errors))

    def test_disabled_repository_does_not_call_api(self):
        repo = replace(self.repo, enabled=False, disable_reason="migration")
        self.assert_ok(self.runner.reconcile(repo))
        self.assertFalse(self.gh.calls)

    def test_mapping_reversible_and_collision_free(self):
        for name in ("a/b", "a-b", "开发/任务", "topic." + "x" * 100):
            mapped = branch_name(2, "refs/heads/" + name)
            self.assertEqual(original_branch(mapped), name)
            self.assertEqual(self.git.run("check-ref-format", "refs/heads/" + mapped, check=False).returncode, 0)
        self.assertNotEqual(branch_name(2, "refs/heads/a/b"), branch_name(2, "refs/heads/a-b"))

    def test_dispatch_hints_do_not_change_targets(self):
        hint = {"client_payload": {"from": "forgejo", "repo": "f/test", "ref": "refs/heads/dev/a", "sha": "f" * 40}}
        self.assertEqual(validate_event(hint, self.registry), self.repo)
        self.assert_ok(self.run_sync())
        self.assertEqual(self.refs("github")["refs/heads/master"], self.base)

    def test_unknown_owner_is_rejected(self):
        with self.assertRaises(CoordinationError):
            validate_event({"client_payload": {"from": "forgejo", "repo": "evil/test", "ref": "refs/heads/dev/a"}}, self.registry)

    def test_credential_redaction(self):
        with patch.dict("os.environ", {"GH_SYNC_TOKEN": "secret-token"}):
            value = redact("https://secret-token@github.com/ AUTH=secret-token")
        self.assertNotIn("secret-token", value)

if __name__ == "__main__":
    unittest.main()

