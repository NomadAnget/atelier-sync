"""Public variant disclosure boundary and stale-combination regression tests."""
from dataclasses import replace
from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from coordination.gitops import CoordinationError
from coordination.registry import Repo, Registry
from coordination.variant_ci import guard, run, runtime_command
from coordination.candidate import execute

class PublicVariantTest(unittest.TestCase):
    def setUp(self):
        self.repo=Repo("variant",1,2,"owner/variant","owner/variant",github_branch="main",
            github_private=False,role="assembly",enabled=True,public_variant_ci=True)
        self.registry=Registry((self.repo,))
        self.env={"GITHUB_REPOSITORY":"owner/variant","GITHUB_REPOSITORY_OWNER":"owner",
            "GITHUB_ACTOR":"owner","GITHUB_REF":"refs/heads/main",
            "GITHUB_EVENT_NAME":"repository_dispatch","PUBLIC_VARIANT_CI_ENABLED":"true"}
        self.event={"action":"atelier_ci","client_payload":{"repository":"owner/variant",
            "pr":1,"head_sha":"a"*40,"base_sha":"b"*40}}
    def test_owner_default_branch_dispatch_accepts_metadata_only(self):
        self.assertEqual(guard(self.registry,self.event,self.env)[0],self.repo)
    def test_outsider_cannot_reach_private_source_transport(self):
        class Never:
            def __getattr__(self,name):
                raise AssertionError("Private API/Git reached: "+name)
        self.env["GITHUB_ACTOR"]="outsider"
        with patch.dict(os.environ,self.env),self.assertRaises(CoordinationError):
            run(self.registry,self.event,Path("unused"),api=Never(),git=Never())
    def test_fork_or_other_repository_cannot_select_source(self):
        self.event["client_payload"]["repository"]="outsider/private-project"
        with self.assertRaises(CoordinationError):guard(self.registry,self.event,self.env)
    def test_public_pull_request_event_never_reaches_source(self):
        self.env["GITHUB_EVENT_NAME"]="pull_request"
        with self.assertRaises(CoordinationError):guard(self.registry,self.event,self.env)
    def test_feature_branch_cannot_prepare_private_source(self):
        self.env["GITHUB_REF"]="refs/heads/feature"
        with self.assertRaises(CoordinationError):guard(self.registry,self.event,self.env)
    def test_inactive_or_private_domain_cannot_use_public_executor(self):
        for repo in (replace(self.repo,enabled=False),replace(self.repo,github_private=True),
                     replace(self.repo,role="python"),replace(self.repo,public_variant_ci=False)):
            with self.assertRaises(CoordinationError):guard(Registry((repo,)),self.event,self.env)
    def test_dispatch_rejects_invalid_sha_and_boolean_pr(self):
        for key,value in (("head_sha","wrong"),("pr",True)):
            event=json.loads(json.dumps(self.event));event["client_payload"][key]=value
            with self.assertRaises(CoordinationError):guard(self.registry,event,self.env)
    def test_candidate_prepared_runtime_executes_real_checks_without_install(self):
        # This is a real subprocess check, not a mocked CI success or a source upload.
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/"tmp") as name:
            bundle=Path(name)
            (bundle/"control").mkdir();(bundle/"source").mkdir()
            source=bundle/"source/check.py"
            source.write_text("import os\nassert not any(os.getenv(k) for k in ('GITHUB_TOKEN','GH_SYNC_TOKEN'))\nassert not os.path.exists('.git')\n")
            manifest={"role":"assembly","commands":[["{python}","check.py"]],"advisory_commands":[]}
            (bundle/"control/manifest.json").write_text(json.dumps(manifest))
            self.assertEqual(execute(bundle,prepared_runtime=True)[0]["code"],0)
            source.write_text("raise RuntimeError('actual required failure')\n")
            with self.assertRaises(RuntimeError):execute(bundle,prepared_runtime=True)
    def test_public_overlay_has_one_job_and_no_source_upload(self):
        import stage
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/"tmp") as name:
            checkout=Path(name)
            stage.overlay(self.repo,checkout,Path(__file__).resolve().parents[1]/"registry.json")
            workflow=(checkout/".github/workflows/trusted-ci.yml").read_text()
            self.assertNotIn("upload-artifact",workflow)
            self.assertNotIn("download-artifact",workflow)
            self.assertNotIn("actions/cache",workflow)
            self.assertNotIn("create-github-app-token",workflow)
            self.assertNotIn("docker",workflow)
            self.assertIn("coordination.variant_ci",workflow)
            self.assertIn("persist-credentials: false",workflow)
            self.assertIn("EXPECTED_BLOB",workflow)

    def test_runtime_is_direct_python_without_credentials_or_container(self):
        command=runtime_command(Path("/candidate"), "/approved/.venv/bin/python")
        self.assertEqual(command[0],"/approved/.venv/bin/python")
        self.assertNotIn("docker",command)
        self.assertFalse(any("TOKEN" in x for x in command))
if __name__=="__main__":unittest.main()

