"""Release an approved core tag; prepare/build/publish are separate jobs."""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tomllib
import zipfile
from email.parser import BytesParser
from .api import Api, ApiError
from .ci import outputs, snapshot
from .gitops import CoordinationError, Git, require_sha
from .registry import Registry

VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
def target(registry, git, tag, directory):
    if not isinstance(tag, str) or not VERSION.fullmatch(tag.removeprefix("v")) or not tag.startswith("v"):
        raise CoordinationError("Release requires an exact vX.Y.Z core tag")
    repo = next((r for r in registry.repos if r.role == "core"), None)
    if not repo or not repo.enabled or not repo.protection_ready:
        raise CoordinationError("Core release awaits repository migration activation")
    work = directory / (repo.key + ".git")
    urls = {s: repo.url(s, registry.forgejo_host) for s in ("github", "forgejo")}
    refs = {s: git.refs(u) for s, u in urls.items()}
    ref = "refs/tags/" + tag
    obj = refs["github"].get(ref)
    if not obj or refs["forgejo"].get(ref) != obj:
        raise CoordinationError("Release tag has not completed exact-object Forgejo backflow")
    git.fetch(work, urls)
    sha = git.run("rev-parse", obj + "^{commit}", cwd=work).stdout.strip()
    for side, branch in (("github", repo.github_branch), ("forgejo", repo.forgejo_branch)):
        main = refs[side].get("refs/heads/" + branch)
        if not main or not git.ancestor(work, sha, main):
            raise CoordinationError("Release is not in both approved main histories")
    project = tomllib.loads(git.run("show", sha + ":pyproject.toml", cwd=work).stdout)["project"]
    if project["name"] != "atelier-core" or project["version"] != tag[1:]:
        raise CoordinationError("Core metadata does not match the approved tag/version")
    epoch = git.run("show", "-s", "--format=%ct", sha, cwd=work).stdout.strip()
    return repo, work, obj, sha, epoch

def guard(registry):
    if os.environ.get("GITHUB_REPOSITORY") != registry.state_repository:
        raise CoordinationError("Release may only run in the private coordinator")
    if os.environ.get("RELEASE_ENABLED") != "true":
        raise CoordinationError("Release publication has not been activated")
    if not os.environ.get("COORDINATOR_TRUSTED_SHA") or os.environ.get("GITHUB_SHA") != os.environ["COORDINATOR_TRUSTED_SHA"]:
        raise CoordinationError("Release uses an unapproved control revision")

