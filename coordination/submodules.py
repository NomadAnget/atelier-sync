"""Exact-SHA parent gitlink PRs. Reuses .gitmodules discovery; never rebases or force-pushes."""
from __future__ import annotations
from pathlib import Path, PurePosixPath
import os
import tempfile
from urllib.parse import urljoin, urlparse
from .gitops import CoordinationError, require_sha
from .registry import External

def modules(git, work, sha, parent, registry, *, include_external=False):
    data = git.run("show", sha + ":.gitmodules", cwd=work, check=False)
    if data.returncode:
        # A repository without .gitmodules is legitimate; missing objects are not.
        git.run("cat-file", "-e", sha + "^{commit}", cwd=work)
        tree = git.run("ls-tree", sha, "--", ".gitmodules", cwd=work).stdout.strip()
        if tree:
            raise CoordinationError("Cannot read the selected .gitmodules object")
        return []
    file = work / "modules.config"
    file.write_text(data.stdout)
    entries = git.run("config", "--file", str(file), "--get-regexp",
                      r"^submodule\..*\.path$", check=False)
    if entries.returncode not in (0, 1):
        raise CoordinationError("Invalid .gitmodules")
    found, used = [], set()
    for line in entries.stdout.splitlines():
        key, path = line.split(None, 1)
        name = key.removeprefix("submodule.").removesuffix(".path")
        parts = PurePosixPath(path).parts
        if not parts or path.startswith("/") or ".." in parts or path in used:
            raise CoordinationError("Invalid/duplicate submodule path")
        used.add(path)
        url = git.run("config", "--file", str(file), "--get", f"submodule.{name}.url").stdout.strip()
        resolved = urljoin(parent.url("github") + "/", url)
        parsed = urlparse(resolved)
        if parsed.username or parsed.password:
            raise CoordinationError("Submodule URL contains credentials")
        child = next((r for r in registry.repos if resolved == r.url("github")), None)
        if not child:
            if resolved not in registry.external_submodules:
                raise CoordinationError("Unregistered submodule URL: " + resolved)
            if not include_external:
                continue
            child = External(parsed.path.lstrip("/"))
        if child:
            tree = git.run("ls-tree", sha, "--", path, cwd=work).stdout.strip().split()
            if len(tree) < 3 or tree[0] != "160000":
                raise CoordinationError("Registered submodule path is not a gitlink")
            found.append((path, child, tree[2]))
    return found


def verify_public_parent_tree(git, work, sha, base, parent, registry):
    """Do not publish private child trees when transporting an assembly source branch."""
    expected = modules(git, work, base, parent, registry, include_external=True)
    actual = modules(git, work, sha, parent, registry, include_external=True)
    if not expected or {(p, c.key) for p, c, _ in actual} != {(p, c.key) for p, c, _ in expected}:
        raise CoordinationError("Public parent module layout changed; explicit review before publication")
    for path, _, _ in expected:
        mode = git.run("ls-tree", sha, "--", path, cwd=work).stdout.split()
        if not mode or mode[0] != "160000":
            raise CoordinationError("Private child source must remain a gitlink before public transport")

def pointer_commit(git, work, base, updates, previous=None):
    """Create an automation commit using a private index, without checking out/executing code."""
    directory = Path(work) / "tmp"
    directory.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=directory) as temporary:
        env = {"GIT_INDEX_FILE": str(Path(temporary) / "index"),
               "GIT_AUTHOR_NAME": "atelier-coordinator", "GIT_AUTHOR_EMAIL": "atelier@localhost",
               "GIT_COMMITTER_NAME": "atelier-coordinator", "GIT_COMMITTER_EMAIL": "atelier@localhost"}
        git.run("read-tree", base, cwd=work, extra_env=env)
        for path, sha in updates.items():
            require_sha(sha)
            git.run("update-index", "--add", "--cacheinfo", f"160000,{sha},{path}",
                    cwd=work, extra_env=env)
        tree = git.run("write-tree", cwd=work, extra_env=env).stdout.strip()
        args = ["commit-tree", tree, "-p", previous or base]
        if previous and previous != base and not git.ancestor(work, base, previous):
            args += ["-p", base]
        message = "chore: exact-SHA submodule updates [skip ci]\n\n" + "\n".join(
            f"{path}: {sha}" for path, sha in sorted(updates.items()))
        return git.run(*args, "-m", message, cwd=work, extra_env=env).stdout.strip()

