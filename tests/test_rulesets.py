"""Ruleset and receipt evidence use only Metadata/Contents/Pull requests reads."""
from __future__ import annotations
import base64
from copy import deepcopy
from dataclasses import replace
import json
import os
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from test_controls import Replies
from coordination.ci import publish, save_index
from coordination.gitops import CoordinationError
from coordination.registry import Repo, Registry
from coordination.rulesets import protection, provenance

class RulesetTest(unittest.TestCase):
    def setUp(self):
        self.repo=Repo("test",1,2,"o/test","f/test",protection_mode="ruleset",
            ci_workflow=".github/workflows/trusted-ci-publish.yml")
        rules=[
            {"type":"pull_request","parameters":{"allowed_merge_methods":["merge"],"required_approving_review_count":0}},
            {"type":"required_status_checks","parameters":{"strict_required_status_checks_policy":True,
                "required_status_checks":[{"context":"atelier/trusted-ci"}]}},
            {"type":"non_fast_forward"},{"type":"deletion"}]
        self.active=[{**r,"ruleset_id":42,"ruleset_source_type":"Repository","ruleset_source":"o/test"} for r in rules]
        self.rule={"id":42,"source":"o/test","source_type":"Repository","target":"branch",
                   "enforcement":"active","bypass_actors":[],"rules":deepcopy(rules)}
        self.api=Replies({"/repos/o/test/rules/branches/master":self.active,"/repos/o/test/rulesets/42":self.rule})
    def test_active_complete_ruleset_needs_no_administration_reads(self):
        self.assertTrue(protection(self.api,self.repo)["enforce_admins"]["enabled"])
        self.assertFalse(any("/protection" in c[1] for c in self.api.calls))
    def test_evaluation_ruleset_is_not_enforcement(self):
        self.rule["enforcement"]="evaluate"
        with self.assertRaises(CoordinationError):protection(self.api,self.repo)
    def test_hidden_bypass_list_is_not_assumed_empty(self):
        del self.rule["bypass_actors"]
        with self.assertRaises(CoordinationError):protection(self.api,self.repo)
    def test_admin_or_app_bypass_prevents_admission(self):
        self.rule["bypass_actors"]=[{"actor_type":"RepositoryRole","actor_id":5,"bypass_mode":"always"}]
        with self.assertRaises(CoordinationError):protection(self.api,self.repo)
    def test_missing_strict_check_is_not_enforcement(self):
        self.active[1]["parameters"]["strict_required_status_checks_policy"]=False
        with self.assertRaises(CoordinationError):protection(self.api,self.repo)
    def test_rules_not_active_on_current_main_cannot_approve(self):
        self.api.values["/repos/o/test/rules/branches/master"]=[]
        with self.assertRaises(CoordinationError):protection(self.api,self.repo)
    def test_force_push_without_explicit_restriction_is_rejected(self):
        self.api.values["/repos/o/test/rules/branches/master"]=self.active[:2]+self.active[3:]
        with self.assertRaises(CoordinationError):protection(self.api,self.repo)

