"""Read-only migration inventory and complete advertised-reference object backups."""
from __future__ import annotations
import argparse
import concurrent.futures
import json
from pathlib import Path
from coordination.api import Api
from coordination.gitops import CoordinationError, Git, redact
from coordination.registry import Registry
from urllib.parse import quote

ROOT=Path(__file__).resolve().parent
def inspect(repo,registry):
    item={"name":repo.name}
    git=Git(registry.forgejo_host,registry.forgejo_user,local_auth=True)
    for side,name in (("github",repo.github_name),("forgejo",repo.forgejo_name)):
        api=Api(side,registry.forgejo_host,local_auth=True)
        info={"full_name":name}
        item[side]=info
        try:
            meta=api.request("GET",f"/repos/{name}")
            info.update({k:meta.get(k) for k in ("id","default_branch","private","archived","allow_auto_merge","mirror")})
            repo.metadata(side,meta)
        except CoordinationError as exc:
            info["metadata_unverified"]=redact(str(exc))
        try:
            info["refs"]=git.refs(repo.url(side,registry.forgejo_host))
        except CoordinationError as exc:
            info["refs_unverified"]=redact(str(exc))
        if side=="github":
            try:
                info["prs"]=[{"number":p["number"],"state":p["state"],"merged_at":p.get("merged_at"),
                    "head":p["head"]["ref"],"head_sha":p["head"]["sha"],"base":p["base"]["ref"],
                    "url":p["html_url"]} for p in api.prs(name)]
            except CoordinationError as exc:
                info["prs_unverified"]=redact(str(exc))
            branch=quote(repo.github_branch,safe="")
            try:
                info["active_rules"]=api.pages(f"/repos/{name}/rules/branches/{branch}")
                info["rulesets"]=[api.request("GET",f"/repos/{name}/rulesets/{identity}") for identity in {
                    x["ruleset_id"] for x in info["active_rules"] if x.get("ruleset_source_type")=="Repository"}]
            except CoordinationError as exc:
                info["rulesets_unverified"]=redact(str(exc))
            try:
                info["protection"]=api.request("GET",f"/repos/{name}/branches/{branch}/protection")
            except CoordinationError as exc:
                info["protection_unverified"]=redact(str(exc))
        else:
            try:
                info["branch_protections"]=api.pages(f"/repos/{name}/branch_protections")
            except CoordinationError as exc:
                info["protection_unverified"]=redact(str(exc))
    return item

def backup(repo,registry,root):
    result={}
    git=Git(registry.forgejo_host,registry.forgejo_user,local_auth=True)
    for side in ("github","forgejo"):
        directory=(root/side/(repo.name+".git")).resolve()
        if directory.exists():
            raise CoordinationError("Backup location already exists; choose a fresh checkpoint directory")
        directory.parent.mkdir(parents=True,exist_ok=True)
        try:
            git.run("clone","--mirror",repo.url(side,registry.forgejo_host),str(directory))
            git.run("fsck","--connectivity-only",cwd=directory)
            result[side]={"path":str(directory),"complete":True}
        except CoordinationError as exc:
            result[side]={"path":str(directory),"complete":False,"error":redact(str(exc))}
    return result

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry",type=Path,default=ROOT/"registry.json")
    parser.add_argument("--output",type=Path,default=ROOT/"tmp/inventory.json")
    parser.add_argument("--backup",type=Path)
    args=parser.parse_args()
    registry=Registry.load(args.registry)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        items=list(pool.map(lambda r:inspect(r,registry),registry.repos))
    if args.backup:
        for item,repo in zip(items,registry.repos):
            item["backup"]=backup(repo,registry,args.backup)
    args.output.write_text(json.dumps({"repositories":items},ensure_ascii=False,indent=2)+"\n")
    incomplete=False
    for item in items:
        errors=[f"{side}:{key}" for side in ("github","forgejo") for key in item[side] if key.endswith("_unverified")]
        incomplete|=bool(errors)
        print(item["name"],", ".join(errors) if errors else "inventory read")
    return int(incomplete)

if __name__=="__main__":
    raise SystemExit(main())

