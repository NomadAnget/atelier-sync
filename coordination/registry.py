"""Explicit repository identities, visibility, branch ownership, and trusted CI recipes."""
from __future__ import annotations
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from .gitops import CoordinationError

@dataclass(frozen=True)
class Repo:
    name: str
    github_id: int
    forgejo_id: int
    github_name: str
    forgejo_name: str
    github_branch: str = "master"
    forgejo_branch: str = "master"
    github_private: bool = True
    forgejo_private: bool = True
    enabled: bool = False
    allow_public_import: bool = False
    forgejo_prefixes: tuple[str, ...] = ()
    github_prefixes: tuple[str, ...] = ()
    adopted_forgejo_branches: tuple[str, ...] = ()
    auto_merge: bool = False
    protection_ready: bool = False
    role: str = "python"
    public_variant_ci: bool = False
    core_sha: str = ""
    required_context: str = "atelier/trusted-ci"
    protection_mode: str = "legacy"
    ci_workflow: str = ".github/workflows/trusted-ci.yml"
    commands: tuple[tuple[str, ...], ...] = ()
    advisory_commands: tuple[tuple[str, ...], ...] = ()
    disable_reason: str = ""

    @property
    def key(self):
        return f"gh-{self.github_id}-fj-{self.forgejo_id}"

    def url(self, side, host="forgejo.hurcaguari.top"):
        return f"https://github.com/{self.github_name}.git" if side == "github" else f"https://{host}/{self.forgejo_name}.git"

    def metadata(self, side, meta):
        expected_id = self.github_id if side == "github" else self.forgejo_id
        expected_name = self.github_name if side == "github" else self.forgejo_name
        expected_branch = self.github_branch if side == "github" else self.forgejo_branch
        expected_private = self.github_private if side == "github" else self.forgejo_private
        if int(meta.get("id", 0)) != expected_id or meta.get("full_name", "").lower() != expected_name.lower():
            raise CoordinationError(f"{self.name}: {side} repository identity changed")
        if meta.get("default_branch") != expected_branch:
            raise CoordinationError(f"{self.name}: {side} default branch changed")
        if meta.get("private") != expected_private or meta.get("archived") or meta.get("mirror"):
            raise CoordinationError(f"{self.name}: {side} visibility/archive/mirror mismatch")

@dataclass(frozen=True)
class External:
    github_name: str
    @property
    def key(self):
        return "external-" + self.github_name.encode().hex()
    def url(self, side):
        return "https://github.com/" + self.github_name

@dataclass(frozen=True)
class Registry:
    repos: tuple[Repo, ...]
    forgejo_host: str = "forgejo.hurcaguari.top"
    forgejo_user: str = "Carnation"
    state_repository: str = "NomadAnget/atelier-sync"
    state_repository_id: int = 1390502500
    state_branch: str = "automation-state"
    external_submodules: tuple[str, ...] = ()

    @classmethod
    def load(cls, path):
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if raw.get("version") != 1:
            raise CoordinationError("Unsupported registry version")
        allowed = set(Repo.__dataclass_fields__)
        repos = []
        for item in raw["repositories"]:
            unknown = set(item) - allowed
            if unknown:
                raise CoordinationError("Unknown repository settings: " + ", ".join(sorted(unknown)))
            for name in ("forgejo_prefixes", "github_prefixes", "adopted_forgejo_branches"):
                if name in item:
                    item[name] = tuple(item[name])
            for name in ("commands", "advisory_commands"):
                item[name] = tuple(tuple(c) for c in item.get(name, []))
            repo = Repo(**item)
            if repo.public_variant_ci and repo.protection_mode != "ruleset":
                raise CoordinationError("Public variant CI requires ruleset protection")
            if repo.public_variant_ci and (repo.role != "assembly" or repo.github_private):
                raise CoordinationError("Public variant CI requires a public assembly repository")
            if repo.protection_mode not in ("legacy", "ruleset", "coordinator"):
                raise CoordinationError("Unknown protection mode")
            if repo.protection_mode == "coordinator" and (not repo.github_private or repo.role not in ("python", "frontend")):
                raise CoordinationError("Combination approval is only for private domain repositories")
            for name in (repo.github_name, repo.forgejo_name):
                if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name):
                    raise CoordinationError("Invalid registered repository name")
            if repo.enabled and (repo.github_id <= 0 or repo.forgejo_id <= 0):
                raise CoordinationError("Enabled repositories require verified stable IDs")
            if not repo.enabled and not repo.disable_reason:
                raise CoordinationError("Disabled repository must explain its migration gate")
            repos.append(repo)
        if len({r.key for r in repos}) != len(repos) or len({r.name for r in repos}) != len(repos):
            raise CoordinationError("Duplicate repository registry entry")
        state = raw.get("state", {})
        return cls(tuple(repos), **raw.get("forgejo", {}),
                   state_repository=state.get("repository", "NomadAnget/atelier-sync"),
                   state_repository_id=state.get("repository_id", 1390502500),
                   state_branch=state.get("branch", "automation-state"),
                   external_submodules=tuple(raw.get("external_submodules", [])))

def branch_name(repository_id, source_ref, generation=1):
    if not source_ref.startswith("refs/heads/"):
        raise CoordinationError("Only source branches may be mapped")
    # Hex chunks are reversible, collision-free, and have no unsafe ref components.
    encoded = source_ref.removeprefix("refs/heads/").encode().hex()
    parts = [encoded[i:i+80] for i in range(0, len(encoded), 80)]
    return f"forgejo/{repository_id}/g{generation}/" + "/".join(parts)

def original_branch(mapped):
    try:
        return bytes.fromhex("".join(mapped.split("/")[3:])).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise CoordinationError("Invalid mapped branch encoding") from exc

