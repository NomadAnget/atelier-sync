"""Atelier collaboration coordinator. Default: read-only; live activation is a separate gate."""
from __future__ import annotations
import argparse
import json
import logging
import os
from pathlib import Path
import fcntl
from coordination.api import Api
from coordination.engine import Reconciler, validate_event
from coordination.gitops import CoordinationError, Git, redact
from coordination.registry import Registry
from coordination.state import FileStore, RemoteStore
from coordination.submodules import coordinate

ROOT = Path(__file__).resolve().parent

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=ROOT / "registry.json")
    parser.add_argument("--work", type=Path, default=ROOT / "tmp/objects")
    parser.add_argument("--local-state", type=Path)
    parser.add_argument("--local-auth", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--event", type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        if args.apply and args.dry_run:
            raise CoordinationError("--apply and --dry-run are mutually exclusive")
        if args.apply and (args.local_auth or args.local_state):
            raise CoordinationError("Live writes require Actions credentials and remote persistent state")
        if args.apply and os.environ.get("COORDINATOR_ENABLED") != "true":
            raise CoordinationError("COORDINATOR_ENABLED must be explicitly true for live writes")
        registry = Registry.load(args.registry)
        names = set(args.only or [r.name for r in registry.repos])
        if names - {r.name for r in registry.repos}:
            raise CoordinationError("Unknown repository filter")
        if args.event:
            item = validate_event(json.loads(args.event.read_text()), registry)
            logging.info("Validated hint for %s; re-read registered live references", item.name)
        git = Git(registry.forgejo_host, registry.forgejo_user, local_auth=args.local_auth)
        github = Api("github", local_auth=args.local_auth)
        forgejo = Api("forgejo", registry.forgejo_host, local_auth=args.local_auth)
        store = RemoteStore(github, registry) if args.apply else FileStore(
            args.local_state or ROOT / "tmp/preview-state")
        args.work.mkdir(parents=True, exist_ok=True)
        with (args.work / ".coordinator.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            runner = Reconciler(registry, git, github, forgejo, store, args.work, apply=args.apply)
            results, mirrored = [], {}
            for repo in registry.repos:
                if repo.name not in names:
                    continue
                result = runner.reconcile(repo)
                results.append(result)
                if repo.enabled and result.ok and args.apply:
                    record, _ = store.load(repo.key)
                    if (record["mirror"].get("forgejo") and
                            record["mirror"].get("github") == record["mirror"]["forgejo"]):
                        mirrored[repo.key] = record["mirror"]["github"]
            for repo in registry.repos:
                result = next((x for x in results if x.repository == repo.name), None)
                if result and result.ok:
                    try:
                        coordinate(runner, repo, mirrored, result)
                    except CoordinationError as exc:
                        result.errors.append(str(exc))
            report = ["# Collaboration reconciliation", "",
                      f"Mode: {'apply' if args.apply else 'read-only'}", ""]
            for result in results:
                report.append(f"## {result.repository}")
                report.extend("- " + redact(x) for x in result.actions)
                report.extend("- ERROR: " + redact(x) for x in result.errors)
                logging.info("%s: %s", result.repository, "error" if result.errors else "disabled" if any(x.startswith("disabled:") for x in result.actions) else "ok")
                for error in result.errors:
                    logging.error("  %s", redact(error))
            if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
                with open(summary, "a") as file:
                    file.write("\n".join(report) + "\n")
            (args.work / "last-report.md").write_text("\n".join(report) + "\n")
            return int(any(not r.ok for r in results))
    except (CoordinationError, OSError, ValueError) as exc:
        logging.error("%s", redact(str(exc)))
        return 1

if __name__ == "__main__":
    raise SystemExit(main())