def prepare(registry, git, tag, bundle):
    guard(registry)
    repo, work, obj, sha, epoch = target(registry, git, tag, bundle.parent / "objects")
    snapshot(git, work, sha, bundle / "source")
    manifest = {"version": 1, "repository": repo.github_name, "repository_id": repo.github_id,
                "tag": tag, "tag_object": obj, "sha": sha, "package": "atelier-core",
                "package_version": tag[1:], "epoch": epoch,
                "control_sha": os.environ["COORDINATOR_TRUSTED_SHA"]}
    (bundle / "manifest.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
    outputs({"sha": sha, "tag": tag})
    return manifest

def distribution_info(path):
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            entries = [x for x in archive.namelist() if x.endswith(".dist-info/METADATA")]
            if len(entries) != 1:
                raise CoordinationError("Wheel metadata is ambiguous")
            raw = archive.read(entries[0])
    elif path.name.endswith(".tar.gz"):
        with tarfile.open(path) as archive:
            entries = [x for x in archive if x.name.endswith("/PKG-INFO") and len(Path(x.name).parts) == 2 and x.isfile()]
            if len(entries) != 1:
                raise CoordinationError("Source distribution metadata is ambiguous")
            raw = archive.extractfile(entries[0]).read()
    else:
        raise CoordinationError("Unexpected distribution format")
    meta = BytesParser().parsebytes(raw)
    return meta.get("Name"), meta.get("Version")

def distributions(directory, manifest):
    result = {}
    for path in directory.iterdir():
        if not path.is_file() or path.is_symlink():
            raise CoordinationError("Unexpected distribution directory/link")
        name, version = distribution_info(path)
        if name != manifest["package"] or version != manifest["package_version"]:
            raise CoordinationError("Distribution metadata disagrees with the approved core tag")
        # Reject unsafe multipart names and unexpected extra files.
        if not re.fullmatch(r"atelier_core-[0-9A-Za-z_.+-]+(?:\.whl|\.tar\.gz)", path.name):
            raise CoordinationError("Unexpected distribution filename")
        result[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if len(result) != 2 or sum(n.endswith(".whl") for n in result) != 1 or sum(n.endswith(".tar.gz") for n in result) != 1:
        raise CoordinationError("Release needs exactly one wheel and one source distribution")
    return result

def publish_files(package_api, upload, owner, directory, manifest):
    hashes = distributions(directory, manifest)
    endpoint = f"/packages/{owner}/pypi/{manifest['package']}/{manifest['package_version']}/files"
    try:
        old = package_api.request("GET", endpoint)
    except ApiError as exc:
        if exc.status != 404:
            raise
        old = []
    existing = {x["name"]: x["sha256"] for x in old}
    for name, digest in existing.items():
        if hashes.get(name) != digest:
            raise CoordinationError("Existing version differs from this release; never replace or delete it")
    missing = [directory / name for name in hashes if name not in existing]
    if missing:
        upload(missing)
    after = {x["name"]: x["sha256"] for x in package_api.request("GET", endpoint)}
    if after != hashes:
        raise CoordinationError("Published package files failed exact digest verification")
    return hashes

def publish(registry, git, native, package_api, manifest, directory):
    guard(registry)
    repo, work, obj, sha, epoch = target(registry, git, manifest["tag"], directory.parent / "objects")
    if manifest.get("control_sha") != os.environ["COORDINATOR_TRUSTED_SHA"] or manifest.get("sha") != sha or manifest.get("tag_object") != obj:
        raise CoordinationError("Release source changed since preparation")
    if manifest.get("repository_id") != repo.github_id or manifest.get("package") != "atelier-core" or manifest.get("package_version") != manifest["tag"][1:]:
        raise CoordinationError("Release manifest has changed")
    def upload(paths):
        env = {**os.environ, "TWINE_USERNAME": registry.forgejo_user,
               "TWINE_PASSWORD": os.environ["FORGEJO_PACKAGE_TOKEN"], "TWINE_NON_INTERACTIVE": "1"}
        # Trusted client parses files; no package installation or candidate build occurs here.
        subprocess.run([os.environ.get("RELEASE_PYTHON", os.sys.executable), "-m", "twine", "upload",
                        "--repository-url", f"https://{registry.forgejo_host}/api/packages/{registry.forgejo_user}/pypi",
                        *map(str, paths)], env=env, check=True, timeout=600)
    hashes = publish_files(package_api, upload, registry.forgejo_user, directory, manifest)
    receipt = {**manifest, "files": hashes, "run_id": os.environ["GITHUB_RUN_ID"]}
    path = f"/repos/{registry.state_repository}/contents/releases/{repo.key}-{manifest['tag']}.json"
    body = {"message": f"release: {manifest['tag']}", "branch": registry.state_branch,
            "content": base64.b64encode((json.dumps(receipt, sort_keys=True) + "\n").encode()).decode()}
    try:
        body["sha"] = native.request("GET", path + "?ref=" + registry.state_branch)["sha"]
    except ApiError as exc:
        if exc.status != 404:
            raise
    native.request("PUT", path, body)
    return receipt

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["prepare", "publish"])
    parser.add_argument("--registry", type=Path, default=Path("registry.json"))
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--dist", type=Path)
    args = parser.parse_args()
    registry = Registry.load(args.registry)
    if args.operation == "prepare":
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        prepare(registry, Git(), event["inputs"]["tag"], args.bundle)
    else:
        manifest = json.loads((args.bundle / "manifest.json").read_text())
        publish(registry, Git(), Api("github", token_env="GITHUB_TOKEN"),
                Api("forgejo", registry.forgejo_host, token_env="FORGEJO_PACKAGE_TOKEN"), manifest, args.dist)

if __name__ == "__main__":
    main()

