"""Private CI preparation/receipts and public metadata-only status publication."""
from __future__ import annotations
import argparse
import base64
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile
from .api import Api, ApiError
from .gitops import CoordinationError, Git, require_sha
from .registry import Registry
from .submodules import modules

def snapshot(git, work, sha, destination):
    destination.mkdir(parents=True, exist_ok=True)
    data = git.run("archive", "--format=tar", sha, cwd=work, binary=True).stdout
    # No candidate-controlled links or special files may influence privileged preparation.
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or
                    any(part == ".git" or part == ".env" or part.startswith(".env.") for part in path.parts)
                    or member.issym() or member.islnk() or not (member.isfile() or member.isdir())):
                raise CoordinationError("Source archive has unsafe paths, links, environment files, or special files")
            target = destination.joinpath(*path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(member.mode & 0o777)

def outputs(values):
    if file := os.environ.get("GITHUB_OUTPUT"):
        with open(file, "a") as stream:
            for key, value in values.items():
                stream.write(f"{key}={value}\n")

def prepare(registry, github, git, payload, bundle, control_sha, *, execution_repository=None):
    host = execution_repository or registry.state_repository
    if not registry.state_repository or os.environ.get("GITHUB_REPOSITORY") != host:
        raise CoordinationError("Candidate source must be prepared in the approved execution repository")
    state_meta = github.request("GET", f"/repos/{registry.state_repository}")
    if not state_meta.get("private") or int(state_meta["id"]) != registry.state_repository_id:
        raise CoordinationError("CI artifact repository is not private/registered")
    require_sha(control_sha)
    approved = os.environ.get("COORDINATOR_TRUSTED_SHA")
    if approved != control_sha:
        raise CoordinationError("Coordinator workflow revision has not been approved")
    repository, number = payload.get("repository"), payload.get("pr")
    repo = next((r for r in registry.repos if r.github_name == repository), None)
    if not repo or isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise CoordinationError("Invalid CI repository/PR")
    if not repo.enabled:
        raise CoordinationError("Candidate CI awaits registered repository activation")
    if execution_repository is not None:
        if (execution_repository != repo.github_name or not repo.public_variant_ci
                or repo.role != "assembly" or repo.github_private):
            raise CoordinationError("Public execution is limited to the registered public variant")
    repo.metadata("github", github.request("GET", f"/repos/{repo.github_name}"))
    pr = github.request("GET", f"/repos/{repo.github_name}/pulls/{number}")
    base = github.request("GET", f"/repos/{repo.github_name}/git/ref/heads/{repo.github_branch}")["object"]["sha"]
    if (pr["state"] != "open" or pr["base"]["ref"] != repo.github_branch or
            int(pr["head"]["repo"]["id"]) != repo.github_id or
            payload.get("head_sha") != pr["head"]["sha"] or payload.get("base_sha") != base):
        raise CoordinationError("CI hint is stale, a fork, or not a registered open PR")
    if execution_repository is not None and (os.environ.get("GITHUB_REF") != "refs/heads/" + repo.github_branch
            or os.environ.get("GITHUB_SHA") != base):
        raise CoordinationError("Public variant CI must execute the current default branch")
    work = bundle.parent / "objects" / repo.key
    git.fetch(work, {"github": repo.url("github")})
    git.run("fetch", "--quiet", "--no-tags", repo.url("github"),
            f"+refs/pull/{number}/merge:refs/ci/merge", cwd=work)
    merge = git.run("rev-parse", "refs/ci/merge", cwd=work).stdout.strip()
    parents = git.run("rev-list", "--parents", "-n", "1", merge, cwd=work).stdout.strip().split()[1:]
    if parents != [base, pr["head"]["sha"]]:
        raise CoordinationError("Fetched merge candidate differs from current PR head/base")
    snapshot(git, work, merge, bundle / "source")
    selected = {}
    if repo.role == "assembly":
        for path, child, sha in modules(git, work, merge, repo, registry, include_external=True):
            child_work = bundle.parent / "objects" / child.key
            git.fetch(child_work, {"github": child.url("github")})
            # A pre-merge combination may pin GitHub's candidate merge object.
            if git.run("cat-file", "-e", sha + "^{commit}", cwd=child_work, check=False).returncode:
                git.run("fetch", "--quiet", "--no-tags", child.url("github"), sha, cwd=child_work)
            git.run("cat-file", "-e", sha + "^{commit}", cwd=child_work)
            snapshot(git, child_work, sha, bundle / "source" / path)
            selected[path] = sha
        for child_path in (bundle / "source/src").iterdir():
            if child_path.is_dir() and child_path.name not in ("pipelines", "services") and not (child_path / "header.py").exists():
                raise CoordinationError("Incomplete assembled domain snapshot")
    core = next((r for r in registry.repos if r.role == "core"), None)
    if not core:
        raise CoordinationError("No registered core snapshot")
    require_sha(repo.core_sha)
    core_work = bundle.parent / "objects" / core.key
    git.fetch(core_work, {"github": core.url("github")})
    snapshot(git, core_work, repo.core_sha, bundle / "core")
    (bundle / "control").mkdir(exist_ok=True)
    shutil.copyfile(Path(__file__).with_name("candidate.py"), bundle / "control/candidate.py")
    checks = Path(__file__).resolve().parents[1] / "ci_checks"
    if repo.role == "assembly" and checks.is_dir():
        shutil.copytree(checks, bundle / "control/checks")
    manifest = {"version": 1, "repository": repo.github_name, "repository_id": repo.github_id,
                "pr": number, "head": pr["head"]["sha"], "base": base, "merge": merge,
                "core": repo.core_sha, "submodules": selected, "role": repo.role,
                "commands": repo.commands, "advisory_commands": repo.advisory_commands,
                "control_sha": control_sha}
    if execution_repository is not None:
        manifest["execution_repository"] = execution_repository
        manifest["execution_mode"] = "public-variant-single-job"
    (bundle / "control/manifest.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
    outputs({"repository": repo.github_name, "repository_id": repo.github_id, "pr": number,
             "head": manifest["head"], "base": base, "merge": merge})
    return manifest

def receipt_path(registry, run_id):
    if not str(run_id).isdigit():
        raise CoordinationError("Invalid CI run identity")
    return f"/repos/{registry.state_repository}/contents/ci/{run_id}.json"

def save_receipt(api, registry, receipt):
    path = receipt_path(registry, receipt["run_id"])
    body = {"message": f"ci: receipt {receipt['run_id']}", "branch": registry.state_branch,
            "content": base64.b64encode((json.dumps(receipt, sort_keys=True) + "\n").encode()).decode()}
    try:
        old = api.request("GET", path + "?ref=" + registry.state_branch)
        body["sha"] = old["sha"]
    except ApiError as exc:
        if exc.status != 404:
            raise
    return api.request("PUT", path, body)

def index_path(registry, repository_id, number, head, base):
    require_sha(head); require_sha(base)
    if type(repository_id) is not int or repository_id < 1 or type(number) is not int or number < 1:
        raise CoordinationError("Invalid CI index identity")
    return f"/repos/{registry.state_repository}/contents/ci/index/{repository_id}-{number}-{head}-{base}.json"

def save_index(api, registry, receipt):
    path = index_path(registry, receipt["repository_id"], receipt["pr"], receipt["head"], receipt["base"])
    record = {"version": 1, "run_id": receipt["run_id"]}
    body = {"message": f"ci: current combination {receipt['run_id']}", "branch": registry.state_branch,
            "content": base64.b64encode((json.dumps(record, sort_keys=True) + "\n").encode()).decode()}
    try:
        old = api.request("GET", path + "?ref=" + registry.state_branch)
        previous = json.loads(base64.b64decode(old["content"]))
        if int(previous["run_id"]) > int(receipt["run_id"]):
            return  # Late finalization must not replace a more recent run.
        body["sha"] = old["sha"]
    except ApiError as exc:
        if exc.status != 404:
            raise
    api.request("PUT", path, body)

def read_index(api, registry, repository_id, number, head, base):
    item = api.request("GET", index_path(registry, repository_id, number, head, base) + "?ref=" + registry.state_branch)
    record = json.loads(base64.b64decode(item["content"]))
    if record.get("version") != 1 or not str(record.get("run_id", "")).isdigit():
        raise CoordinationError("Invalid trusted combination index")
    return str(record["run_id"])

def finalize(registry, native, sync, manifest, success, run_id, *, execution_repository=None):
    # Caller manifest is from the trusted prepare job, never from a candidate-produced result file.
    host = execution_repository or registry.state_repository
    if os.environ.get("GITHUB_REPOSITORY") != host:
        raise CoordinationError("Receipts may only be issued by the approved coordinator execution")
    if execution_repository is not None:
        repo = next((r for r in registry.repos if r.github_name == execution_repository), None)
        if (not repo or not repo.public_variant_ci or repo.github_private or repo.role != "assembly"
                or manifest.get("repository") != execution_repository
                or manifest.get("execution_repository") != execution_repository
                or manifest.get("execution_mode") != "public-variant-single-job"):
            raise CoordinationError("Public receipt does not match the registered variant")
    receipt = {**manifest, "run_id": str(run_id), "success": success,
               "workflow": ".github/workflows/trusted-ci.yml",
               "publisher": "private-coordinator", "version": 1}
    save_receipt(native, registry, receipt)
    save_index(native, registry, receipt)
    sync.request("POST", f"/repos/{manifest['repository']}/dispatches",
                 {"event_type": "atelier_ci_result", "client_payload": {"run_id": str(run_id)}})

def read_receipt(api, registry, run_id):
    item = api.request("GET", receipt_path(registry, run_id) + "?ref=" + registry.state_branch)
    receipt = json.loads(base64.b64decode(item["content"]))
    if (receipt.get("version") != 1 or receipt.get("publisher") != "private-coordinator"
            or type(receipt.get("success")) is not bool or str(receipt.get("run_id")) != str(run_id)):
        raise CoordinationError("Unknown CI receipt issuer")
    return receipt

def publish(registry, sync, native, run_id, repository):
    receipt = read_receipt(sync, registry, run_id)
    repo = next((r for r in registry.repos if r.github_name == repository), None)
    if not repo or receipt["repository"] != repository or receipt["repository_id"] != repo.github_id:
        raise CoordinationError("Receipt targets another repository")
    if receipt["control_sha"] != os.environ.get("COORDINATOR_TRUSTED_SHA"):
        raise CoordinationError("Receipt uses an unapproved control revision")
    pr = native.request("GET", f"/repos/{repository}/pulls/{receipt['pr']}")
    base = native.request("GET", f"/repos/{repository}/git/ref/heads/{repo.github_branch}")["object"]["sha"]
    if pr["head"]["sha"] != receipt["head"] or base != receipt["base"] or pr["state"] != "open":
        raise CoordinationError("Receipt head/base no longer matches the open PR")
    # Publisher's workflow runs on the current default branch and executes no candidate artifacts.
    if os.environ.get("GITHUB_SHA") != base:
        raise CoordinationError("Publisher workflow is from a stale default-branch revision")
    description = f"b={base} m={receipt['merge']} r={receipt['run_id']}"
    status = native.request("POST", f"/repos/{repository}/statuses/{receipt['head']}",
                   {"state": "success" if receipt["success"] else "failure",
                    "context": repo.required_context, "description": description,
                    "target_url": f"https://github.com/{repository}/actions/runs/{os.environ['GITHUB_RUN_ID']}"})
    if repo.protection_mode == "ruleset":
        if not status or not isinstance(status.get("id"), int) or status["id"] < 1:
            raise CoordinationError("GitHub status publication has no verified identity")
        if os.environ.get("GITHUB_EVENT_NAME") != "repository_dispatch":
            raise CoordinationError("Metadata publication must use the trusted dispatch workflow")
        receipt["publication"] = {"issuer": "default-branch-metadata", "repository": repository,
            "repository_id": repo.github_id, "pr": receipt["pr"], "head": receipt["head"],
            "base": base, "merge": receipt["merge"], "control_sha": receipt["control_sha"],
            "workflow": repo.ci_workflow, "workflow_sha": base, "event": "repository_dispatch",
            "run_id": os.environ["GITHUB_RUN_ID"], "status_id": status["id"],
            "success": receipt["success"]}
        # Only the trusted default-branch publisher has this Contents-write credential.
        # The candidate cannot supply this record; no Actions-read/status-read scope is needed.
        save_receipt(sync, registry, receipt)
    return receipt

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["prepare", "finalize", "publish"])
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--bundle", type=Path)
    args = parser.parse_args()
    registry = Registry.load(args.registry)
    sync = Api("github")
    native = Api("github", token_env="GITHUB_TOKEN")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    payload = event.get("client_payload", {})
    if args.operation == "prepare":
        prepare(registry, sync, Git(), payload, args.bundle, os.environ["GITHUB_SHA"])
    elif args.operation == "finalize":
        # Only trusted prepare manifest is downloaded here, not arbitrary candidate output.
        manifest = json.loads((args.bundle / "control/manifest.json").read_text())
        finalize(registry, native, sync, manifest, os.environ.get("CANDIDATE_RESULT") == "success",
                 os.environ["GITHUB_RUN_ID"])
    else:
        publish(registry, sync, native, payload.get("run_id"), os.environ["GITHUB_REPOSITORY"])

if __name__ == "__main__":
    main()

