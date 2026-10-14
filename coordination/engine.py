"""Reconcile live refs; transport source branches and mirror only GitHub's approved main."""
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
import time
import re
from .api import ApiError
from .gitops import CoordinationError, Git, require_ref, require_sha
from .gates import admission
from .registry import branch_name

@dataclass
class Result:
    repository: str
    actions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    @property
    def ok(self):
        return not self.errors

class Reconciler:
    def __init__(self, registry, git, github, forgejo, store, work, *, apply=False, now=time.time):
        self.registry, self.git = registry, git
        self.github, self.forgejo, self.store = github, forgejo, store
        self.work, self.apply, self.now = Path(work), apply, now

    def checkpoint(self, repo, state, version):
        if self.apply:
            return self.store.save(repo.key, state, version)
        return version

    def reconcile(self, repo):
        result = Result(repo.name)
        if not repo.enabled:
            result.actions.append("disabled: " + repo.disable_reason)
            return result
        try:
            if self.apply and not repo.protection_ready:
                raise CoordinationError("Repository write activation awaits protection/migration approval")
            for side, api, name in (("github", self.github, repo.github_name),
                                     ("forgejo", self.forgejo, repo.forgejo_name)):
                repo.metadata(side, api.request("GET", f"/repos/{name}"))
            if repo.forgejo_private and not repo.github_private and not repo.allow_public_import:
                raise CoordinationError("Private Forgejo source is not approved for public GitHub import")
            work = self.work / (repo.key + ".git")
            urls = {s: repo.url(s, self.registry.forgejo_host) for s in ("github", "forgejo")}
            refs = {s: self.git.refs(u) for s, u in urls.items()}
            for side, branch in (("github", repo.github_branch), ("forgejo", repo.forgejo_branch)):
                if refs[side].get("HEAD") != "refs/heads/" + branch:
                    raise CoordinationError(f"{side}: remote default reference changed")
            self.git.fetch(work, urls)
            state, version = self.store.load(repo.key)
            version = self.mirror(repo, work, urls, refs, state, version, result)
            prs = self.github.prs(repo.github_name)
            live = refs["forgejo"]
            for ref, task in state["tasks"].items():
                if ref not in live and not task.get("deleted"):
                    task["deleted"] = True
                    task["deleted_phase"] = task["phase"]
                    task["phase"] = "source-deleted"
                    version = self.checkpoint(repo, state, version)
                    result.actions.append(f"{ref}: source deleted; history/PR retained")
            for ref, sha in live.items():
                if not ref.startswith("refs/heads/") or ref == "refs/heads/" + repo.forgejo_branch:
                    continue
                source = ref.removeprefix("refs/heads/")
                if not (source in repo.adopted_forgejo_branches or
                        any(source.startswith(p) for p in repo.forgejo_prefixes)):
                    result.actions.append(f"{ref}: unregistered legacy/source branch retained")
                    continue
                if repo.role == "assembly" and not repo.github_private:
                    from .submodules import verify_public_parent_tree
                    verify_public_parent_tree(self.git, work, sha, refs["github"]["refs/heads/" + repo.github_branch], repo, self.registry)
                version = self.source(repo, work, urls, refs, ref, sha, prs, state, version, result)
            for pr in prs:
                if pr["state"] != "open" or pr["base"]["ref"] != repo.github_branch:
                    continue
                if int(pr["head"]["repo"]["id"]) != repo.github_id:
                    continue  # Fork PRs remain manual; no automatic secret-bearing preparations.
                head = pr["head"]["ref"]
                if any(head.startswith(p) for p in repo.github_prefixes) and not head.startswith(("forgejo/", "bisync/")):
                    live_sha = refs["github"].get("refs/heads/" + head)
                    if live_sha == pr["head"]["sha"]:
                        version = self.manage_pr(repo, work, pr, live_sha, refs, state, version, result)
            # Native GitHub branches only receive PRs when explicitly registered by prefix.
            open_heads = {p["head"]["ref"] for p in prs if p["base"]["ref"] == repo.github_branch and p["state"] == "open"}
            for ref, sha in refs["github"].items():
                if not ref.startswith("refs/heads/"):
                    continue
                head = ref.removeprefix("refs/heads/")
                if (head in open_heads or head == repo.github_branch or head.startswith(("forgejo/", "bisync/", "automation/"))
                        or not any(head.startswith(p) for p in repo.github_prefixes)):
                    continue
                base = refs["github"]["refs/heads/" + repo.github_branch]
                if self.git.ancestor(work, sha, base):
                    continue
                old = [p for p in prs if p["head"]["ref"] == head and p["base"]["ref"] == repo.github_branch]
                if old:
                    previous_pr = max(old, key=lambda p: p["number"])
                    if not previous_pr.get("merged_at"):
                        result.actions.append(f"{head}: closed task requires manual resumption")
                        continue
                    if not self.git.ancestor(work, previous_pr["head"]["sha"], sha):
                        raise CoordinationError("Native task branch was rewritten after merge")
                result.actions.append(f"{head}: create native GitHub PR")
                if self.apply:
                    pr = self.github.create_pr(repo.github_name, head, repo.github_branch,
                                               f"review: {head}", "Registered GitHub development task.")
                    version = self.manage_pr(repo, work, pr, sha, refs, state, version, result)
        except (CoordinationError, OSError) as exc:
            result.errors.append(str(exc))
        return result

    def mirror(self, repo, work, urls, refs, state, version, result):
        gh_ref, fj_ref = "refs/heads/" + repo.github_branch, "refs/heads/" + repo.forgejo_branch
        gh, fj = refs["github"].get(gh_ref), refs["forgejo"].get(fj_ref)
        if not gh or not fj:
            raise CoordinationError("Main reference is missing; requires explicit migration bootstrap")
        previous = state["mirror"].get("github")
        if previous and not self.git.ancestor(work, previous, gh):
            raise CoordinationError("GitHub authority rewound/rewrote since the last checkpoint")
        if gh != fj:
            if not self.git.ancestor(work, fj, gh):
                raise CoordinationError("Forgejo main has independent/diverged commits; migration required")
            result.actions.append(f"mirror main {gh[:12]} -> Forgejo (never push GitHub main)")
            if self.apply:
                self.git.push(work, urls["forgejo"], fj_ref, gh, fj)
                refs["forgejo"][fj_ref] = gh
        else:
            result.actions.append(f"main aligned at {gh[:12]}")
        # In dry-run, do not pretend an unapplied write has completed.
        if gh == fj or self.apply:
            state["mirror"] = {"github": gh, "forgejo": gh}
            version = self.checkpoint(repo, state, version)
        for ref, sha in refs["github"].items():
            if not re.fullmatch(r"refs/tags/v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", ref):
                continue
            # Version tags must refer to commits in the approved default-branch history.
            peeled = self.git.run("rev-parse", sha + "^{commit}", cwd=work).stdout.strip()
            if not self.git.ancestor(work, peeled, gh):
                raise CoordinationError(f"{ref}: version tag is outside approved main history")
            target = refs["forgejo"].get(ref)
            if target == sha:
                continue
            if target:
                raise CoordinationError(f"{ref}: tag object conflict")
            result.actions.append(f"mirror exact tag object {ref}")
            if self.apply:
                self.git.push(work, urls["forgejo"], ref, sha, None)
        return version

    def source(self, repo, work, urls, refs, ref, sha, prs, state, version, result):
        require_ref(self.git, ref)
        require_sha(sha)
        base_sha = refs["github"]["refs/heads/" + repo.github_branch]
        task = state["tasks"].get(ref)
        if task and not task.get("deleted") and task["phase"] not in ("completed", "closed"):
            previous = [p for p in prs if p["head"]["ref"] == task["head"]
                        and p["base"]["ref"] == repo.github_branch
                        and int((p["head"].get("repo") or {}).get("id", 0)) == repo.github_id]
            if previous and not any(p["state"] == "open" for p in previous):
                closed = max(previous, key=lambda p: p["number"])
                consumed = closed["head"]["sha"]
                if closed.get("merged_at") and closed.get("merge_commit_sha"):
                    merge = closed["merge_commit_sha"]
                    commit = self.git.run("rev-list", "--parents", "-n", "1", merge,
                                          cwd=work, check=False)
                    if commit.returncode == 0 and len(commit.stdout.split()) == 3:
                        consumed = commit.stdout.split()[2]
                # Observe completion BEFORE advancing the old mapped branch. Otherwise
                # post-merge work can be mistaken for the already consumed task head.
                task.update(phase="completed" if closed.get("merged_at") else "closed",
                            number=closed["number"], source_sha=consumed,
                            merge_sha=closed.get("merge_commit_sha"))
                version = self.checkpoint(repo, state, version)
        if task and task["phase"] == "closed" and not task.get("deleted"):
            result.actions.append(f"{ref}: closed without merge; manual resumption required")
            return version
        if task and task["phase"] == "completed" and not task.get("deleted"):
            if self.git.ancestor(work, sha, task["source_sha"]):
                result.actions.append(f"{ref}: already completed; old work ignored")
                return version
            if not self.git.ancestor(work, task["source_sha"], sha):
                raise CoordinationError(f"{ref}: completed source was rewritten; explicit generation required")
        if self.git.ancestor(work, sha, base_sha) and not (task and task.get("deleted")):
            result.actions.append(f"{ref}: already in approved main")
            if task and task["phase"] != "completed":
                task.update(phase="completed", source_sha=sha)
                version = self.checkpoint(repo, state, version)
            return version
        new = not task or task.get("deleted") or task["phase"] == "completed"
        if new:
            generation = task["generation"] + 1 if task else 1
            head = branch_name(repo.forgejo_id, ref, generation)
            require_ref(self.git, "refs/heads/" + head)
            if refs["github"].get("refs/heads/" + head):
                raise CoordinationError("Unowned mapped branch already exists; adopt explicitly")
            history = (task.get("history", []) + [{k: v for k, v in task.items() if k != "history"}]) if task else []
            task = {"generation": generation, "head": head, "phase": "prepared", "source_sha": sha,
                    "synced": None, "history": history, "deleted": False}
            state["tasks"][ref] = task
            # Persist ownership before publishing the ref, so crash recovery is safe.
            version = self.checkpoint(repo, state, version)
        head = task["head"]
        target_ref = "refs/heads/" + head
        target = refs["github"].get(target_ref)
        if target and not task.get("synced") and target != task["source_sha"]:
            raise CoordinationError(f"{ref}: prepared target was modified outside its owner")
        if task.get("synced") and target != task["synced"] and target != sha:
            raise CoordinationError(f"{ref}: mapped branch modified outside its owner")
        if task["source_sha"] != sha and not self.git.ancestor(work, task["source_sha"], sha):
            raise CoordinationError(f"{ref}: source history rewritten; new generation requires confirmation")
        if target != sha:
            if target and not self.git.ancestor(work, target, sha):
                raise CoordinationError(f"{ref}: mapped branch cannot fast-forward")
            result.actions.append(f"{ref} -> {head} @ {sha[:12]}")
            if self.apply:
                self.git.push(work, urls["github"], target_ref, sha, target)
                refs["github"][target_ref] = sha
                task.update(source_sha=sha, synced=sha, phase="synced")
                version = self.checkpoint(repo, state, version)
        elif self.apply and (task.get("synced") != sha):
            task.update(source_sha=sha, synced=sha, phase="synced")
            version = self.checkpoint(repo, state, version)
        matches = [p for p in prs if p["head"]["ref"] == head and p["base"]["ref"] == repo.github_branch
                   and int(p["head"]["repo"]["id"]) == repo.github_id]
        if len([p for p in matches if p["state"] == "open"]) > 1:
            raise CoordinationError("Multiple PRs for the same task")
        pr = next((p for p in matches if p["state"] == "open"), None)
        if pr is None and matches:
            pr = max(matches, key=lambda p: p["number"])
            task.update(phase="completed" if pr.get("merged_at") else "closed",
                        number=pr["number"], source_sha=pr["head"]["sha"],
                        merge_sha=pr.get("merge_commit_sha"))
            version = self.checkpoint(repo, state, version)
            result.actions.append(f"{ref}: PR #{pr['number']} {task['phase']}")
            return version
        if pr is None:
            result.actions.append(f"{head}: create PR into {repo.github_branch}")
            if not self.apply:
                return version
            body = (f"Forgejo source: {repo.forgejo_name} {ref}\nSource SHA: {sha}\n"
                    f"Task: {repo.key}/g{task['generation']}\n\n"
                    "Resolve conflicts/base updates on the Forgejo source branch; this GitHub branch is a mirror.")
            pr = self.github.create_pr(repo.github_name, head, repo.github_branch, f"Forgejo: {ref[11:]}", body)
            task.update(number=pr["number"], phase="waiting-checks")
            version = self.checkpoint(repo, state, version)
        elif self.apply:
            task["number"] = pr["number"]
            version = self.checkpoint(repo, state, version)
        return self.manage_pr(repo, work, pr, sha, refs, state, version, result)

    def manage_pr(self, repo, work, pr, head_sha, refs, state, version, result):
        if not self.apply:
            result.actions.append(f"PR #{pr['number']}: inspect trusted checks; no mutation")
            return version
        current = self.github.request("GET", f"/repos/{repo.github_name}/pulls/{pr['number']}")
        base_sha = self.git.refs(repo.url("github"))["refs/heads/" + repo.github_branch]
        if current["head"]["sha"] != head_sha or current["state"] != "open":
            result.actions.append(f"PR #{pr['number']}: changed/closed; retry live reconciliation")
            return version
        key = str(pr["number"])
        record = state["github_tasks"].setdefault(key, {})
        if not current.get("mergeable") or current.get("draft"):
            if current.get("auto_merge"):
                self.github.auto_merge(repo.github_name, pr["number"], False)
            result.actions.append(f"PR #{pr['number']}: conflict/draft/mergeability pending")
            return version
        pair = [head_sha, base_sha]
        if repo.protection_mode == "coordinator":
            from .submodules import candidate_approval
            try:
                proof = candidate_approval(self, repo, current, base_sha, record, result)
            except CoordinationError as exc:
                result.actions.append(f"PR #{pr['number']}: combination review held: {exc}")
                return self.checkpoint(repo, state, version)
            latest = self.github.request("GET", f"/repos/{repo.github_name}/pulls/{pr['number']}")
            if latest["head"]["sha"] != head_sha or self.git.refs(repo.url("github")).get(
                    "refs/heads/" + repo.github_branch) != base_sha:
                raise CoordinationError("Domain head/base changed after combination review")
            if not repo.auto_merge:
                result.actions.append(f"PR #{pr['number']}: reviewed; merge disabled")
                return self.checkpoint(repo, state, version)
            record["proof"] = proof
            version = self.checkpoint(repo, state, version)
            record["merge_sha"] = self.github.merge_reviewed(repo.github_name, pr["number"], head_sha)
            result.actions.append(f"PR #{pr['number']}: combination-approved domain merged")
            return self.checkpoint(repo, state, version)
        if record.get("pair") != pair or self.now() - record.get("requested", 0) >= 600:
            # CI request is metadata only. Its trusted receiver reconstructs the merge candidate.
            execution = repo.github_name if repo.public_variant_ci else self.registry.state_repository
            self.github.request("POST", f"/repos/{execution}/dispatches",
                                {"event_type": "atelier_ci", "client_payload": {"repository": repo.github_name, "pr": pr["number"],
                                  "head_sha": head_sha, "base_sha": base_sha}})
            record.update(pair=pair, requested=self.now())
            version = self.checkpoint(repo, state, version)
            result.actions.append(f"PR #{pr['number']}: requested isolated trusted CI")
        try:
            proof = admission(self.github, self.git, work, repo, current, head_sha, base_sha)
        except CoordinationError as exc:
            if current.get("auto_merge"):
                self.github.auto_merge(repo.github_name, pr["number"], False)
            result.actions.append(f"PR #{pr['number']}: auto-merge held: {exc}")
            return version
        # Compare the authority again immediately before asking GitHub to auto-merge.
        latest = self.github.request("GET", f"/repos/{repo.github_name}/pulls/{pr['number']}")
        if latest["head"]["sha"] != head_sha or self.git.refs(repo.url("github")).get(
                "refs/heads/" + repo.github_branch) != base_sha:
            raise CoordinationError("Head/base changed while verifying auto-merge")
        record["proof"] = proof
        version = self.checkpoint(repo, state, version)
        record["merge_sha"] = self.github.merge_reviewed(repo.github_name, pr["number"], head_sha)
        version = self.checkpoint(repo, state, version)
        result.actions.append(f"PR #{pr['number']}: reviewed merge accepted")
        return version

def validate_event(event, registry):
    payload = event.get("client_payload")
    if not isinstance(payload, dict):
        raise CoordinationError("Dispatch payload must be an object")
    source = payload.get("from")
    name = payload.get("repo")
    if source not in ("github", "forgejo") or not isinstance(name, str):
        raise CoordinationError("Dispatch source/repository is invalid")
    match = next((r for r in registry.repos if name == (
        r.github_name if source == "github" else r.forgejo_name)), None)
    if match is None:
        raise CoordinationError("Unregistered dispatch repository identity")
    ref = payload.get("ref", "")
    if not isinstance(ref, str) or not ref.startswith(("refs/heads/", "refs/tags/")):
        raise CoordinationError("Dispatch reference is invalid")
    require_ref(Git(), ref)
    # Hint SHA is validated but never used as a write target; reconciliation reads live refs.
    if "sha" in payload and payload["sha"] is not None:
        require_sha(payload["sha"])
    return match

