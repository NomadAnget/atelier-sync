"""Native auto-merge admission: current head/base, protected branch, trusted CI provenance."""
from __future__ import annotations
import re
import os
from urllib.parse import quote
from .ci import read_receipt
from . import rulesets
from .gitops import CoordinationError

SENSITIVE = (".github/", ".forgejo/", "coordination/", "templates/", "registry.json",
             "repos.toml", "bisync.py", "audit.py", "docs/architecture", "docs/plan", "docs/CONSTITUTION.md",
             "pyproject.toml", "uv.lock", "deploy.sh", ".gitmodules")

def sensitive(paths):
    return any(path.startswith(SENSITIVE) for path in paths)

def provenance(api, repo, pr, base_sha):
    merge_sha = pr.get("merge_commit_sha")
    if not merge_sha or not re.fullmatch(r"[0-9a-f]{40}", merge_sha):
        raise CoordinationError("PR merge candidate is unavailable")
    commits = api.request("GET", f"/repos/{repo.github_name}/git/commits/{merge_sha}")
    parents = [x["sha"] for x in commits["parents"]]
    if len(parents) != 2 or parents != [base_sha, pr["head"]["sha"]]:
        raise CoordinationError("PR merge candidate does not match current head/base")
    if repo.protection_mode == "ruleset":
        return rulesets.provenance(api, repo, pr, base_sha, merge_sha)
    statuses = api.pages(f"/repos/{repo.github_name}/commits/{pr['head']['sha']}/statuses")
    latest = next((s for s in statuses if s["context"] == repo.required_context), None)
    if not latest or latest["state"] != "success":
        raise CoordinationError("Current head/base has no successful trusted CI status")
    match_receipt = re.fullmatch(r"b=([0-9a-f]{40}) m=([0-9a-f]{40}) r=(\d+)", latest.get("description", ""))
    if not match_receipt or match_receipt.group(1) != base_sha or match_receipt.group(2) != merge_sha:
        raise CoordinationError("CI status belongs to a stale/different merge candidate")
    # CI receipts live in the private coordinator; public repositories receive only metadata.
    class ReceiptRegistry:
        state_repository = os.environ.get("COORDINATOR_REPOSITORY", "NomadAnget/atelier-sync")
        state_branch = os.environ.get("COORDINATOR_STATE_BRANCH", "automation-state")
    receipt = read_receipt(api, ReceiptRegistry(), match_receipt.group(3))
    if (receipt.get("success") is not True or receipt.get("repository_id") != repo.github_id
            or receipt.get("pr") != pr["number"] or receipt.get("head") != pr["head"]["sha"]
            or receipt.get("base") != base_sha or receipt.get("merge") != merge_sha
            or not os.environ.get("COORDINATOR_TRUSTED_SHA")
            or receipt.get("control_sha") != os.environ["COORDINATOR_TRUSTED_SHA"]
            or receipt.get("workflow") != ".github/workflows/trusted-ci.yml"):
        raise CoordinationError("CI receipt is not from the approved private control workflow")
    url = latest.get("target_url", "")
    match = re.fullmatch(r"https://github\.com/" + re.escape(repo.github_name) + r"/actions/runs/(\d+)", url)
    if not match or latest.get("creator", {}).get("login") != "github-actions[bot]":
        raise CoordinationError("CI status has an untrusted publisher/target")
    run_id = match[1]
    run = api.request("GET", f"/repos/{repo.github_name}/actions/runs/{run_id}")
    if (run["event"] != "repository_dispatch" or run["head_branch"] != repo.github_branch
            or run["head_sha"] != base_sha or run["path"].split("@")[0] != repo.ci_workflow
            or run["conclusion"] != "success"):
        raise CoordinationError("CI run is not from the current trusted default-branch workflow")
    jobs = api.request("GET", f"/repos/{repo.github_name}/actions/runs/{run_id}/jobs")["jobs"]
    for name in ("publish",):
        matches = [j for j in jobs if j["name"] == name]
        if len(matches) != 1 or matches[0]["conclusion"] != "success":
            raise CoordinationError(f"Required CI job {name} did not run successfully")
    return {"run_id": run_id, "head": pr["head"]["sha"], "base": base_sha, "merge": merge_sha}

def admission(api, git, work, repo, pr, expected_head, base_sha):
    if not repo.auto_merge or not repo.protection_ready:
        raise CoordinationError("Auto-merge awaits migration/protection approval")
    if pr["head"]["sha"] != expected_head or pr["base"]["ref"] != repo.github_branch:
        raise CoordinationError("PR head/base identity changed")
    if pr.get("draft") or not pr.get("mergeable") or pr["state"] != "open":
        raise CoordinationError("PR is draft, conflicting, closed, or still calculating mergeability")
    if not git.ancestor(work, base_sha, expected_head):
        raise CoordinationError("PR source must incorporate the latest default branch")
    paths = api.pages(f"/repos/{repo.github_name}/pulls/{pr['number']}/files")
    if sensitive([x["filename"] for x in paths]):
        raise CoordinationError("Automation/security/release changes require explicit manual review")
    protection = (rulesets.protection(api, repo) if repo.protection_mode == "ruleset" else
                  api.request("GET", f"/repos/{repo.github_name}/branches/{quote(repo.github_branch, safe='')}/protection"))
    required = protection.get("required_status_checks") or {}
    contexts = set(required.get("contexts", [])) | {x["context"] for x in required.get("checks", [])}
    if not required.get("strict") or repo.required_context not in contexts:
        raise CoordinationError("Strict trusted CI protection is not installed")
    if not protection.get("enforce_admins", {}).get("enabled"):
        raise CoordinationError("Administrators must be subject to the branch protection")
    review_policy = protection.get("required_pull_request_reviews")
    if review_policy is None:
        raise CoordinationError("Protected main branch must require pull requests")
    if (review_policy.get("bypass_pull_request_allowances") or {}).get("users") or (
            review_policy.get("bypass_pull_request_allowances") or {}).get("apps") or (
            review_policy.get("bypass_pull_request_allowances") or {}).get("teams"):
        raise CoordinationError("Main branch PR bypass allowances are not empty")
    reviews = api.pages(f"/repos/{repo.github_name}/pulls/{pr['number']}/reviews")
    final = {}
    for review in reviews:
        final[review["user"]["login"]] = review
    if any(x["state"] == "CHANGES_REQUESTED" for x in final.values()):
        raise CoordinationError("Outstanding requested changes")
    approved = [x for x in final.values() if x["state"] == "APPROVED"
                and x["commit_id"] == expected_head and x["user"]["login"] != pr["user"]["login"]]
    if len(approved) < review_policy.get("required_approving_review_count", 0):
        raise CoordinationError("Required approvals have not been obtained for this head")
    return provenance(api, repo, pr, base_sha)

