"""Safety gates, private receipts, archive isolation and exact-gitlink integration tests."""
from __future__ import annotations
import base64
from copy import deepcopy
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import test_bisync as fixtures
from coordination.api import ApiError
from coordination.candidate import execute
from coordination.ci import publish, snapshot, read_receipt
from coordination.engine import Result, validate_event
from coordination.gates import admission, provenance
from coordination.gitops import CoordinationError
from coordination.registry import Repo, Registry
from coordination.state import RemoteStore
from coordination.submodules import coordinate, modules, pointer_commit

class Replies:
    def __init__(self, values):
        self.values, self.calls = values, []
    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        value = self.values.get((method, path), self.values.get(path))
        if isinstance(value, Exception):
            raise value
        if value is None and (method, path) not in self.values and path not in self.values:
            raise AssertionError((method, path, body))
        return deepcopy(value)
    def pages(self, path):
        return self.request("GET", path)

class GateTest(unittest.TestCase):
    def setUp(self):
        self.repo = Repo("test", 1, 2, "o/test", "f/test", auto_merge=True, protection_ready=True,
                         ci_workflow=".github/workflows/trusted-ci-publish.yml")
        self.h, self.b, self.m, self.c = (x * 40 for x in ("a", "b", "c", "d"))
        self.pr = {"number": 1, "state": "open", "draft": False, "mergeable": True,
                   "head": {"sha": self.h}, "base": {"ref": "master"},
                   "merge_commit_sha": self.m, "user": {"login": "author"}}
        self.receipt = {"version": 1, "publisher": "private-coordinator", "success": True,
                        "repository_id": 1, "repository": "o/test", "pr": 1,
                        "head": self.h, "base": self.b, "merge": self.m, "control_sha": self.c,
                        "workflow": ".github/workflows/trusted-ci.yml", "run_id": "11"}
        self.status = {"state": "success", "context": "atelier/trusted-ci",
                       "description": f"b={self.b} m={self.m} r=11",
                       "target_url": "https://github.com/o/test/actions/runs/12",
                       "creator": {"login": "github-actions[bot]"}}
        self.values = {
            f"/repos/o/test/git/commits/{self.m}": {"parents": [{"sha": self.b}, {"sha": self.h}]},
            f"/repos/o/test/commits/{self.h}/statuses": [self.status],
            "/repos/NomadAnget/atelier-sync/contents/ci/11.json?ref=automation-state":
                {"content": base64.b64encode(json.dumps(self.receipt).encode()).decode()},
            "/repos/o/test/actions/runs/12": {"event": "repository_dispatch", "head_branch": "master",
                "head_sha": self.b, "path": ".github/workflows/trusted-ci-publish.yml", "conclusion": "success"},
            "/repos/o/test/actions/runs/12/jobs": {"jobs": [{"name": "publish", "conclusion": "success"}]},
            "/repos/o/test/pulls/1/files": [{"filename": "implementation.py"}],
            "/repos/o/test/branches/master/protection": {
                "required_status_checks": {"strict": True, "contexts": ["atelier/trusted-ci"]},
                "enforce_admins": {"enabled": True},
                "required_pull_request_reviews": {"required_approving_review_count": 0}},
            "/repos/o/test/pulls/1/reviews": [],
        }
        self.api = Replies(self.values)
        self.git = SimpleNamespace(ancestor=lambda *args: True)
        self.env = patch.dict(os.environ, {"COORDINATOR_TRUSTED_SHA": self.c})
        self.env.start()
        self.addCleanup(self.env.stop)
    def test_trusted_latest_combination_is_admitted(self):
        self.assertEqual(admission(self.api, self.git, None, self.repo, self.pr, self.h, self.b)["merge"], self.m)
    def test_stale_base_status_is_rejected(self):
        self.values[f"/repos/o/test/commits/{self.h}/statuses"][0]["description"] = f"b={'e'*40} m={self.m} r=11"
        with self.assertRaises(CoordinationError):
            provenance(self.api, self.repo, self.pr, self.b)
    def test_same_named_status_cannot_forge_trusted_provenance(self):
        self.values["/repos/o/test/actions/runs/12"]["event"] = "push"
        with self.assertRaises(CoordinationError):
            provenance(self.api, self.repo, self.pr, self.b)
    def test_failed_private_receipt_is_rejected(self):
        self.receipt["success"] = False
        self.values["/repos/NomadAnget/atelier-sync/contents/ci/11.json?ref=automation-state"]["content"] = base64.b64encode(json.dumps(self.receipt).encode()).decode()
        with self.assertRaises(CoordinationError):
            provenance(self.api, self.repo, self.pr, self.b)
    def test_wrong_head_receipt_is_rejected(self):
        self.receipt["head"] = "f" * 40
        self.values["/repos/NomadAnget/atelier-sync/contents/ci/11.json?ref=automation-state"]["content"] = base64.b64encode(json.dumps(self.receipt).encode()).decode()
        with self.assertRaises(CoordinationError):
            provenance(self.api, self.repo, self.pr, self.b)
    def test_skipped_publisher_job_is_not_success(self):
        self.values["/repos/o/test/actions/runs/12/jobs"]["jobs"][0]["conclusion"] = "skipped"
        with self.assertRaises(CoordinationError):
            provenance(self.api, self.repo, self.pr, self.b)
    def test_changed_merge_candidate_is_rejected(self):
        self.values[f"/repos/o/test/git/commits/{self.m}"]["parents"][0]["sha"] = "f" * 40
        with self.assertRaises(CoordinationError):
            provenance(self.api, self.repo, self.pr, self.b)
    def test_workflow_change_always_requires_manual_review(self):
        self.values["/repos/o/test/pulls/1/files"] = [{"filename": ".github/workflows/untrusted.yml"}]
        with self.assertRaises(CoordinationError):
            admission(self.api, self.git, None, self.repo, self.pr, self.h, self.b)
    def test_loose_protection_is_rejected(self):
        self.values["/repos/o/test/branches/master/protection"]["required_status_checks"]["strict"] = False
        with self.assertRaises(CoordinationError):
            admission(self.api, self.git, None, self.repo, self.pr, self.h, self.b)
    def test_unreadable_protection_is_explicit_error(self):
        self.values["/repos/o/test/branches/master/protection"] = ApiError(403, "unavailable")
        with self.assertRaises(ApiError):
            admission(self.api, self.git, None, self.repo, self.pr, self.h, self.b)
    def test_admin_bypass_is_rejected(self):
        self.values["/repos/o/test/branches/master/protection"]["enforce_admins"]["enabled"] = False
        with self.assertRaises(CoordinationError):
            admission(self.api, self.git, None, self.repo, self.pr, self.h, self.b)
    def test_missing_approvals_is_rejected(self):
        self.values["/repos/o/test/branches/master/protection"]["required_pull_request_reviews"]["required_approving_review_count"] = 1
        with self.assertRaises(CoordinationError):
            admission(self.api, self.git, None, self.repo, self.pr, self.h, self.b)
    def test_self_approval_is_not_a_required_approval(self):
        self.values["/repos/o/test/branches/master/protection"]["required_pull_request_reviews"]["required_approving_review_count"] = 1
        self.values["/repos/o/test/pulls/1/reviews"] = [{"state": "APPROVED", "commit_id": self.h, "user": {"login": "author"}}]
        with self.assertRaises(CoordinationError):
            admission(self.api, self.git, None, self.repo, self.pr, self.h, self.b)
    def test_publisher_posts_only_metadata(self):
        registry = Registry((self.repo,))
        self.values["/repos/o/test/pulls/1"] = self.pr
        self.values["/repos/o/test/git/ref/heads/master"] = {"object": {"sha": self.b}}
        self.values[("POST", f"/repos/o/test/statuses/{self.h}")] = {}
        with patch.dict(os.environ, {"GITHUB_SHA": self.b, "GITHUB_RUN_ID": "12"}):
            publish(registry, self.api, self.api, "11", "o/test")
        posted = self.api.calls[-1]
        self.assertEqual(posted[0], "POST")
        self.assertEqual(set(posted[2]), {"context", "state", "description", "target_url"})
        self.assertLessEqual(len(posted[2]["description"]), 140)

