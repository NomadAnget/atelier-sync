"""Render reviewable repository overlays/patches from local backups. Never writes remotes."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from coordination.gitops import CoordinationError, Git
from coordination.registry import Registry

ROOT = Path(__file__).resolve().parent
RETIRED = (
 ".github/workflows/bisync-notify.yml", ".forgejo/workflows/bisync-notify.yml",
 ".forgejo/workflows/bump-variants.yml", ".github/workflows/constitution-check.yml",
 ".github/workflows/monitor-submodules.yml", ".github/workflows/release-link.yml",
 ".forgejo/workflows/publish.yml", ".forgejo/workflows/deploy.yml",
)
def overlay(repo, destination, registry_path):
    changed = []
    for name in RETIRED:
        path = destination / name
        if path.exists():
            path.write_text("""name: Retired legacy automation
on:
  workflow_dispatch:
permissions: {}
jobs:
  retired:
    runs-on: ubuntu-latest
    steps:
      - run: echo "Retired. Use the private atelier-sync coordinator. No credentials or repository writes."
""")
            changed.append(name)
    if repo is not None and repo.public_variant_ci:
        variant_ci = destination / ".github/workflows/trusted-ci.yml"
        variant_ci.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "templates/public-variant-ci.yml", variant_ci)
        changed.append(str(variant_ci.relative_to(destination)))
    target = destination / ".github/workflows/trusted-ci-publish.yml"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "templates/trusted-ci-publish.yml", target)
    changed.append(str(target.relative_to(destination)))
    # Both jobs load the fixed private coordinator; no control source is published here.
    return changed

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=ROOT / "registry.json")
    parser.add_argument("--backups", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    dest = args.destination.resolve()
    if "tmp" not in dest.parts or dest.exists():
        raise CoordinationError("Choose a new isolated directory inside the variant tmp directory")
    registry, git = Registry.load(args.registry), Git()
    dest.mkdir(parents=True)
    reports = []
    for repo in registry.repos:
        backup = args.backups / "github" / (repo.name + ".git")
        if not backup.exists() or not repo.github_id:
            reports.append({"repository": repo.name, "skipped": "GitHub peer unavailable; no implicit repository creation"})
            continue
        checkout = dest / repo.name
        git.run("clone", "--quiet", "--shared", "--branch", repo.github_branch, str(backup.resolve()), str(checkout))
        base = git.run("rev-parse", "HEAD", cwd=checkout).stdout.strip()
        changed = overlay(repo, checkout, args.registry)
        tracked = git.run("diff", "--binary", cwd=checkout).stdout
        untracked = git.run("ls-files", "--others", "--exclude-standard", "-z", cwd=checkout).stdout
        patch = tracked
        for name in filter(None, untracked.split("\0")):
            item = subprocess.run(["git", "diff", "--no-index", "--binary", "--", "/dev/null", name],
                                  cwd=checkout, text=True, capture_output=True)
            if item.returncode not in (0, 1):
                raise CoordinationError("Cannot render new-file patch")
            patch += item.stdout
        patch_file = dest / (repo.name + ".patch")
        patch_file.write_text(patch)
        reports.append({"repository": repo.name, "base": base, "checkout": str(checkout),
                        "patch": str(patch_file), "sha256": hashlib.sha256(patch.encode()).hexdigest(),
                        "changed": changed, "remote_written": False})
    (dest / "manifest.json").write_text(json.dumps(reports, indent=2) + "\n")
    print(f"Rendered {sum('patch' in r for r in reports)} local repository patches; no commits or remote changes")

if __name__ == "__main__":
    main()