def coordinate(reconciler, parent, mirrored, result):
    """Only confirmed child backflow may propose parent gitlinks."""
    if not parent.enabled or parent.role != "assembly":
        return
    git, api, registry = reconciler.git, reconciler.github, reconciler.registry
    work = reconciler.work / (parent.key + ".git")
    url = parent.url("github")
    refs = git.refs(url)
    base = refs["refs/heads/" + parent.github_branch]
    state, version = reconciler.store.load(parent.key)
    desired = {}
    entries = modules(git, work, base, parent, registry)
    for path, child, pinned in entries:
        sha = mirrored.get(child.key)
        if not sha or sha == pinned:
            continue
        child_work = reconciler.work / (child.key + ".git")
        if git.ancestor(child_work, sha, pinned):
            continue
        if not git.ancestor(child_work, pinned, sha):
            raise CoordinationError(f"{path}: pinned history diverged; no automatic replacement")
        desired[path] = sha
    if not desired:
        return
    task = state["pointer_tasks"].get("active")
    prs = api.prs(parent.github_name)
    if task:
        for path, child, pinned in entries:
            if (path in task.get("desired", {}) and task["desired"][path] != pinned
                    and child.key not in mirrored):
                raise CoordinationError("Partial reconciliation cannot discard a previously selected child update")
        pr = next((p for p in prs if p["number"] == task.get("number")), None)
        if pr and pr["state"] == "closed":
            task["phase"] = "completed" if pr.get("merged_at") else "closed"
            version = reconciler.checkpoint(parent, state, version)
        if task["phase"] == "closed":
            result.actions.append("Pointer PR closed without merge; manual resumption required")
            return
    new = not task or task["phase"] == "completed"
    if new:
        generation = task["generation"] + 1 if task else 1
        head = f"automation/submodules/{parent.key}/g{generation}"
        if "refs/heads/" + head in refs:
            raise CoordinationError("Unowned pointer branch exists")
        task = {"generation": generation, "head": head, "phase": "prepared",
                "desired": {}, "synced": None, "history": [task] if task else []}
        state["pointer_tasks"]["active"] = task
        version = reconciler.checkpoint(parent, state, version)
    head, previous = task["head"], refs.get("refs/heads/" + task["head"])
    if previous != task.get("synced") and not (
            task["phase"] == "publishing" and previous == task.get("prepared_sha")):
        raise CoordinationError("Pointer branch changed outside its owner")
    result.actions.append("propose exact gitlinks: " + ", ".join(f"{p}={s[:12]}" for p, s in sorted(desired.items())))
    if not reconciler.apply:
        return
    if task["desired"] != desired or task.get("base") != base or not previous:
        sha = pointer_commit(git, work, base, desired, previous)
        task.update(phase="publishing", prepared_sha=sha, desired=desired, base=base)
        version = reconciler.checkpoint(parent, state, version)
        git.push(work, url, "refs/heads/" + head, sha, previous)
        task.update(phase="waiting-checks", synced=sha)
        version = reconciler.checkpoint(parent, state, version)
    else:
        sha = previous
        if task["phase"] == "publishing":
            task.update(synced=sha, phase="waiting-checks")
            version = reconciler.checkpoint(parent, state, version)
    current = next((p for p in prs if p["head"]["ref"] == head and p["state"] == "open"), None)
    if not current:
        current = api.create_pr(parent.github_name, head, parent.github_branch,
                                "chore: reviewed submodule integration",
                                "Exact selected child SHAs, after confirmed Forgejo main backflow:\n\n" +
                                "\n".join(f"- {path}: {value}" for path, value in sorted(desired.items())))
        task["number"] = current["number"]
        version = reconciler.checkpoint(parent, state, version)
    if task.get("number") != current["number"]:
        # PR creation may have succeeded before its response/checkpoint was lost.
        # Record the discovered PR too, so a later manual close stays closed.
        task["number"] = current["number"]
        version = reconciler.checkpoint(parent, state, version)
    reconciler.manage_pr(parent, work, current, sha, {"github": refs}, state, version, result)