class PointerTest(unittest.TestCase):
    def setUp(self):
        self.fx = fixtures.LocalGitTest()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        f = self.fx
        f.repo = replace(f.repo, role="assembly")
        self.child = Repo("child", 3, 4, "o/child", "f/child", enabled=True, protection_ready=True)
        self.child_wc = f.root / "child-wc"
        self.child_urls = {side: f.root / (side + "-child.git") for side in ("github", "forgejo")}
        for url in self.child_urls.values():
            f.git.run("init", "--bare", "-q", "-b", "master", str(url))
        f.git.run("init", "-q", "-b", "master", str(self.child_wc))
        f.git.run("config", "user.name", "test", cwd=self.child_wc)
        f.git.run("config", "user.email", "test@localhost", cwd=self.child_wc)
        self.child_base = self.child_commit("base")
        self.child_next = self.child_commit("next")
        for url in self.child_urls.values():
            f.git.run("push", "-q", str(url), "master", cwd=self.child_wc)
        parent_urls = f.urls
        patch.object(Repo, "url", lambda r, side, host=None: str(
            self.child_urls[side] if r.name == "child" else parent_urls[side])).start()
        f.git.run("config", "--file", ".gitmodules", "submodule.engine.path", "src/engines", cwd=f.wc)
        f.git.run("config", "--file", ".gitmodules", "submodule.engine.url", str(self.child_urls["github"]), cwd=f.wc)
        f.git.run("update-index", "--add", "--cacheinfo", f"160000,{self.child_base},src/engines", cwd=f.wc)
        f.git.run("add", ".gitmodules", cwd=f.wc)
        f.git.run("commit", "-q", "-m", "parent with gitlink", cwd=f.wc)
        self.base = f.git.run("rev-parse", "HEAD", cwd=f.wc).stdout.strip()
        for side in ("github", "forgejo"):
            f.push(side, self.base, "refs/heads/master")
        f.registry = Registry((f.repo, self.child))
        f.runner.registry = f.registry
        self.assertTrue(f.run_sync().ok)
        f.git.fetch(f.runner.work / (self.child.key + ".git"),
                    {side: str(url) for side, url in self.child_urls.items()})
    def child_commit(self, value):
        f = self.fx
        (self.child_wc / "f").write_text(value)
        f.git.run("add", "f", cwd=self.child_wc)
        f.git.run("commit", "-q", "-m", value, cwd=self.child_wc)
        return f.git.run("rev-parse", "HEAD", cwd=self.child_wc).stdout.strip()
    def propose(self, selected=None):
        result = Result("test")
        coordinate(self.fx.runner, self.fx.repo, selected if selected is not None else {
            self.child.key: self.child_next}, result)
        return result
    def test_public_parent_rejects_full_private_child_tree_before_transport(self):
        from coordination.submodules import verify_public_parent_tree
        f=self.fx;parent=replace(f.repo,github_private=False)
        verify_public_parent_tree(f.git,f.wc,self.base,self.base,parent,f.registry)
        f.git.run("rm","--cached","src/engines",cwd=f.wc)
        target=f.wc/"src/engines";target.mkdir(parents=True,exist_ok=True)
        (target/"private_fixture.py").write_text("private_source_fixture = True\n")
        f.git.run("add","src/engines",cwd=f.wc)
        f.git.run("commit","-q","-m","private source tree fixture",cwd=f.wc)
        sha=f.git.run("rev-parse","HEAD",cwd=f.wc).stdout.strip()
        with self.assertRaises(CoordinationError):
            verify_public_parent_tree(f.git,f.wc,sha,self.base,parent,f.registry)

    def test_pointer_pr_keeps_exact_sha_and_main_unchanged(self):
        self.propose()
        f = self.fx
        task = f.store.load(f.repo.key)[0]["pointer_tasks"]["active"]
        work = f.runner.work / (f.repo.key + ".git")
        pin = f.git.run("rev-parse", task["synced"] + ":src/engines", cwd=work).stdout.strip()
        self.assertEqual(pin, self.child_next)
        self.assertEqual(f.refs("github")["refs/heads/master"], self.base)
        self.assertEqual(len(f.gh.pull_requests), 1)
        paths = f.git.run("diff", "--name-only", self.base, task["synced"], cwd=work).stdout.splitlines()
        self.assertEqual(paths, ["src/engines"])
    def test_duplicate_pointer_event_reuses_commit_and_pr(self):
        self.propose()
        old = self.fx.store.load(self.fx.repo.key)[0]["pointer_tasks"]["active"]["synced"]
        self.propose()
        new = self.fx.store.load(self.fx.repo.key)[0]["pointer_tasks"]["active"]["synced"]
        self.assertEqual(old, new)
        self.assertEqual(len(self.fx.gh.pull_requests), 1)
    def test_unconfirmed_child_cannot_advance_parent(self):
        self.propose({})
        self.assertFalse(self.fx.gh.pull_requests)
    def test_base_change_preserves_user_code_and_fast_forward_bot_history(self):
        self.propose()
        f = self.fx
        previous = f.store.load(f.repo.key)[0]["pointer_tasks"]["active"]["synced"]
        current = f.commit("new parent code")
        f.push("github", current, "refs/heads/master")
        self.assertTrue(f.run_sync().ok)
        self.propose()
        latest = f.store.load(f.repo.key)[0]["pointer_tasks"]["active"]["synced"]
        work = f.runner.work / (f.repo.key + ".git")
        self.assertTrue(f.git.ancestor(work, previous, latest))
        self.assertEqual(f.git.run("show", latest + ":f", cwd=work).stdout, "new parent code")
        self.assertEqual(f.git.run("rev-parse", latest + ":src/engines", cwd=work).stdout.strip(), self.child_next)
    def test_closed_pointer_pr_is_not_reopened(self):
        self.propose()
        self.fx.gh.pull_requests[0]["state"] = "closed"
        result = self.propose()
        self.assertIn("manual resumption", " ".join(result.actions))
        self.assertEqual(len(self.fx.gh.pull_requests), 1)
    def test_pointer_pr_found_after_lost_number_checkpoint_stays_owned(self):
        self.propose()
        f = self.fx
        state, revision = f.store.load(f.repo.key)
        del state["pointer_tasks"]["active"]["number"]
        f.store.save(f.repo.key, state, revision)
        self.propose()
        self.assertEqual(f.store.load(f.repo.key)[0]["pointer_tasks"]["active"]["number"], 1)
        f.gh.pull_requests[0]["state"] = "closed"
        self.propose()
        self.assertEqual(len(f.gh.pull_requests), 1)
        self.assertEqual(f.store.load(f.repo.key)[0]["pointer_tasks"]["active"]["phase"], "closed")

    def test_pointer_branch_manual_edit_is_rejected(self):
        self.propose()
        f = self.fx
        task = f.store.load(f.repo.key)[0]["pointer_tasks"]["active"]
        sha = f.commit("manual")
        f.push("github", sha, "refs/heads/" + task["head"], True)
        with self.assertRaises(CoordinationError):
            self.propose()

class IsolationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=fixtures.ROOT / "tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
    def archive(self, member):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            archive.addfile(member, io.BytesIO(b"x") if member.isfile() else None)
        git = SimpleNamespace(run=lambda *a, **k: SimpleNamespace(stdout=buffer.getvalue()))
        return git
    def test_archive_rejects_directory_escape(self):
        member = tarfile.TarInfo("../escape")
        member.size = 1
        with self.assertRaises(CoordinationError):
            snapshot(self.archive(member), None, "a"*40, self.root/"source")
    def test_archive_rejects_symlinks(self):
        member = tarfile.TarInfo("link")
        member.type, member.linkname = tarfile.SYMTYPE, "/home/runner/.gitconfig"
        with self.assertRaises(CoordinationError):
            snapshot(self.archive(member), None, "a"*40, self.root/"source")
    def test_archive_rejects_git_metadata(self):
        member = tarfile.TarInfo(".git/config")
        member.size = 1
        with self.assertRaises(CoordinationError):
            snapshot(self.archive(member), None, "a"*40, self.root/"source")
    def test_archive_rejects_environment_files(self):
        member = tarfile.TarInfo(".env")
        member.size = 1
        with self.assertRaises(CoordinationError):
            snapshot(self.archive(member), None, "a"*40, self.root/"source")
    def test_archive_preserves_executable_mode(self):
        member = tarfile.TarInfo("run.sh")
        member.mode, member.size = 0o755, 1
        snapshot(self.archive(member), None, "a"*40, self.root/"source")
        self.assertEqual((self.root/"source/run.sh").stat().st_mode & 0o777, 0o755)
    def bundle(self, commands, advisory=None):
        (self.root/"source").mkdir(exist_ok=True)
        (self.root/"control").mkdir(exist_ok=True)
        (self.root/"control/manifest.json").write_text(json.dumps({
            "role":"frontend","commands":commands,"advisory_commands":advisory or []}))
        return self.root
    def test_candidate_rejects_privileged_token(self):
        with patch.dict(os.environ, {"GH_SYNC_TOKEN":"test-secret"}):
            with self.assertRaises(RuntimeError):
                execute(self.bundle([["true"]]))
    def test_candidate_cannot_see_parent_environment_token(self):
        command = ["{python}","-c","import os; assert not any(os.environ.get(x) for x in ('GH_TOKEN','GITHUB_TOKEN','GH_SYNC_TOKEN','FORGEJO_SYNC_TOKEN'))"]
        with patch.dict(os.environ, {name:"" for name in ("GH_TOKEN","GITHUB_TOKEN","GH_SYNC_TOKEN","FORGEJO_SYNC_TOKEN")}):
            self.assertEqual(execute(self.bundle([command]))[0]["code"], 0)
    def test_failing_test_blocks_candidate(self):
        with patch.dict(os.environ, {name:"" for name in ("GH_TOKEN","GITHUB_TOKEN","GH_SYNC_TOKEN","FORGEJO_SYNC_TOKEN")}):
            with self.assertRaises(RuntimeError):
                execute(self.bundle([["false"]]))
    def test_unconfigured_checks_are_not_green(self):
        with patch.dict(os.environ, {name:"" for name in ("GH_TOKEN","GITHUB_TOKEN","GH_SYNC_TOKEN","FORGEJO_SYNC_TOKEN")}):
            with self.assertRaises(RuntimeError):
                execute(self.bundle([]))
    def test_advisory_finding_keeps_blocking_tests(self):
        with patch.dict(os.environ, {name:"" for name in ("GH_TOKEN","GITHUB_TOKEN","GH_SYNC_TOKEN","FORGEJO_SYNC_TOKEN")}):
            result = execute(self.bundle([["true"]], [["false"]]))
        self.assertEqual([r["code"] for r in result], [1, 0])
    def test_checker_crash_blocks_even_when_advisory(self):
        with patch.dict(os.environ, {name:"" for name in ("GH_TOKEN","GITHUB_TOKEN","GH_SYNC_TOKEN","FORGEJO_SYNC_TOKEN")}):
            with self.assertRaises(RuntimeError):
                execute(self.bundle([["true"]], [["{python}","-c","raise SystemExit(2)"]]))
    def test_malformed_ref_hint_is_rejected(self):
        repo = Repo("test", 1, 2, "o/test", "f/test")
        with self.assertRaises(CoordinationError):
            validate_event({"client_payload":{"from":"forgejo","repo":"f/test","ref":"refs/heads/a\nbad"}}, Registry((repo,)))

if __name__ == "__main__":
    unittest.main()