class PrivateProofTest(unittest.TestCase):
    def setUp(self):
        self.repo=Repo("test",1,2,"o/test","f/test",protection_mode="ruleset",
            ci_workflow=".github/workflows/trusted-ci-publish.yml")
        self.registry=Registry((self.repo,))
        self.h,self.b,self.m,self.c=(x*40 for x in "abcd")
        self.pr={"number":1,"state":"open","head":{"sha":self.h}}
        self.expected={"repository":"o/test","repository_id":1,"pr":1,"head":self.h,"base":self.b,
                       "merge":self.m,"control_sha":self.c}
        self.receipt={**self.expected,"version":1,"publisher":"private-coordinator","success":True,
            "workflow":".github/workflows/trusted-ci.yml","run_id":"11",
            "publication":{**self.expected,"issuer":"default-branch-metadata","workflow":self.repo.ci_workflow,
                "workflow_sha":self.b,"event":"repository_dispatch","success":True,"run_id":"12","status_id":123}}
        self.path="/repos/NomadAnget/atelier-sync/contents/ci/11.json"
        self.index=f"/repos/NomadAnget/atelier-sync/contents/ci/index/1-1-{self.h}-{self.b}.json"
        self.values={self.index+"?ref=automation-state":self.encode({"version":1,"run_id":"11"}),
                     self.path+"?ref=automation-state":self.encode(self.receipt),
                     "/repos/o/test/pulls/1":self.pr,
                     "/repos/o/test/git/ref/heads/master":{"object":{"sha":self.b}},
                     ("POST",f"/repos/o/test/statuses/{self.h}"):{"id":123},
                     ("PUT",self.path):{}}
        self.api=Replies(self.values)
        self.env=patch.dict(os.environ,{"COORDINATOR_TRUSTED_SHA":self.c,"GITHUB_SHA":self.b,
                       "GITHUB_RUN_ID":"12","GITHUB_EVENT_NAME":"repository_dispatch"})
        self.env.start();self.addCleanup(self.env.stop)
    def encode(self,obj):return {"content":base64.b64encode(json.dumps(obj).encode()).decode(),"sha":"e"*40}
    def refresh(self):self.values[self.path+"?ref=automation-state"]=self.encode(self.receipt)
    def test_public_variant_cannot_reuse_receipt_from_other_execution(self):
        repo=replace(self.repo,public_variant_ci=True,role="assembly",github_private=False)
        with self.assertRaises(CoordinationError):provenance(self.api,repo,self.pr,self.b,self.m)
        self.receipt.update(execution_repository="another/variant",execution_mode="public-variant-single-job")
        self.refresh()
        with self.assertRaises(CoordinationError):provenance(self.api,repo,self.pr,self.b,self.m)
        self.receipt["execution_repository"]=repo.github_name;self.refresh()
        self.assertEqual(provenance(self.api,repo,self.pr,self.b,self.m)["head"],self.h)

    def test_private_proof_needs_no_actions_or_status_read_scopes(self):
        self.assertEqual(provenance(self.api,self.repo,self.pr,self.b,self.m)["private_run_id"],"11")
        self.assertTrue(all("/contents/" in c[1] for c in self.api.calls))
    def test_missing_metadata_publication_is_not_a_required_check(self):
        del self.receipt["publication"];self.refresh()
        with self.assertRaises(CoordinationError):provenance(self.api,self.repo,self.pr,self.b,self.m)
    def test_publication_for_another_head_is_rejected(self):
        self.receipt["publication"]["head"]="f"*40;self.refresh()
        with self.assertRaises(CoordinationError):provenance(self.api,self.repo,self.pr,self.b,self.m)
    def test_latest_failed_receipt_cannot_reuse_old_green_status(self):
        self.receipt["success"]=False;self.refresh()
        with self.assertRaises(CoordinationError):provenance(self.api,self.repo,self.pr,self.b,self.m)
    def test_unapproved_control_revision_is_rejected(self):
        self.receipt["publication"]["control_sha"]="f"*40;self.refresh()
        with self.assertRaises(CoordinationError):provenance(self.api,self.repo,self.pr,self.b,self.m)
    def test_publication_writes_private_proof_only_after_status(self):
        publish(self.registry,self.api,self.api,"11","o/test")
        methods=[c[0] for c in self.api.calls]
        self.assertLess(methods.index("POST"),methods.index("PUT"))
        stored=json.loads(base64.b64decode(self.api.calls[-1][2]["content"]))
        self.assertEqual(stored["publication"]["status_id"],123)
    def test_late_index_update_cannot_replace_newer_result(self):
        self.values[self.index+"?ref=automation-state"]=self.encode({"version":1,"run_id":"99"})
        save_index(self.api,self.registry,self.receipt)
        self.assertTrue(all(c[0]=="GET" for c in self.api.calls))

    def test_complete_auto_merge_admission_uses_the_scope_compatible_path(self):
        from coordination.gates import admission
        fixture = RulesetTest()
        fixture.setUp()
        self.values.update(fixture.api.values)
        self.values["/repos/o/test/pulls/1/files"] = [{"filename":"implementation.py"}]
        self.values["/repos/o/test/pulls/1/reviews"] = []
        self.values[f"/repos/o/test/git/commits/{self.m}"] = {"parents":[{"sha":self.b},{"sha":self.h}]}
        self.pr.update(draft=False, mergeable=True, base={"ref":"master"},
                       merge_commit_sha=self.m, user={"login":"author"})
        repo = replace(self.repo, auto_merge=True, protection_ready=True)
        proof = admission(self.api,SimpleNamespace(ancestor=lambda *a:True),None,repo,self.pr,self.h,self.b)
        self.assertEqual(proof["private_run_id"],"11")
        self.assertFalse(any("/protection" in c[1] or "/actions/" in c[1] or "/statuses" in c[1] for c in self.api.calls))

    def test_first_combination_index_uses_an_independent_sha_scoped_path(self):
        from coordination.api import ApiError
        self.values[self.index+"?ref=automation-state"] = ApiError(404,"new")
        self.values[("PUT",self.index)] = {}
        save_index(self.api,self.registry,self.receipt)
        record = json.loads(base64.b64decode(self.api.calls[-1][2]["content"]))
        self.assertEqual(record,{"version":1,"run_id":"11"})

