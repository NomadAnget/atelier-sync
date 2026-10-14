"""Complete local Git loop with real candidate execution; platform API remains simulated."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import base64
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from coordination.api import ApiError
from coordination.candidate import execute
from coordination.ci import snapshot
from coordination.engine import Reconciler, Result
from coordination.gitops import Git
from coordination.registry import Repo, Registry
from coordination.state import MemoryStore
from coordination.submodules import coordinate, modules

class CombinationLoopTest(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/"tmp")
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name); self.git=Git(local_auth=True)
        self.child=Repo("domain",1,2,"owner/domain","forge/domain",github_branch="main",forgejo_branch="main",
            enabled=True,protection_ready=True,auto_merge=True,protection_mode="coordinator",github_prefixes=("dev/",))
        self.parent=Repo("variant",3,4,"owner/variant","forge/variant",github_branch="main",forgejo_branch="main",
            enabled=True,protection_ready=True,auto_merge=True,role="assembly",github_private=False,
            allow_public_import=True,public_variant_ci=True,protection_mode="ruleset",ci_workflow=".github/workflows/trusted-ci-publish.yml")
        self.registry=Registry((self.child,self.parent));self.urls={}
        for repo in self.registry.repos:
            for side in ("github","forgejo"):
                target=self.root/(repo.name+"-"+side+".git")
                self.git.run("init","--bare","-q","-b","main",str(target));self.urls[repo.key,side]=str(target)
        self.addCleanup(patch.stopall)
        patch.object(Repo,"url",lambda repo,side,host=None:self.urls[repo.key,side]).start()
        self.cbase=self.commit(self.child,{"impl.py":"answer = 1\n"})
        self.pbase=self.commit(self.parent,{".gitmodules":f'[submodule "domain"]\n path = src/domain\n url = {self.child.url("github")}\n',
            "src/check.py":"from domain.impl import answer\nassert answer > 0\n"}, {"src/domain":self.cbase})
        self.chead=self.commit(self.child,{"impl.py":"answer = 2\n"},base=self.cbase,ref="dev/task")
        self.api=LocalPlatform(self);self.store=MemoryStore()
        self.runner=Reconciler(self.registry,self.git,self.api,self.api,self.store,self.root/"objects",apply=True)
        for repo in self.registry.repos:self.git.fetch(self.runner.work/(repo.key+".git"),{"github":repo.url("github"),"forgejo":repo.url("forgejo")})
        self.pr=self.api.create_pr(self.child.github_name,"dev/task","main","change","")
        self.env=patch.dict(os.environ,{"COORDINATOR_TRUSTED_SHA":"c"*40});self.env.start();self.addCleanup(self.env.stop)
    def commit(self,repo,files,links=None,base=None,ref="main"):
        work=self.root/(repo.name+"-wc")
        if not work.exists():self.git.run("init","-q",str(work))
        env={"GIT_AUTHOR_NAME":"fixture","GIT_AUTHOR_EMAIL":"fixture@localhost","GIT_COMMITTER_NAME":"fixture","GIT_COMMITTER_EMAIL":"fixture@localhost"}
        self.git.run("read-tree",base or "--empty",cwd=work)
        for path,content in files.items():
            source=work/path;source.parent.mkdir(parents=True,exist_ok=True);source.write_text(content)
            blob=self.git.run("hash-object","-w",str(source),cwd=work,extra_env=env).stdout.strip()
            self.git.run("update-index","--add","--cacheinfo",f"100644,{blob},{path}",cwd=work)
        for path,sha in (links or {}).items():self.git.run("update-index","--add","--cacheinfo",f"160000,{sha},{path}",cwd=work)
        tree=self.git.run("write-tree",cwd=work).stdout.strip()
        args=["commit-tree",tree,"-m","fixture"]+(["-p",base] if base else [])
        sha=self.git.run(*args,cwd=work,extra_env=env).stdout.strip()
        for side in (("github","forgejo") if ref=="main" else ("github",)):
            self.git.run("push","-q",repo.url(side),sha+":refs/heads/"+ref,cwd=work)
        return sha
    def child_step(self):
        state,version=self.store.load(self.child.key);result=Result(self.child.name)
        self.runner.manage_pr(self.child,self.runner.work/(self.child.key+".git"),self.pr,self.chead,{},state,version,result)
        return result
    def test_pending_then_actual_combination_then_domain_and_product_merge_backflow(self):
        held=self.child_step()
        self.assertIn("Waiting for public combination CI"," ".join(held.actions))
        self.assertEqual(self.git.refs(self.child.url("github"))["refs/heads/main"],self.cbase)
        draft=self.api.prs(self.parent.github_name)[0]
        self.assertTrue(draft["draft"])
        self.api.test_and_publish(self.parent,draft)
        passed=self.child_step();self.assertIn("domain merged"," ".join(passed.actions))
        merged=self.git.refs(self.child.url("github"))["refs/heads/main"]
        self.assertNotEqual(merged,self.cbase)
        result=self.runner.reconcile(self.child);self.assertTrue(result.ok,result.errors)
        self.assertEqual(self.git.refs(self.child.url("forgejo"))["refs/heads/main"],merged)
        parent_result=Result(self.parent.name);coordinate(self.runner,self.parent,{self.child.key:merged},parent_result)
        product=next(p for p in self.api.prs(self.parent.github_name) if not p["draft"])
        self.assertEqual(self.git.refs(self.parent.url("github"))["refs/heads/main"],self.pbase)
        self.api.test_and_publish(self.parent,product)
        parent_result=Result(self.parent.name);coordinate(self.runner,self.parent,{self.child.key:merged},parent_result)
        self.assertIn("reviewed merge accepted"," ".join(parent_result.actions))
        result=self.runner.reconcile(self.parent);self.assertTrue(result.ok,result.errors)
        final=self.git.refs(self.parent.url("github"))["refs/heads/main"]
        self.assertEqual(self.git.refs(self.parent.url("forgejo"))["refs/heads/main"],final)
        work=self.runner.work/(self.parent.key+".git")
        self.assertEqual(modules(self.git,work,final,self.parent,self.registry)[0][2],merged)
        self.assertNotIn("src/domain/impl.py",self.git.run("ls-tree","-r","--name-only",final,cwd=work).stdout)
    def test_failed_combination_keeps_private_main(self):
        self.child_step();draft=self.api.prs(self.parent.github_name)[0]
        self.api.test_and_publish(self.parent,draft,success=False)
        self.child_step()
        self.assertEqual(self.git.refs(self.child.url("github"))["refs/heads/main"],self.cbase)

class LocalPlatform:
    def __init__(self,test):self.t=test;self.pulls={r.github_name:[] for r in test.registry.repos};self.records={};self.counter=100
    def repo(self,name):return next(r for r in self.t.registry.repos if name in (r.github_name,r.forgejo_name))
    def create_pr(self,name,head,base,title,body,draft=False):
        repo=self.repo(name);work=self.t.runner.work/(repo.key+".git")
        self.t.git.fetch(work,{"github":repo.url("github")})
        refs=self.t.git.refs(repo.url("github"));h=refs["refs/heads/"+head];b=refs["refs/heads/"+base]
        tree=self.t.git.run("rev-parse",h+"^{tree}",cwd=work).stdout.strip()
        env={"GIT_AUTHOR_NAME":"platform","GIT_AUTHOR_EMAIL":"platform@localhost","GIT_COMMITTER_NAME":"platform","GIT_COMMITTER_EMAIL":"platform@localhost"}
        m=self.t.git.run("commit-tree",tree,"-p",b,"-p",h,"-m","merge candidate",cwd=work,extra_env=env).stdout.strip()
        pr={"number":len(self.pulls[name])+1,"state":"open","draft":draft,"mergeable":True,"auto_merge":None,"merged_at":None,
            "head":{"sha":h,"ref":head,"repo":{"id":repo.github_id}},"base":{"sha":b,"ref":base},"merge_commit_sha":m,"user":{"login":"owner"}}
        self.pulls[name].append(pr);return deepcopy(pr)
    def prs(self,name):return deepcopy(self.pulls[name])
    def pages(self,path):return self.request("GET",path)
    def request(self,method,path,body=None):
        endpoint=path.split("?")[0]
        if "/contents/ci/" in endpoint:
            if endpoint not in self.records:raise ApiError(404,"pending")
            return {"content":base64.b64encode(json.dumps(self.records[endpoint]).encode()).decode()}
        name="/".join(endpoint.split("/")[2:4]);repo=self.repo(name);work=self.t.runner.work/(repo.key+".git")
        tail=endpoint.split(name,1)[1]
        if method=="POST" and tail=="/dispatches":return None
        if method=="POST" and tail=="/pulls":return self.create_pr(name,body["head"],body["base"],body["title"],body["body"],body["draft"])
        if tail=="":return {"id":repo.github_id if name==repo.github_name else repo.forgejo_id,"full_name":name,"default_branch":"main","private":repo.github_private if name==repo.github_name else repo.forgejo_private}
        if tail.startswith("/pulls/"):
            number=int(tail.split("/")[2]);pr=self.pulls[name][number-1]
            if tail.endswith("/files"):return [{"filename":"impl.py" if repo.role!="assembly" else "src/domain"}]
            if tail.endswith("/reviews"):return []
            return deepcopy(pr)
        if tail.startswith("/git/commits/"):
            data=self.t.git.run("rev-list","--parents","-n","1",tail.rsplit("/",1)[1],cwd=work).stdout.split()
            return {"parents":[{"sha":sha} for sha in data[1:]]}
        if tail.startswith("/rules/branches/"):
            common={"ruleset_id":42,"ruleset_source_type":"Repository","ruleset_source":name}
            return [{**common,"type":"pull_request","parameters":{"allowed_merge_methods":["merge"],"required_approving_review_count":0}},
                {**common,"type":"required_status_checks","parameters":{"strict_required_status_checks_policy":True,"required_status_checks":[{"context":repo.required_context}]}},
                {**common,"type":"non_fast_forward"},{**common,"type":"deletion"}]
        if tail=="/rulesets/42":return {"enforcement":"active","target":"branch","source_type":"Repository","source":name,"bypass_actors":[]}
        raise AssertionError((method,path,body))
    def merge_reviewed(self,name,number,head):
        repo=self.repo(name);pr=self.pulls[name][number-1];assert pr["head"]["sha"]==head and not pr["draft"]
        self.t.git.run("push","-q",repo.url("github"),pr["merge_commit_sha"]+":refs/heads/main",cwd=self.t.runner.work/(repo.key+".git"))
        pr.update(state="closed",merged_at="now");return pr["merge_commit_sha"]
    def test_and_publish(self,repo,pr,success=True):
        self.counter+=1;run=str(self.counter);work=self.t.runner.work/(repo.key+".git")
        bundle=self.t.root/("candidate-"+run)
        snapshot(self.t.git,work,pr["merge_commit_sha"],bundle/"source")
        for path,child,sha in modules(self.t.git,work,pr["merge_commit_sha"],repo,self.t.registry):
            snapshot(self.t.git,self.t.runner.work/(child.key+".git"),sha,bundle/"source"/path)
        (bundle/"control").mkdir();(bundle/"runtime/home").mkdir(parents=True)
        manifest={"role":"frontend","commands":[["{python}","src/check.py"]],"advisory_commands":[]}
        (bundle/"control/manifest.json").write_text(json.dumps(manifest))
        with patch.dict(os.environ,{"HOME":str(bundle/"runtime/home")}):execute(bundle,prepared_runtime=True)
        expected={"repository":repo.github_name,"repository_id":repo.github_id,"pr":pr["number"],"head":pr["head"]["sha"],
            "base":pr["base"]["sha"],"merge":pr["merge_commit_sha"],"control_sha":"c"*40}
        receipt={**expected,"version":1,"publisher":"private-coordinator","workflow":".github/workflows/trusted-ci.yml","success":success,"run_id":run,
            "execution_repository":repo.github_name,"execution_mode":"public-variant-single-job",
            "publication":{**expected,"issuer":"default-branch-metadata","workflow":repo.ci_workflow,"workflow_sha":expected["base"],
                           "event":"repository_dispatch","success":success,"run_id":run,"status_id":self.counter}}
        self.records[f"/repos/NomadAnget/atelier-sync/contents/ci/{run}.json"]=receipt
        self.records[f"/repos/NomadAnget/atelier-sync/contents/ci/index/{repo.github_id}-{pr['number']}-{expected['head']}-{expected['base']}.json"]={"version":1,"run_id":run}

