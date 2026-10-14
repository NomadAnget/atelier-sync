"""Single-job public variant CI. No source artifacts or caches; candidate receives no credential environment variables."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
from .api import Api
from .ci import prepare, finalize
from .gitops import CoordinationError, Git, require_sha, redact
from .registry import Registry
import sys

def guard(registry, event, env=None):
    env = os.environ if env is None else env
    name = env.get("GITHUB_REPOSITORY", "")
    repo = next((r for r in registry.repos if r.github_name == name), None)
    if (not repo or not repo.enabled or not repo.public_variant_ci
            or repo.role != "assembly" or repo.github_private):
        raise CoordinationError("Only an activated registered public variant may run this CI")
    owner = name.split("/")[0]
    if (env.get("GITHUB_REPOSITORY_OWNER") != owner or env.get("GITHUB_ACTOR") not in (owner, "github-actions[bot]")
            or env.get("GITHUB_REF") != "refs/heads/" + repo.github_branch):
        raise CoordinationError("Public source preparation requires the owner on the default branch")
    if env.get("PUBLIC_VARIANT_CI_ENABLED") != "true":
        raise CoordinationError("Public variant CI has not been activated")
    if env.get("GITHUB_EVENT_NAME") != "repository_dispatch" or event.get("action") != "atelier_ci":
        raise CoordinationError("Only trusted coordinator dispatch is supported")
    payload = event.get("client_payload")
    if not isinstance(payload, dict) or payload.get("repository") != repo.github_name:
        raise CoordinationError("Dispatch cannot select another repository")
    for field in ("head_sha", "base_sha"):
        require_sha(payload.get(field))
    if type(payload.get("pr")) is not int or payload["pr"] < 1:
        raise CoordinationError("Invalid variant PR")
    return repo, payload

def runtime_command(bundle, python=sys.executable):
    bundle = Path(bundle).resolve()
    return [python, str(bundle / "control/candidate.py"), str(bundle), "--prepared-runtime"]


def build_runtime(bundle, python=sys.executable):
    # Install only the approved core before executing candidate code.
    subprocess.run(["uv", "pip", "install", "--python", python, "--no-cache",
                    str(Path(bundle) / "core"), "ollama>=0.1.6"], check=True, timeout=600,
                   env={"PATH": os.environ["PATH"], "HOME": str(Path(bundle) / "runtime/home"),
                        "UV_CACHE_DIR": str(Path(bundle) / "runtime/uv-cache")})


def run(registry, event, bundle, *, api=None, git=None):
    repo, payload = guard(registry, event)
    approved = os.environ.get("COORDINATOR_TRUSTED_SHA", "")
    require_sha(approved)
    git = git or Git(registry.forgejo_host, registry.forgejo_user)
    control_root = Path(__file__).resolve().parents[1]
    if git.run("rev-parse", "HEAD", cwd=control_root).stdout.strip() != approved:
        raise CoordinationError("Public variant CI must use the pinned private coordinator checkout")
    bundle = Path(bundle).resolve()
    temporary = Path(os.environ["RUNNER_TEMP"]).resolve()
    if temporary not in bundle.parents or bundle.exists():
        raise CoordinationError("Source bundle must be a fresh directory in this runner temporary directory")
    api = api or Api("github")
    manifest = prepare(registry, api, git, payload, bundle, approved,
                       execution_repository=repo.github_name)
    run_id = os.environ["GITHUB_RUN_ID"]
    if not run_id.isdigit():
        raise CoordinationError("Invalid run identity")
    (bundle / "runtime/home").mkdir(parents=True)
    try:
        build_runtime(bundle)
        result = subprocess.run(runtime_command(bundle), check=False, timeout=1800,
                                cwd=bundle / "source",
                                env={"PATH": os.environ["PATH"],
                                     "HOME": str(bundle / "runtime/home")})
        success = result.returncode == 0
        current = api.request("GET", f"/repos/{repo.github_name}/pulls/{manifest['pr']}")
        base = api.request("GET", f"/repos/{repo.github_name}/git/ref/heads/{repo.github_branch}")["object"]["sha"]
        if (current["state"] != "open" or current["head"]["sha"] != manifest["head"]
                or base != manifest["base"]):
            success = False
        # This trusted host step receives no candidate-authored manifest/result JSON.
        # Source snapshots are never uploaded; only the original prepare metadata is persisted privately.
        finalize(registry, api, api, manifest, success, run_id,
                 execution_repository=repo.github_name)
        return 0 if success else 1
    finally:
        shutil.rmtree(bundle)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args()
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        return run(Registry.load(args.registry), event, args.bundle)
    except (CoordinationError, OSError, ValueError, subprocess.SubprocessError) as exc:
        import sys
        sys.stderr.write(redact(str(exc)) + "\n")
        return 1

if __name__ == "__main__":
    raise SystemExit(main())

