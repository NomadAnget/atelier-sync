"""Metadata-readable rulesets and private CI evidence keep existing PAT scopes unchanged."""
from __future__ import annotations
import os
from urllib.parse import quote
from .ci import read_index, read_receipt
from .gitops import CoordinationError

def protection(api, repo):
    active = api.pages(f"/repos/{repo.github_name}/rules/branches/{quote(repo.github_branch, safe='')}")
    ids = {rule["ruleset_id"] for rule in active
           if rule.get("ruleset_source_type") == "Repository"
           and rule.get("ruleset_source", "").lower() == repo.github_name.lower()}
    if not ids:
        raise CoordinationError("No active repository ruleset applies to main")
    for identity in sorted(ids):
        rule = api.request("GET", f"/repos/{repo.github_name}/rulesets/{identity}")
        if (rule.get("enforcement") != "active" or rule.get("target") != "branch"
                or rule.get("source_type") != "Repository"
                or rule.get("source", "").lower() != repo.github_name.lower()):
            continue
        if "bypass_actors" not in rule:
            raise CoordinationError("Ruleset bypass actors are unreadable with the current identity")
        if rule["bypass_actors"] != []:
            continue  # A separate complete ruleset with no bypass can still enforce all gates.
        enforced = {x["type"]: x for x in active if x.get("ruleset_id") == identity}
        if not {"pull_request", "required_status_checks", "non_fast_forward", "deletion"} <= enforced.keys():
            continue
        checks = enforced["required_status_checks"].get("parameters", {})
        contexts = [x["context"] for x in checks.get("required_status_checks", [])]
        review = enforced["pull_request"].get("parameters", {})
        if not checks.get("strict_required_status_checks_policy") or repo.required_context not in contexts:
            continue
        if review.get("allowed_merge_methods") != ["merge"]:
            raise CoordinationError("The reviewed main ruleset must allow only merge commits")
        if "required_approving_review_count" not in review:
            raise CoordinationError("Ruleset approval requirements are unreadable")
        return {"required_status_checks": {"strict": True, "contexts": contexts},
                "enforce_admins": {"enabled": True},
                "required_pull_request_reviews": {
                    "required_approving_review_count": review["required_approving_review_count"]}}
    raise CoordinationError("No complete, active, bypass-free strict main ruleset was found")

def provenance(api, repo, pr, base, merge):
    class ReceiptRegistry:
        state_repository = os.environ.get("COORDINATOR_REPOSITORY", "NomadAnget/atelier-sync")
        state_branch = os.environ.get("COORDINATOR_STATE_BRANCH", "automation-state")
    run_id = read_index(api, ReceiptRegistry(), repo.github_id, pr["number"], pr["head"]["sha"], base)
    receipt = read_receipt(api, ReceiptRegistry(), run_id)
    expected = {"repository": repo.github_name, "repository_id": repo.github_id, "pr": pr["number"],
                "head": pr["head"]["sha"], "base": base, "merge": merge,
                "control_sha": os.environ.get("COORDINATOR_TRUSTED_SHA")}
    if (not expected["control_sha"] or receipt.get("success") is not True
            or receipt.get("workflow") != ".github/workflows/trusted-ci.yml"
            or any(receipt.get(k) != v for k, v in expected.items())):
        raise CoordinationError("Latest private CI receipt does not approve this head/base/merge")
    if repo.public_variant_ci and (receipt.get("execution_repository") != repo.github_name
            or receipt.get("execution_mode") != "public-variant-single-job"):
        raise CoordinationError("CI receipt is not from the registered public variant execution")
    publication = receipt.get("publication") or {}
    if (any(publication.get(k) != v for k, v in expected.items())
            or publication.get("issuer") != "default-branch-metadata"
            or publication.get("workflow") != repo.ci_workflow
            or publication.get("workflow_sha") != base
            or publication.get("event") != "repository_dispatch"
            or publication.get("success") is not True
            or type(publication.get("status_id")) is not int or publication["status_id"] < 1
            or not str(publication.get("run_id", "")).isdigit()):
        raise CoordinationError("Required status has no trusted default-branch publication receipt")
    proof = {"run_id": str(publication["run_id"]), "private_run_id": run_id,
             "head": expected["head"], "base": base, "merge": merge}
    return proof

