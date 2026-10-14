"""Persistence, release immutability, transport uncertainty, and rollout boundaries."""
from __future__ import annotations
import base64
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile
import test_bisync as fixtures
from test_controls import Replies
from coordination.api import ApiError
from coordination.gitops import Git, CoordinationError
from coordination.registry import Registry
from coordination.release import distributions, publish_files, target
from coordination.state import FileStore, RemoteStore, empty
import coordinator
import stage

class PersistenceTest(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).resolve().parents[1]/"tmp"
        root.mkdir(exist_ok=True)
        temporary=tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(temporary.cleanup)
        self.directory=Path(temporary.name)
    def test_two_local_writers_cannot_save_same_revision(self):
        a,b=FileStore(self.directory),FileStore(self.directory)
        da,ra=a.load("task"); db,rb=b.load("task")
        a.save("task",da,ra)
        with self.assertRaises(CoordinationError):
            b.save("task",db,rb)
    def remote(self, private=True, branch="master"):
        registry=Registry(())
        api=Replies({
            "/repos/NomadAnget/atelier-sync":{"id":1390502500,"private":private,"default_branch":branch},
            "/repos/NomadAnget/atelier-sync/git/ref/heads/automation-state":{},
        })
        return api,registry
    def test_public_state_repository_is_rejected(self):
        api,registry=self.remote(False)
        with self.assertRaises(CoordinationError): RemoteStore(api,registry)
    def test_state_cannot_share_default_branch(self):
        api,registry=self.remote(branch="automation-state")
        with self.assertRaises(CoordinationError): RemoteStore(api,registry)
    def test_missing_state_branch_is_not_implicitly_created(self):
        api,registry=self.remote()
        api.values["/repos/NomadAnget/atelier-sync/git/ref/heads/automation-state"]=ApiError(404,"missing")
        with self.assertRaises(ApiError): RemoteStore(api,registry)
        self.assertTrue(all(call[0]=="GET" for call in api.calls))
    def test_remote_noop_still_checks_concurrent_revision(self):
        api,registry=self.remote()
        store=RemoteStore(api,registry); data=empty()
        raw=(json.dumps(data,sort_keys=True)+"\n").encode()
        sha=hashlib.sha1(b"blob "+str(len(raw)).encode()+b"\0"+raw).hexdigest()
        path=store.path("task")+"?ref=automation-state"
        api.values[path]={"sha":sha}
        self.assertEqual(store.save("task",data,sha),sha)
        self.assertTrue(all(c[0]=="GET" for c in api.calls))
        api.values[path]={"sha":"a"*40}
        with self.assertRaises(CoordinationError): store.save("task",data,sha)
    def test_remote_cas_conflict_is_explicit(self):
        api,registry=self.remote()
        store=RemoteStore(api,registry)
        api.values[("PUT",store.path("task"))]=ApiError(409,"race")
        with self.assertRaises(CoordinationError): store.save("task",empty(),"a"*40)
    def test_apply_requires_explicit_activation(self):
        with patch.dict(os.environ,{"COORDINATOR_ENABLED":"false"}):
            self.assertEqual(coordinator.main(["--apply"]),1)
    def test_apply_cannot_use_local_auth_or_ephemeral_state(self):
        with patch.dict(os.environ,{"COORDINATOR_ENABLED":"true"}):
            self.assertEqual(coordinator.main(["--apply","--local-auth"]),1)
    def test_rollout_writes_only_workflows_control_and_registry(self):
        checkout=self.directory/"checkout";checkout.mkdir()
        (checkout/".forgejo/workflows").mkdir(parents=True)
        (checkout/".forgejo/workflows/bump-variants.yml").write_text("old writer")
        (checkout/"business.py").write_text("untouched\n")
        source=Path(__file__).resolve().parents[1]
        changes=stage.overlay(None,checkout,source/"registry.json")
        self.assertEqual((checkout/"business.py").read_text(),"untouched\n")
        self.assertTrue(all(n.startswith((".github/",".forgejo/")) for n in changes))
        self.assertNotIn("secrets", (checkout/".forgejo/workflows/bump-variants.yml").read_text())