def candidate_approval(reconciler, child, pr, base, record, result):
    """Review a private domain's exact merge candidate in every consuming public variant."""
    from .gates import sensitive
    from .rulesets import provenance
    api, git, registry = reconciler.github, reconciler.git, reconciler.registry
    if not child.github_private or not child.auto_merge or not child.protection_ready:
        raise CoordinationError("Domain combination policy is not activated")
    child_work = reconciler.work / (child.key + ".git")
    if not git.ancestor(child_work, base, pr["head"]["sha"]):
        raise CoordinationError("Domain branch must incorporate the latest main")
    if sensitive([x["filename"] for x in api.pages(f"/repos/{child.github_name}/pulls/{pr['number']}/files")]):
        raise CoordinationError("Workflow, dependency, or contract changes need manual review")
    reviews = api.pages(f"/repos/{child.github_name}/pulls/{pr['number']}/reviews")
    latest_reviews = {r["user"]["login"]: r for r in reviews}
    if any(r["state"] == "CHANGES_REQUESTED" for r in latest_reviews.values()):
        raise CoordinationError("Domain PR has requested changes")
    merge = pr.get("merge_commit_sha")
    require_sha(merge)
    parents = api.request("GET", f"/repos/{child.github_name}/git/commits/{merge}")["parents"]
    if [x["sha"] for x in parents] != [base, pr["head"]["sha"]]:
        raise CoordinationError("Domain merge candidate changed")
    combinations = record.setdefault("combinations", {})
    proofs, pending, consumed = {}, [], set()
    for parent in registry.repos:
        if parent.role != "assembly" or parent.github_private:
            continue
        work = reconciler.work / (parent.key + ".git")
        git.fetch(work, {"github": parent.url("github")})
        refs = git.refs(parent.url("github"))
        parent_base = refs["refs/heads/" + parent.github_branch]
        paths = [path for path, module, _ in modules(git, work, parent_base, parent, registry)
                 if module.key == child.key]
        if not paths:
            continue
        consumed.add(parent.key)
        if not parent.enabled or not parent.public_variant_ci or not parent.protection_ready:
            raise CoordinationError("Consuming public variant has not been activated: " + parent.name)
        head = f"automation/combinations/{child.github_id}/pr-{pr['number']}"
        target = "refs/heads/" + head
        previous = refs.get(target)
        item = combinations.get(parent.key)
        if previous and (not item or previous != item.get("head")):
            raise CoordinationError("Combination branch changed outside its owner")
        if not item or item.get("domain_merge") != merge or item.get("base") != parent_base:
            sha = pointer_commit(git, work, parent_base, {path: merge for path in paths}, previous)
            # Record the intended object before transport, so a lost response is recoverable.
            combinations[parent.key] = item = {"head": sha, "base": parent_base,
                "domain_head": pr["head"]["sha"], "domain_base": base, "domain_merge": merge}
            git.push(work, parent.url("github"), target, sha, previous)
        matches = [p for p in api.prs(parent.github_name) if p["head"]["ref"] == head
                   and p["base"]["ref"] == parent.github_branch]
        review = next((p for p in matches if p["state"] == "open"), None)
        if not review and matches:
            raise CoordinationError("Combination PR was closed; explicit resumption required")
        if not review:
            review = api.request("POST", f"/repos/{parent.github_name}/pulls",
                {"head": head, "base": parent.github_branch, "draft": True,
                 "title": f"review: {child.name} PR #{pr['number']} combination",
                 "body": "Pre-merge combination review; only gitlink pointers are published. "
                         "This draft is not merged into product main."})
        review = api.request("GET", f"/repos/{parent.github_name}/pulls/{review['number']}")
        item["pr"] = review["number"]
        if not review.get("draft") or review["head"]["sha"] != item["head"]:
            raise CoordinationError("Combination PR is no longer the expected draft")
        pair = [item["head"], parent_base]
        if item.get("requested_pair") != pair or item.get("requested_control") != os.environ.get("COORDINATOR_TRUSTED_SHA"):
            api.request("POST", f"/repos/{parent.github_name}/dispatches",
                {"event_type": "atelier_ci", "client_payload": {"repository": parent.github_name,
                 "pr": review["number"], "head_sha": item["head"], "base_sha": parent_base}})
            item["requested_pair"] = pair
            item["requested_control"] = os.environ.get("COORDINATOR_TRUSTED_SHA")
        try:
            proof = provenance(api, parent, review, parent_base, review.get("merge_commit_sha"))
        except CoordinationError as exc:
            pending.append(parent.name + ": " + str(exc))
        else:
            proofs[parent.key] = {**proof, "pr": review["number"], "domain_merge": merge}
    if not consumed:
        raise CoordinationError("No public variant consumes this domain; combination review cannot be skipped")
    if pending:
        raise CoordinationError("Waiting for public combination CI: " + "; ".join(pending))
    # All consumers must still be at the bases actually tested.
    for parent in registry.repos:
        if parent.key in consumed and git.refs(parent.url("github"))["refs/heads/" + parent.github_branch] != combinations[parent.key]["base"]:
            raise CoordinationError("Variant base advanced during combination review")
    return {"head": pr["head"]["sha"], "base": base, "merge": merge, "variants": proofs}

