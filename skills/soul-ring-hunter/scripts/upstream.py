#!/usr/bin/env python3
"""Read public GitHub object metadata; never install or advance adopted baselines."""
import argparse
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request

SHA = re.compile(r"^[0-9a-f]{40}$")
REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+$")
DECISIONS = {"update", "reuse", "create", "reference", "defer", "reject"}
LIMIT = 2 * 1024 * 1024


class UpstreamError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise UpstreamError("GitHub redirected; verify the registered repository")


def github_get(endpoint):
    """No tokens, environment credentials, code downloads, or redirect following."""
    if not endpoint.startswith("/repos/") or "?" in endpoint or "#" in endpoint:
        raise UpstreamError("invalid API endpoint")
    req = urllib.request.Request("https://api.github.com" + endpoint, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "soul-ring-hunter-upstream/1",
        "X-GitHub-Api-Version": "2022-11-28",
    }, method="GET")
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=15) as response:
            raw = response.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise UpstreamError("GitHub response exceeds 2 MiB")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise UpstreamError("unexpected GitHub JSON")
        return data
    except urllib.error.HTTPError as exc:
        raise UpstreamError("GitHub HTTP " + str(exc.code)) from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise UpstreamError("GitHub request failed: " + type(exc).__name__) from exc