class ReleaseTest(unittest.TestCase):
    # Override inherited suite: common setup only, release tests below.
    def setUp(self):
        PersistenceTest.setUp(self)
        self.manifest={"package":"atelier-core","package_version":"1.2.3"}
        self.dist=self.directory/"dist";self.dist.mkdir()
        metadata=b"Metadata-Version: 2.1\nName: atelier-core\nVersion: 1.2.3\n\n"
        with zipfile.ZipFile(self.dist/"atelier_core-1.2.3-py3-none-any.whl","w") as wheel:
            wheel.writestr("atelier_core-1.2.3.dist-info/METADATA",metadata)
        with tarfile.open(self.dist/"atelier_core-1.2.3.tar.gz","w:gz") as archive:
            member=tarfile.TarInfo("atelier_core-1.2.3/PKG-INFO");member.size=len(metadata)
            archive.addfile(member,io.BytesIO(metadata))
        self.hashes=distributions(self.dist,self.manifest)
        self.endpoint="/packages/Carnation/pypi/atelier-core/1.2.3/files"
    def api(self,old):
        return Replies({self.endpoint:old})
    def test_existing_equal_version_is_read_back_without_upload(self):
        api=self.api([{"name":n,"sha256":h} for n,h in self.hashes.items()])
        upload=lambda p:self.fail("equal files must not be uploaded")
        self.assertEqual(publish_files(api,upload,"Carnation",self.dist,self.manifest),self.hashes)
    def test_existing_different_digest_is_never_deleted_or_overwritten(self):
        api=self.api([{"name":next(iter(self.hashes)),"sha256":"0"*64}])
        with self.assertRaises(CoordinationError):
            publish_files(api,lambda p:self.fail("mismatch upload"),"Carnation",self.dist,self.manifest)
        self.assertTrue(all(x[0]=="GET" for x in api.calls))
    def test_missing_files_can_resume_after_partial_upload(self):
        name=next(iter(self.hashes));uploaded=[]
        api=self.api([{"name":name,"sha256":self.hashes[name]}])
        def upload(paths):
            uploaded.extend(p.name for p in paths)
            api.values[self.endpoint]=[{"name":n,"sha256":h} for n,h in self.hashes.items()]
        publish_files(api,upload,"Carnation",self.dist,self.manifest)
        self.assertEqual(uploaded,[n for n in self.hashes if n!=name])
    def test_successful_upload_requires_actual_registry_hashes(self):
        api=self.api([])
        with self.assertRaises(CoordinationError):
            publish_files(api,lambda paths:None,"Carnation",self.dist,self.manifest)
    def test_wrong_distribution_version_is_rejected(self):
        with self.assertRaises(CoordinationError):
            distributions(self.dist,{"package":"atelier-core","package_version":"9.9.9"})
    def test_missing_wheel_is_not_a_successful_release(self):
        next(self.dist.glob("*.whl")).unlink()
        with self.assertRaises(CoordinationError): distributions(self.dist,self.manifest)
    def test_extra_file_is_rejected_without_execution(self):
        (self.dist/"script.py").write_text("raise RuntimeError('must never execute')")
        with self.assertRaises(CoordinationError): distributions(self.dist,self.manifest)
    def test_tag_missing_mirror_is_rejected(self):
        repo=fixtures.LocalGitTest()
        repo.setUp();self.addCleanup(repo.doCleanups)
        core=replace(repo.repo,role="core")
        registry=Registry((core,))
        with self.assertRaises(CoordinationError):
            target(registry,repo.git,"v1.2.3",self.directory/"objects")
    def test_unversioned_tag_cannot_trigger_release(self):
        with self.assertRaises(CoordinationError):
            target(Registry(()),Git(),"latest",self.directory/"objects")

class TransportTest(unittest.TestCase):
    def test_lost_response_after_successful_push_is_resolved_by_readback(self):
        git=Git();sha="a"*40
        with patch.object(git,"refs",side_effect=[{},{"refs/heads/task":sha}]), patch.object(
                git,"run",return_value=SimpleNamespace(returncode=1,stderr="RPC failed")):
            with patch("coordination.gitops.require_ref"):
                git.push(None,"https://example.invalid/repo","refs/heads/task",sha,None)

