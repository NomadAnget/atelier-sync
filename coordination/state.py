"""Versioned task ledger; remote persistence uses GitHub's file-SHA compare-and-swap."""
from __future__ import annotations
import base64
from copy import deepcopy
import json
import fcntl
import hashlib
from pathlib import Path
from urllib.parse import quote
from .api import ApiError
from .gitops import CoordinationError

def empty():
    return {"version": 1, "tasks": {}, "github_tasks": {}, "mirror": {}, "pointer_tasks": {}}

def validate(data):
    if not isinstance(data, dict) or data.get("version") != 1:
        raise CoordinationError("Unsupported or damaged state record")
    for key in ("tasks", "github_tasks", "mirror", "pointer_tasks"):
        if not isinstance(data.get(key), dict):
            raise CoordinationError(f"Missing state section: {key}")
    return data

class MemoryStore:
    def __init__(self):
        self.values = {}
    def load(self, key):
        version, data = self.values.get(key, (0, empty()))
        return deepcopy(validate(data)), version
    def save(self, key, data, expected):
        version = self.values.get(key, (0, None))[0]
        if version != expected:
            raise CoordinationError("State version conflict")
        self.values[key] = (version + 1, deepcopy(validate(data)))
        return version + 1

class FileStore(MemoryStore):
    """Local audit/test state; live workflows use RemoteStore instead."""
    def __init__(self, directory):
        super().__init__()
        self.directory = Path(directory)
    def load(self, key):
        path = self.directory / (key + ".json")
        if path.exists():
            item = json.loads(path.read_text())
            self.values[key] = (item["revision"], item["data"])
        return super().load(key)
    def save(self, key, data, expected):
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / (key + ".lock")).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.load(key)
            revision = super().save(key, data, expected)
            path = self.directory / (key + ".json")
            temporary = path.with_suffix(".pending")
            temporary.write_text(json.dumps({"revision": revision, "data": data}, sort_keys=True) + "\n")
            temporary.replace(path)
            return revision

class RemoteStore:
    def __init__(self, api, registry):
        self.api, self.registry = api, registry
        meta = api.request("GET", f"/repos/{registry.state_repository}")
        if not meta.get("private") or int(meta["id"]) != registry.state_repository_id:
            raise CoordinationError("State repository must be the registered private repository")
        if registry.state_branch == meta["default_branch"]:
            raise CoordinationError("State cannot share the code default branch")
        # Missing state branch is a migration prerequisite, not an implicit bootstrap.
        api.request("GET", f"/repos/{registry.state_repository}/git/ref/heads/{registry.state_branch}")

    def path(self, key):
        return f"/repos/{self.registry.state_repository}/contents/state/{key}.json"

    def load(self, key):
        try:
            item = self.api.request("GET", self.path(key) + "?ref=" + quote(self.registry.state_branch))
        except ApiError as exc:
            if exc.status == 404:
                return empty(), None
            raise
        return validate(json.loads(base64.b64decode(item["content"]))), item["sha"]

    def save(self, key, data, expected):
        encoded = (json.dumps(validate(data), sort_keys=True) + "\n").encode()
        blob = hashlib.sha1(b"blob " + str(len(encoded)).encode() + b"\0" + encoded).hexdigest()
        if expected == blob:
            # Still compare the live revision: a no-op must not hide a concurrent writer.
            current = self.api.request("GET", self.path(key) + "?ref=" + quote(self.registry.state_branch))
            if current["sha"] != expected:
                raise CoordinationError("State version conflict; re-read and reconcile")
            return expected
        body = {"message": f"state: reconcile {key}", "branch": self.registry.state_branch,
                "content": base64.b64encode(encoded).decode()}
        if expected:
            body["sha"] = expected
        try:
            result = self.api.request("PUT", self.path(key), body)
        except ApiError as exc:
            if exc.status in (409, 422):
                raise CoordinationError("State version conflict; re-read and reconcile") from None
            raise
        return result["content"]["sha"]

