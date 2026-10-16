#!/usr/bin/env python3
"""Merge a checked child PR and sync its gitlink into consuming variants."""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"
OWNER = "NomadAnget"
TOKEN = os.environ.get("GH_SYNC_TOKEN", "")
if not TOKEN:
    raise SystemExit("GH_SYNC_TOKEN is required")

def api(method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        API + path, data=data, method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + TOKEN,
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1500]
        raise RuntimeError(f"GitHub API {method} {path} failed ({exc.code}): {detail}") from exc

def path(repo, suffix):
    if not re.fullmatch(r"NomadAnget/atelier-[A-Za-z0-9_.-]+", repo):
        raise RuntimeError(f"Rejected repository: {repo}")
    return f"/repos/{repo}/{suffix}"

def main():
    result = json.loads(Path(os.environ["RESULT_FILE"]).read_text())
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    payload = event.get("client_payload") or {}
    child = payload.get("child_repo") or payload.get("repository")
    pr_number = str(payload.get("pr_number", ""))
    expected_head = payload.get("head_sha") or payload.get("sha")
    expected_base = payload.get("base_sha")
    checked_candidate = result.get("source_sha")
    if not result.get("success"):
        raise RuntimeError("Kernel check failed; no PR was merged.")
    if (not child or not pr_number.isdigit()
        or not re.fullmatch(r"[0-9a-f]{40}", str(expected_head))
        or not re.fullmatch(r"[0-9a-f]{40}", str(checked_candidate or ""))):
        raise RuntimeError("Notification and check result must include full source commit SHAs.")
    if result.get("repository") != child:
        raise RuntimeError("Checked repository does not match the notified PR.")

    pr = api("GET", path(child, f"pulls/{pr_number}"))
    already_merged = pr.get("state") == "closed" and pr.get("merged") is True
    if ((pr.get("state") != "open" and not already_merged) or (pr.get("draft") and not already_merged)
        or pr.get("head", {}).get("sha") != expected_head
        or pr.get("head", {}).get("repo", {}).get("full_name") != child
        or pr.get("base", {}).get("repo", {}).get("full_name") != child
        or pr.get("base", {}).get("ref") != "master"
        or (expected_base and pr.get("base", {}).get("sha") != expected_base)):
        raise RuntimeError("Source PR changed or is not eligible for automatic merge.")

    candidate = api("GET", path(child, f"git/commits/{checked_candidate}"))
    candidate_tree = candidate["tree"]["sha"]
    preview_sha = pr.get("merge_commit_sha")
    if not preview_sha:
        raise RuntimeError("GitHub has not prepared a PR merge result yet; retry after it becomes mergeable.")
    preview = api("GET", path(child, f"git/commits/{preview_sha}"))
    if preview["tree"]["sha"] != candidate_tree:
        raise RuntimeError("PR merge tree differs from the checked head tree; nothing was merged.")

    merged = ({"merged": True, "sha": pr.get("merge_commit_sha")} if already_merged
              else {"merged": False, "sha": checked_candidate})
    if not merged["sha"]:
        raise RuntimeError("Source PR has no merge candidate SHA.")
    promoted = []

    affected = []
    for variant in result.get("variants", []):
        if variant.get("skipped") or variant.get("repository") == child:
            continue
        matches = [p for p, item in variant.get("submodules", {}).items()
                   if item.get("sha") == checked_candidate]
        if matches:
            if len(matches) != 1:
                raise RuntimeError(f"Ambiguous submodule path in {variant['repository']}.")
            affected.append((variant["repository"], matches[0], variant["base"]["sha"]))

    for repo, module_path, base_sha in affected:
        installed_before = api("GET", path(repo, f"contents/{module_path}?ref=master"))
        if already_merged and installed_before.get("sha") == merged["sha"]:
            promoted.append(f"{repo}: already synchronized")
            continue
        current = api("GET", path(repo, "git/ref/heads/master"))["object"]["sha"]
        if current != base_sha:
            raise RuntimeError(f"{repo}/master moved since it was checked; rerun before promotion.")

    if not already_merged:
        merged = api("PUT", path(child, f"pulls/{pr_number}/merge"),
                     {"sha": expected_head, "merge_method": "merge"})
        if not merged.get("merged"):
            raise RuntimeError(f"Source PR merge failed: {merged.get('message', 'unknown error')}")
    final = api("GET", path(child, f"git/commits/{merged['sha']}"))
    if final["tree"]["sha"] != candidate_tree:
        raise RuntimeError("Merged source tree differs from the checked tree; variant pointers were not changed.")

    for repo, module_path, base_sha in affected:
        current = api("GET", path(repo, "git/ref/heads/master"))["object"]["sha"]
        if current != base_sha:
            raise RuntimeError(f"{repo}/master moved during promotion; pointer update stopped.")
        base_commit = api("GET", path(repo, f"git/commits/{base_sha}"))
        tree = api("POST", path(repo, "git/trees"), {
            "base_tree": base_commit["tree"]["sha"],
            "tree": [{"path": module_path, "mode": "160000", "type": "commit", "sha": merged["sha"]}],
        })
        commit = api("POST", path(repo, "git/commits"), {
            "message": f"ci(submodule): sync {child}#{pr_number}",
            "tree": tree["sha"], "parents": [base_sha],
        })
        slug = child.split("/", 1)[1].replace(".", "-")
        branch = f"automation/sync/{slug}-{pr_number}-{expected_head[:8]}"
        try:
            api("POST", path(repo, "git/refs"), {
                "ref": f"refs/heads/{branch}", "sha": commit["sha"],
            })
        except RuntimeError as exc:
            if "(422)" not in str(exc):
                raise
            existing = api("GET", path(repo, f"git/ref/heads/{branch}"))["object"]["sha"]
            installed_branch = api("GET", path(repo, f"contents/{module_path}?ref={branch}"))
            if installed_branch.get("sha") != merged["sha"]:
                raise RuntimeError(f"{repo}:{branch} exists with different content.") from exc
            commit = api("GET", path(repo, f"git/commits/{existing}"))

        marker = "<!-- atelier-sync-pointer-update -->"
        body = (
            f"atelier-sync generated this pointer update after passing kernel checks.\n\n"
            f"- Source PR: https://github.com/{child}/pull/{pr_number}\n"
            f"- Checked head: {expected_head}\n"
            f"- Merged child commit: {merged['sha']}\n"
            f"- Submodule path: {module_path}\n\n{marker}"
        )
        prs = api("GET", path(repo, f"pulls?state=open&head={OWNER}:{branch}&base=master"))
        pointer_pr = prs[0] if prs else api("POST", path(repo, "pulls"), {
            "title": f"ci(submodule): sync {child}#{pr_number}",
            "head": branch, "base": "master", "body": body,
        })
        current = api("GET", path(repo, "git/ref/heads/master"))["object"]["sha"]
        if current != base_sha:
            raise RuntimeError(f"{repo}/master moved before pointer PR merge.")
        merged_pointer = api("PUT", path(repo, f"pulls/{pointer_pr['number']}/merge"), {
            "sha": pointer_pr["head"]["sha"], "merge_method": "merge",
        })
        if not merged_pointer.get("merged"):
            raise RuntimeError(f"{repo} pointer PR #{pointer_pr['number']} merge failed: {merged_pointer.get('message')}")
        installed = api("GET", path(repo, f"contents/{module_path}?ref=master"))
        if installed.get("sha") != merged["sha"]:
            raise RuntimeError(f"{repo} merged, but its gitlink does not point to the merged child commit.")
        promoted.append(f"{repo} PR #{pointer_pr['number']}")

    lines = [
        "## Central promotion",
        f"- Merged {child} PR #{pr_number} at {merged['sha']}.",
        "- Updated variants: " + (", ".join(promoted) if promoted else "none; this repository is not a variant submodule."),
    ]
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
    print("\n".join(lines))

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Promotion failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