def fingerprint(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def timestamp(value):
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise UpstreamError("invalid GitHub object SHA")
    return value


def validate_registry(registry):
    if registry.get("schema") != 1 or not isinstance(registry.get("entries"), list):
        raise ValueError("registry requires schema=1 and entries=[]")
    if len(registry["entries"]) > 64:
        raise ValueError("at most 64 registered entries")
    seen = set()
    for entry in registry["entries"]:
        if not isinstance(entry, dict):
            raise ValueError("each entry must be an object")
        ident = entry.get("id")
        if not isinstance(ident, str) or not ident or ident in seen:
            raise ValueError("entry ids must be nonempty and unique")
        seen.add(ident)
        repo = entry.get("repo")
        if not isinstance(repo, str) or not REPO.fullmatch(repo) or repo.split("/")[1] in {".", ".."}:
            raise ValueError(ident + ": invalid owner/repository")
        ref = entry.get("ref")
        if not isinstance(ref, str) or not ref or len(ref) > 200 or any(ord(c) < 32 for c in ref):
            raise ValueError(ident + ": invalid tracked ref")
        paths = entry.get("paths")
        if not isinstance(paths, list) or not 1 <= len(paths) <= 8 or not all(isinstance(p, str) for p in paths) or len(set(paths)) != len(paths):
            raise ValueError(ident + ": requires 1-8 unique explicit paths")
        for path in paths:
            if not isinstance(path, str) or not path or len(path) > 1024 or "\\" in path:
                raise ValueError(ident + ": invalid monitored path")
            parts = path.split("/")
            if any(p in {"", ".", ".."} for p in parts) or len(parts) > 8 or any(ord(c) < 32 for c in path):
                raise ValueError(ident + ": use relative paths with at most 8 components")
        baseline = entry.get("baseline_commit")
        if baseline is not None and (not isinstance(baseline, str) or not SHA.fullmatch(baseline)):
            raise ValueError(ident + ": baseline_commit must be 40 hex characters or null")
        if entry.get("mode") not in {"adapted", "upstream"} or entry.get("decision") not in DECISIONS:
            raise ValueError(ident + ": invalid mode or absorption decision")
        if not isinstance(entry.get("enabled"), bool):
            raise ValueError(ident + ": enabled must be boolean")
        interval = entry.get("interval_days")
        if isinstance(interval, bool) or not isinstance(interval, int) or interval < 7:
            raise ValueError(ident + ": interval_days must be an integer >= 7")
        if not isinstance(entry.get("targets"), list) or not all(isinstance(p, str) for p in entry["targets"]):
            raise ValueError(ident + ": targets must be labels in a list")
        if "source_evidence" not in entry:
            raise ValueError(ident + ": source_evidence is required")


class GitHubObjects:
    def __init__(self, repo, transport):
        self.base = "/repos/" + repo
        self.transport = transport
        self.cache = {}

    def get(self, suffix):
        if suffix not in self.cache:
            if len(self.cache) >= 40:
                raise UpstreamError("metadata request budget exceeded (40)")
            data = self.transport(self.base + suffix)
            if not isinstance(data, dict):
                raise UpstreamError("unexpected GitHub metadata")
            self.cache[suffix] = data
        return self.cache[suffix]

    def resolve(self, ref):
        data = self.get("/commits/" + urllib.parse.quote(ref, safe=""))
        return sha(data.get("sha")), sha(data.get("commit", {}).get("tree", {}).get("sha"))

    def snapshot(self, commit, paths, root=None):
        if root is None:
            root = sha(self.get("/git/commits/" + sha(commit)).get("tree", {}).get("sha"))
        result = {}
        for path in paths:
            current = root
            obj = None
            parts = PurePosixPath(path).parts
            for i, part in enumerate(parts):
                tree = self.get("/git/trees/" + sha(current))
                if tree.get("truncated") is not False:
                    raise UpstreamError("tree incomplete or truncated")
                items = tree.get("tree")
                if not isinstance(items, list) or len(items) > 10000:
                    raise UpstreamError("invalid or oversized tree")
                matches = [x for x in items if isinstance(x, dict) and x.get("path") == part]
                if len(matches) > 1:
                    raise UpstreamError("duplicate path in GitHub tree")
                if not matches:
                    obj = None
                    break
                obj = matches[0]
                if obj.get("type") not in {"tree", "blob"}:
                    raise UpstreamError("monitored path is not a file or directory")
                sha(obj.get("sha"))
                if i < len(parts) - 1:
                    if obj["type"] != "tree":
                        obj = None
                        break
                    current = obj["sha"]
            result[path] = None if obj is None else {"type": obj["type"], "sha": obj["sha"], "mode": obj.get("mode")}
        return result


def check_registry(registry, previous=None, transport=github_get, now=None, force=False):
    """Return (report, new_state). The caller owns atomic persistence."""
    validate_registry(registry)
    previous = previous or {"schema": 1, "entries": {}}
    if previous.get("schema") != 1 or not isinstance(previous.get("entries"), dict) or not all(isinstance(x, dict) for x in previous["entries"].values()):
        raise ValueError("invalid state; preserve it and inspect before retrying")
    now = now or dt.datetime.now(dt.timezone.utc)
    stamp = timestamp(now)
    state = copy.deepcopy(previous)
    results = []
    for entry in registry["entries"]:
        ident = entry["id"]
        old = state["entries"].get(ident, {})
        identity = fingerprint(entry)
        base = entry.get("baseline_commit")
        result = {"id": ident, "state": "skipped", "checked_at": stamp,
                  "before_commit": base, "after_commit": None, "compare_url": None,
                  "relevant_changed": None, "changed_paths": [], "changes_applied": False,
                  "reference_only": entry["decision"] == "reference", "quiet": True,
                  "new_since_last_check": False, "notification_key": None}
        if not entry["enabled"] or entry["decision"] in {"defer", "reject"}:
            result["reason"] = "disabled" if not entry["enabled"] else entry["decision"]
            results.append(result)
            continue
        same_config = old.get("config_fingerprint") == identity
        last_time = old.get("last_success_at")
        if same_config and last_time and not force:
            try:
                elapsed = now - dt.datetime.fromisoformat(last_time.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                elapsed = dt.timedelta(days=entry["interval_days"])
            if dt.timedelta() <= elapsed < dt.timedelta(days=entry["interval_days"]):
                result.update(state="not_due", last_success_at=last_time)
                results.append(result)
                continue
        saved = copy.deepcopy(old)
        saved.update(last_attempt_at=stamp, last_attempt_identity=identity)
        try:
            github = GitHubObjects(entry["repo"], transport)
            head, root = github.resolve(entry["ref"])
            current = github.snapshot(head, entry["paths"], root)
            baseline = None
            cache = old.get("baseline_snapshot", {}) if same_config else {}
            if base:
                if cache.get("commit") == base and set(cache.get("objects", {})) == set(entry["paths"]):
                    baseline = cache["objects"]
                else:
                    baseline = current if base == head else github.snapshot(base, entry["paths"])
                saved["baseline_snapshot"] = {"commit": base, "objects": baseline}
            else:
                saved.pop("baseline_snapshot", None)
            missing = [p for p, obj in current.items() if obj is None]
            changed = [] if baseline is None else [p for p in entry["paths"] if baseline[p] != current[p]]
            status = ("needs_review" if missing else "baseline_unknown" if baseline is None
                      else "update_available" if changed else "unchanged")
            result.update(state=status, after_commit=head, changed_paths=changed,
                          relevant_changed=None if baseline is None else bool(changed), missing_paths=missing,
                          compare_url=None if not base else "https://github.com/" + entry["repo"] + "/compare/" + base + "..." + head)
            observation = {"commit": head, "objects": current, "state": status,
                           "baseline_commit": base, "observed_at": stamp}
            old_observation = old.get("last_observed", {}) if same_config else {}
            keys = ("objects", "state", "baseline_commit")
            repeated = all(old_observation.get(k) == observation[k] for k in keys)
            result["quiet"] = status == "unchanged" or repeated
            result["new_since_last_check"] = not result["quiet"]
            result["notification_key"] = fingerprint({"identity": identity, "state": status,
                                                       "objects": current, "baseline_commit": base})
            saved.update(config_fingerprint=identity, last_success_at=stamp, last_observed=observation)
            saved.pop("last_error", None)
        except (UpstreamError, KeyError, TypeError, ValueError, TimeoutError, OSError) as exc:
            message = str(exc) if isinstance(exc, UpstreamError) else "metadata failure: " + type(exc).__name__
            repeated = old.get("last_error") == message and old.get("last_attempt_identity") == identity
            result.update(state="error", error=message, quiet=repeated, new_since_last_check=not repeated,
                          notification_key=fingerprint({"identity": identity, "error": message}))
            # Keep the old observation and its identity. Failed checks cannot advance either.
            saved = copy.deepcopy(old)
            saved.update(last_attempt_at=stamp, last_attempt_identity=identity, last_error=message)
        state["entries"][ident] = saved
        results.append(result)
    state["last_run_at"] = stamp
    report = {"schema": 1, "checked_at": stamp, "changes_applied": False, "results": results}
    return report, state


def read_json(path):
    with path.open("rb") as stream:
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError("JSON file exceeds 2 MiB")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("JSON must be an object")
    return data


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check")
    check.add_argument("--registry", required=True, type=Path)
    check.add_argument("--state", required=True, type=Path)
    check.add_argument("--force", action="store_true", help="ignore check interval; never changes adopted baseline")
    args = parser.parse_args()
    try:
        if args.registry.resolve() == args.state.resolve() or args.state.is_symlink():
            raise ValueError("state must be a separate, non-symlink file")
        registry = read_json(args.registry)
        previous = read_json(args.state) if args.state.exists() else None
        report, updated = check_registry(registry, previous, force=args.force)
        atomic_json(args.state, updated)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if any(x["state"] == "error" for x in report["results"]) else 0
    except (ValueError, OSError) as exc:
        print(json.dumps({"state": "error", "changes_applied": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
