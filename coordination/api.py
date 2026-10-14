"""Authenticated API transport; credentials never enter URLs/config/logs."""
from __future__ import annotations
import base64
import json
import netrc
import os
import subprocess
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener
from .gitops import CoordinationError, redact

class ApiError(CoordinationError):
    def __init__(self, status, detail):
        self.status = status
        super().__init__(redact(f"API {status}: {detail}"))

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ApiError(code, "Unexpected API redirect; update the repository registry")

class Api:
    def __init__(self, side, host="forgejo.hurcaguari.top", *, local_auth=False, token_env=None):
        self.side, self.host, self.local_auth = side, host, local_auth
        self.token_env = token_env
        self.base = "https://api.github.com" if side == "github" else f"https://{host}/api/v1"
        self.opener = build_opener(NoRedirect())

    def request(self, method, path, body=None, *, _conflict_retries=2):
        if not path.startswith("/") or "://" in path or "\n" in path:
            raise CoordinationError("API path must be relative")
        if self.local_auth and self.side == "github":
            args = ["gh", "api", "--method", method, path]
            if body is not None:
                args += ["--input", "-"]
            result = subprocess.run(args, input=json.dumps(body) if body is not None else None,
                                    capture_output=True, text=True, timeout=60)
            if result.returncode:
                status = 0
                import re
                if match := re.search(r"HTTP (\d+)", result.stderr):
                    status = int(match[1])
                raise ApiError(status, result.stderr.splitlines()[0])
            return json.loads(result.stdout) if result.stdout.strip() else None
        env = self.token_env or ("GH_SYNC_TOKEN" if self.side == "github" else "FORGEJO_SYNC_TOKEN")
        headers = {"Accept": "application/vnd.github+json" if self.side == "github" else "application/json"}
        if self.local_auth and self.side == "forgejo":
            credential = netrc.netrc().authenticators(self.host)
            if not credential:
                raise CoordinationError("Existing Forgejo credential is unavailable")
            headers["Authorization"] = "Basic " + base64.b64encode(
                f"{credential[0]}:{credential[2]}".encode()).decode()
        else:
            if not (token := os.environ.get(env)):
                raise CoordinationError(f"Missing {env}")
            headers["Authorization"] = ("Bearer " if self.side == "github" else "token ") + token
        if self.side == "github":
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = Request(self.base + path, method=method, headers=headers,
                      data=json.dumps(body).encode() if body is not None else None)
        try:
            with self.opener.open(req, timeout=60) as response:
                data = response.read()
                return json.loads(data) if data else None
        except HTTPError as exc:
            if (exc.code == 409 and _conflict_retries and method == "PUT"
                    and "/contents/" in path and body and body.get("branch")):
                # GitHub can reject concurrent commits to DIFFERENT files on one branch.
                # Retry only when this file's CAS revision is still exactly the expected one.
                try:
                    current = self.request("GET", path + "?ref=" + quote(body["branch"], safe=""))
                    unchanged = current.get("sha") == body.get("sha")
                except ApiError as read_error:
                    unchanged = read_error.status == 404 and not body.get("sha")
                if unchanged:
                    return Api.request(self, method, path, body, _conflict_retries=_conflict_retries - 1)
            # Never log complete request/response bodies; they may contain credentials.
            raise ApiError(exc.code, f"{method} {path.split('?')[0]} failed") from None
        except URLError as exc:
            raise ApiError(0, str(exc.reason)) from None

    def pages(self, path):
        items, page = [], 1
        while True:
            sep = "&" if "?" in path else "?"
            batch = self.request("GET", f"{path}{sep}per_page=100&page={page}")
            if not isinstance(batch, list):
                raise CoordinationError("Expected paginated API list")
            items += batch
            if len(batch) < 100:
                return items
            page += 1

    def prs(self, repository):
        return self.pages(f"/repos/{repository}/pulls?state=all")

    def create_pr(self, repository, head, base, title, body):
        try:
            return self.request("POST", f"/repos/{repository}/pulls",
                                {"head": head, "base": base, "title": title, "body": body})
        except ApiError as exc:
            if exc.status != 422:
                raise
            # Concurrent creation or a write whose response was lost.
            existing = [p for p in self.prs(repository) if p["head"]["ref"] == head
                        and p["base"]["ref"] == base and p["state"] == "open"]
            if len(existing) != 1:
                raise
            return existing[0]

    def auto_merge(self, repository, number, enable=True):
        pr = self.request("GET", f"/repos/{repository}/pulls/{number}")
        query = ("mutation($id:ID!){enablePullRequestAutoMerge(input:{pullRequestId:$id,"
                 "mergeMethod:MERGE}){pullRequest{id}}}" if enable else
                 "mutation($id:ID!){disablePullRequestAutoMerge(input:{pullRequestId:$id}){pullRequest{id}}}")
        result = self.request("POST", "/graphql", {"query": query, "variables": {"id": pr["node_id"]}})
        if result.get("errors"):
            raise CoordinationError("Native auto-merge request rejected")

    def merge_reviewed(self, repository, number, head):
        result = self.request("PUT", f"/repos/{repository}/pulls/{number}/merge",
                              {"sha": head, "merge_method": "merge"})
        if not result or result.get("merged") is not True:
            raise CoordinationError("Reviewed PR merge was not accepted")
        return result["sha"]

