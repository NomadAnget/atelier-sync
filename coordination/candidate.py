"""Credential-free candidate executor. Run only in the private coordinator's isolated test job."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

FORBIDDEN = ("GH_SYNC_TOKEN", "FORGEJO_SYNC_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "FORGEJO_PACKAGE_TOKEN", "TWINE_PASSWORD")
def execute(bundle, python=sys.executable, *, prepared_runtime=False):
    bundle = Path(bundle).resolve()
    manifest = json.loads((bundle / "control/manifest.json").read_text())
    leaked = [name for name in FORBIDDEN if os.environ.get(name)]
    if leaked:
        raise RuntimeError("Candidate environment contains privileged credentials: " + ", ".join(leaked))
    if (bundle / "source/.git").exists():
        raise RuntimeError("Candidate source includes Git metadata")
    runtime = bundle / "runtime"
    (runtime / "tmp").mkdir(parents=True, exist_ok=True)
    env = {"TMPDIR": str(runtime / "tmp"), "UV_CACHE_DIR": str(runtime / "uv-cache"),
           "PIP_CACHE_DIR": str(runtime / "pip-cache"), "PATH": os.environ.get("PATH", ""), "HOME": os.environ["HOME"],
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "PYTHONPATH": str(bundle / "source/tests") + ":" + str(bundle / "source")}
    source = bundle / "source"
    install = [python, "-m", "pip", "install", "--no-cache-dir", str(bundle / "core"), "ollama>=0.1.6"]
    if manifest["role"] != "frontend" and not prepared_runtime:
        subprocess.run(install, env=env, check=True, cwd=bundle, timeout=600)
    checks = []
    for advisory, commands in ((True, manifest["advisory_commands"]), (False, manifest["commands"])):
        for argv in commands:
            args = [python if part == "{python}" else part for part in argv]
            if advisory and argv[:3] == ["{python}", "-m", "atelier_core.discipline"]:
                checker = (
                    "import sys,traceback\n"
                    "try:\n"
                    " from atelier_core import discipline\n"
                    " code=discipline.main([sys.argv[1]])\n"
                    "except BaseException:\n"
                    " traceback.print_exc(); code=2\n"
                    "sys.exit(code)\n"
                )
                args = [python, "-I", "-c", checker, str(source / argv[3])]
            result = subprocess.run(args, cwd=source, env=env, timeout=900)
            checks.append({"command": argv, "code": result.returncode, "advisory": advisory})
            # discipline 1 means findings; 2 and failures to execute are infrastructure errors.
            if result.returncode and (not advisory or result.returncode != 1):
                raise RuntimeError("Required check failed or could not execute")
            if result.returncode:
                sys.stderr.write("WARNING: discipline findings are advisory; inspect checker output.\n")
    if not manifest["commands"]:
        raise RuntimeError("No blocking checks are configured")
    return checks

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--prepared-runtime", action="store_true")
    args = parser.parse_args()
    execute(args.bundle, prepared_runtime=args.prepared_runtime)

if __name__ == "__main__":
    main()

