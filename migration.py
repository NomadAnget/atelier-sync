"""Prepare an explicit legacy-task adoption file locally; no platform writes or automatic ownership guesses."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from coordination.gitops import CoordinationError, Git, require_ref, require_sha
from coordination.registry import Registry
from coordination.state import empty

def adopt(registry, inventory, selections):
    records={r["name"]:r for r in inventory["repositories"]}
    states={}
    git=Git()
    for row in selections:
        repo=next((r for r in registry.repos if r.name==row["repository"]),None)
        if not repo or not row.get("ownership_confirmed"):
            raise CoordinationError("Adoption requires an explicit registered task ownership confirmation")
        source,head=row["source_ref"],row["mapped_head"]
        require_ref(git,source);require_ref(git,"refs/heads/"+head)
        if source=="refs/heads/"+repo.forgejo_branch or head==repo.github_branch:
            raise CoordinationError("Main references cannot be adopted as task branches")
        item=records[repo.name]
        pr=next((p for p in item["github"].get("prs",[]) if p["number"]==row["pr"]),None)
        if not pr or pr["head"]!=head or pr["base"]!=repo.github_branch:
            raise CoordinationError("Selected legacy PR/head/base is absent from the audit")
        sha=pr["head_sha"];require_sha(sha)
        source_sha=item["forgejo"].get("refs",{}).get(source)
        if not source_sha:
            raise CoordinationError("Source branch is absent; preserve it as history outside active task adoption")
        require_sha(source_sha)
        if pr["state"]=="open":
            remote=item["github"]["refs"].get("refs/heads/"+head)
            if remote!=sha or source_sha!=sha:
                raise CoordinationError("Active legacy source and mapped head must be reconciled before adoption")
            phase="waiting-checks"
        else:
            phase="completed" if pr.get("merged_at") else "closed"
        state=states.setdefault(repo.key,empty())
        if source in state["tasks"]:
            raise CoordinationError("Duplicate source adoption")
        state["tasks"][source]={"generation":1,"head":head,"number":pr["number"],"phase":phase,
            "source_sha":sha,"synced":sha,"deleted":False,"history":[],
            "adopted_from":"explicit-reviewed-migration","observed_source_sha":source_sha}
    return states

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry",type=Path,default=Path("registry.json"))
    parser.add_argument("--inventory",type=Path,required=True)
    parser.add_argument("--selections",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():
        raise CoordinationError("Adoption output must be a fresh review directory")
    states=adopt(Registry.load(args.registry),json.loads(args.inventory.read_text()),json.loads(args.selections.read_text()))
    args.output.mkdir(parents=True)
    for key,state in states.items():
        (args.output/(key+".json")).write_text(json.dumps(state,indent=2)+"\n")
    print(f"Prepared {len(states)} repository ledgers for review; no state branch or remote was changed")

if __name__=="__main__":
    main()