class AdmissionStateTest(unittest.TestCase):
    setUp = fixtures.LocalGitTest.setUp
    commit = fixtures.LocalGitTest.commit
    push = fixtures.LocalGitTest.push
    refs = fixtures.LocalGitTest.refs
    def test_reviewed_merge_persists_and_returns_ledger_revision(self):
        sha=self.commit("feature")
        self.push("github",sha,"refs/heads/dev/task")
        pr=self.gh.create_pr(self.repo.github_name,"dev/task","master","task","")
        pr["head"]["sha"]=sha
        self.repo=replace(self.repo,auto_merge=True)
        self.gh.merge_reviewed=lambda *args:sha
        with patch("coordination.engine.admission",return_value={"head":sha}):
            state,version=self.store.load(self.repo.key)
            refs={"github":self.git.refs(str(self.urls["github"]))}
            version=self.runner.manage_pr(self.repo,self.runner.work/(self.repo.key+".git"),pr,sha,refs,state,version,SimpleNamespace(actions=[]))
        self.assertIsInstance(version,int)
class MigrationTest(unittest.TestCase):
    def setUp(self):
        PersistenceTest.setUp(self)
        from coordination.registry import Repo
        self.repo=Repo("x",1,2,"o/x","f/x")
        self.registry=Registry((self.repo,))
        self.sha="a"*40
        self.inventory={"repositories":[{"name":"x","github":{"refs":{"refs/heads/bisync/forgejo/dev":self.sha},
            "prs":[{"number":7,"head":"bisync/forgejo/dev","head_sha":self.sha,"base":"master",
                    "state":"open","merged_at":None}]},"forgejo":{"refs":{"refs/heads/dev":self.sha}}}]}
        self.selection=[{"repository":"x","source_ref":"refs/heads/dev","mapped_head":"bisync/forgejo/dev",
                         "pr":7,"ownership_confirmed":True}]
    def test_adoption_preserves_exact_existing_head_and_pr(self):
        from migration import adopt
        states=adopt(self.registry,self.inventory,self.selection)
        task=states[self.repo.key]["tasks"]["refs/heads/dev"]
        self.assertEqual(task["head"],"bisync/forgejo/dev")
        self.assertEqual(task["number"],7)
    def test_unconfirmed_legacy_ownership_is_rejected(self):
        from migration import adopt
        self.selection[0]["ownership_confirmed"]=False
        with self.assertRaises(CoordinationError): adopt(self.registry,self.inventory,self.selection)
    def test_main_cannot_be_imported_as_task(self):
        from migration import adopt
        self.selection[0]["source_ref"]="refs/heads/master"
        with self.assertRaises(CoordinationError): adopt(self.registry,self.inventory,self.selection)
    def test_active_legacy_pr_requires_matching_live_source(self):
        from migration import adopt
        self.inventory["repositories"][0]["forgejo"]["refs"]["refs/heads/dev"]="b"*40
        with self.assertRaises(CoordinationError): adopt(self.registry,self.inventory,self.selection)
    def test_squash_merged_legacy_task_is_imported_as_completed(self):
        from migration import adopt
        self.inventory["repositories"][0]["github"]["prs"][0].update(state="closed",merged_at="yesterday")
        self.assertEqual(adopt(self.registry,self.inventory,self.selection)[self.repo.key]["tasks"]["refs/heads/dev"]["phase"],"completed")



class ContentsBranchRaceTest(unittest.TestCase):
    def test_unrelated_branch_commit_retries_when_file_sha_is_unchanged(self):
        from urllib.error import HTTPError
        from coordination.api import Api
        import io
        api=Api("github")
        conflict=HTTPError("https://api.github.com",409,"branch moved",{},None)
        response=lambda data:io.BytesIO(json.dumps(data).encode())
        with patch.dict(os.environ,{"GH_SYNC_TOKEN":"synthetic"}),patch.object(api.opener,"open",side_effect=[conflict,response({"sha":"old"}),response({"content":{"sha":"new"}})]) as opening:
            result=api.request("PUT","/repos/o/r/contents/state.json",{"branch":"state","sha":"old","content":"e30="})
        self.assertEqual(result["content"]["sha"],"new")
        self.assertEqual(opening.call_count,3)
    def test_real_same_file_conflict_is_never_overwritten(self):
        from urllib.error import HTTPError
        from coordination.api import Api,ApiError
        import io
        api=Api("github")
        conflict=HTTPError("https://api.github.com",409,"file moved",{},None)
        with patch.dict(os.environ,{"GH_SYNC_TOKEN":"synthetic"}),patch.object(api.opener,"open",side_effect=[conflict,io.BytesIO(b'{"sha":"other-writer"}')]) as opening:
            with self.assertRaises(ApiError):api.request("PUT","/repos/o/r/contents/state.json",{"branch":"state","sha":"old","content":"e30="})
        self.assertEqual(opening.call_count,2)

