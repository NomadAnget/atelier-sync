"""Git object transport. Never merges code or force-pushes remote references."""
from __future__ import annotations
import os
from pathlib import Path
import re
import subprocess
import time

SHA = re.compile(r"[0-9a-f]{40}")
class CoordinationError(RuntimeError):
    pass

def redact(text: str) -> str:
    for name in ("GH_SYNC_TOKEN", "FORGEJO_SYNC_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "FORGEJO_PACKAGE_TOKEN", "TWINE_PASSWORD"):
        if value := os.environ.get(name):
            text = text.replace(value, "[redacted]")
    text = re.sub(r"(https?://)[^/\s]+@", r"\1[redacted]@", text)
    return text

class Git:
    def __init__(self, forgejo_host="forgejo.hurcaguari.top", forgejo_user="Carnation",
                 *, local_auth=False):
        self.host = forgejo_host
        self.user = forgejo_user
        self.local_auth = local_auth

    def run(self, *args, cwd=None, check=True, binary=False, extra_env=None):
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": "/dev/null"}
        # Ignore user url.insteadOf: public integration must actually traverse HTTPS/WAF.
        gh_helper = ("!gh auth git-credential" if self.local_auth else
                     '!f() { echo username=x-access-token; echo "password=$GH_SYNC_TOKEN"; }; f')
        fj_helper = (None if self.local_auth else
                     '!f() { echo username="$FORGEJO_USER"; echo "password=$FORGEJO_SYNC_TOKEN"; }; f')
        env["FORGEJO_USER"] = self.user
        env.update(extra_env or {})
        cred = ["-c", "credential.helper=",
                "-c", f"credential.https://github.com.helper={gh_helper}"]
        if fj_helper:
            cred += ["-c", f"credential.https://{self.host}.helper={fj_helper}"]
        result = subprocess.run(["git", *cred, *args], cwd=cwd, env=env,
                                capture_output=True, text=not binary, timeout=180)
        if check and result.returncode:
            err = result.stderr.decode(errors="replace") if binary else result.stderr
            raise CoordinationError(redact(f"git {args[0]}: {err.strip()}"))
        return result

    def refs(self, url):
        output = self.run("ls-remote", "--symref", url, "HEAD", "refs/heads/*", "refs/tags/*").stdout
        result = {}
        for line in output.splitlines():
            value, name = line.split("\t", 1)
            if value.startswith("ref: "):
                result["HEAD"] = value[5:]
            elif name != "HEAD" and not name.endswith("^{}"):
                require_sha(value)
                result[name] = value
        return result

    def fetch(self, directory: Path, remotes):
        if not (directory / "HEAD").exists():
            directory.mkdir(parents=True, exist_ok=True)
            self.run("init", "--bare", "-q", str(directory))
        for side, url in remotes.items():
            self.run("fetch", "--quiet", "--no-tags", "--prune", url,
                     f"+refs/heads/*:refs/{side}/heads/*",
                     f"+refs/tags/*:refs/{side}/tags/*", cwd=directory)

    def ancestor(self, directory, older, newer):
        require_sha(older)
        require_sha(newer)
        result = self.run("merge-base", "--is-ancestor", older, newer,
                          cwd=directory, check=False)
        if result.returncode not in (0, 1):
            raise CoordinationError(redact("Cannot determine ancestry: " + result.stderr.strip()))
        return result.returncode == 0

    def push(self, directory, url, ref, sha, expected, tries=3):
        require_ref(self, ref)
        require_sha(sha)
        current = self.refs(url).get(ref)
        if current == sha:
            return
        if current != expected:
            raise CoordinationError(f"{ref}: target changed before push")
        if current and ref.startswith("refs/heads/") and not self.ancestor(directory, current, sha):
            raise CoordinationError(f"{ref}: refusing non-fast-forward update")
        if current and ref.startswith("refs/tags/"):
            raise CoordinationError(f"{ref}: refusing to replace a tag")
        for attempt in range(tries):
            result = self.run("push", "--quiet", "--no-verify", url, f"{sha}:{ref}",
                              cwd=directory, check=False)
            # Even a failed HTTP response may follow a successful write.
            after = self.refs(url).get(ref)
            if after == sha:
                return
            if after != expected:
                raise CoordinationError(f"{ref}: target changed during push")
            if not result.returncode:
                raise CoordinationError(f"{ref}: successful push could not be verified")
            transient = any(x in result.stderr for x in
                            ("timed out", "RPC failed", "Connection reset", "HTTP/2 stream", "early EOF"))
            if not transient or attempt == tries - 1:
                raise CoordinationError(redact(result.stderr.strip()))
            time.sleep(2 ** attempt)

    def changed_paths(self, directory, base, head):
        return self.run("diff", "--name-only", base, head, cwd=directory).stdout.splitlines()

def require_sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise CoordinationError("Invalid commit/object SHA")

def require_ref(git, value):
    if not isinstance(value, str) or not value.startswith(("refs/heads/", "refs/tags/")):
        raise CoordinationError("Invalid reference namespace")
    if git.run("check-ref-format", value, check=False).returncode:
        raise CoordinationError("Invalid reference")

