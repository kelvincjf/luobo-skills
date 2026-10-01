#!/usr/bin/env python3
"""Bounded skill inventory and recoverable single-directory activation.

Only stdlib. Evidence files bind an agent's assessment, not semantic truth.
No network, model calls, system configuration edits, or implicit skill roots.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

IGNORED = {".git", "__pycache__", ".DS_Store"}


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, data):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def ordinary(path):
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise ValueError(f"SYMLINK: {path}")
    return path.resolve()


def fingerprint(path):
    path = ordinary(path)
    if not path.exists():
        return None
    if not path.is_dir() or not (path / "SKILL.md").is_file():
        raise ValueError(f"NOT_SKILL_DIRECTORY: {path}")
    files = {}
    for root, dirs, names in os.walk(path, followlinks=False):
        for name in dirs + names:
            p = Path(root) / name
            if name == ".git":
                raise ValueError(f"REPOSITORY_METADATA: use a skill subdirectory, not a Git root: {p}")
            if p.is_symlink():
                raise ValueError(f"SYMLINK: {p}")
        dirs[:] = sorted(x for x in dirs if x not in IGNORED)
        for name in sorted(names):
            if name in IGNORED:
                continue
            p = Path(root) / name
            if not p.is_file():
                raise ValueError(f"NOT_REGULAR_FILE: {p}")
            files[p.relative_to(path).as_posix()] = {
                "sha256": sha(p), "mode": p.stat().st_mode & 0o777
            }
    raw = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    return {"sha256": hashlib.sha256(raw).hexdigest(), "files": files}


def digest(path):
    fp = fingerprint(path)
    return fp["sha256"] if fp else None


def copy_skill(source, target):
    fingerprint(source)  # Reject links before copy; verify the copy below as well.
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(*IGNORED))
    if digest(source) != digest(target):
        raise ValueError("COPY_CHANGED: source changed during snapshot")


def inventory(roots):
    result, errors, groups, seen = [], [], {}, set()
    for root_arg in roots:
        root = Path(root_arg).expanduser().resolve()
        if not root.is_dir():
            errors.append({"path": str(root), "error": "MISSING_DIRECTORY"})
            continue
        def onerror(exc):
            errors.append({"path": str(exc.filename), "error": str(exc)})
        for base, dirs, names in os.walk(root, followlinks=False, onerror=onerror):
            dirs[:] = sorted(x for x in dirs if x not in IGNORED and not x.startswith("."))
            if "SKILL.md" not in names:
                continue
            p = Path(base)
            if str(p) in seen:
                continue
            seen.add(str(p))
            try:
                raw = (p / "SKILL.md").read_text(encoding="utf-8")
                front = raw.split("---", 2)[1] if raw.startswith("---\n") else ""
                def field(key):
                    m = re.search(rf"^{key}:\s*(.+)$", front, re.M)
                    return m.group(1).strip().strip("\"'") if m else None
                fp = digest(p)
                groups.setdefault(fp, []).append(str(p))
                result.append({"path": str(p), "name": field("name"),
                               "description": field("description"), "sha256": fp,
                               "active_entry": "not_checked"})
            except (OSError, ValueError, UnicodeError) as exc:
                errors.append({"path": str(p), "error": str(exc)})
    return {"skills": result, "same_content": [v for v in groups.values() if len(v) > 1],
            "errors": errors, "scope": [str(Path(r).expanduser().resolve()) for r in roots]}


def prepare(candidate, target, run):
    candidate, target, run = map(ordinary, (candidate, target, run))
    for a, b in ((candidate, target), (candidate, run), (target, run)):
        if a == b or a in b.parents or b in a.parents:
            raise ValueError("NESTED_PATHS: candidate, target and run must be separate")
    if not target.parent.is_dir() or not run.parent.is_dir():
        raise ValueError("MISSING_PARENT: create the authorized parent directory first")
    if target.parent.stat().st_dev != run.parent.stat().st_dev:
        raise ValueError("CROSS_FILESYSTEM: run and target must share a filesystem")
    candidate_hash, baseline_hash = digest(candidate), digest(target)
    if not candidate_hash:
        raise ValueError("MISSING_CANDIDATE")
    run.mkdir(mode=0o700)  # An existing run is evidence, never reinitialize it.
    copy_skill(candidate, run / "candidate")
    if baseline_hash:
        copy_skill(target, run / "baseline")
    data = {"schema": 1, "created_at": now(), "status": "prepared",
            "target": str(target), "baseline_sha256": baseline_hash,
            "candidate_sha256": candidate_hash}
    if digest(target) != baseline_hash or digest(run / "candidate") != candidate_hash:
        raise ValueError("SNAPSHOT_DRIFT: preserve this run and prepare a fresh one")
    write_json(run / "transaction.json", data)
    return data


def validate_evidence(run, data):
    proof = read_json(run / "validation.json")
    if proof.get("candidate_sha256") != data["candidate_sha256"]:
        raise ValueError("STALE_VALIDATION: candidate fingerprint differs")
    for key in ("new_case", "old_case") if data["baseline_sha256"] else ("new_case",):
        case = proof.get(key, {})
        if case.get("status") != "passed" or case.get("kind") != "behavior":
            raise ValueError(f"NO_BEHAVIOR_PASS: {key}")
        if not case.get("invocation") or not case.get("runtime"):
            raise ValueError(f"MISSING_INVOCATION: {key}")
        evidence = case.get("evidence", [])
        if not evidence:
            raise ValueError(f"NO_EVIDENCE: {key}")
        for item in evidence:
            raw = run / item["path"]
            if any(part.is_symlink() for part in (raw, *raw.parents) if part == run or run in part.parents):
                raise ValueError(f"EVIDENCE_SYMLINK: {key}")
            p = raw.resolve()
            if run not in p.parents or not p.is_file():
                raise ValueError(f"EVIDENCE_OUTSIDE_RUN: {key}")
            if sha(p) != item["sha256"]:
                raise ValueError(f"EVIDENCE_CHANGED: {key}")
    return proof


@contextmanager
def target_lock(target, run):
    lock = target.parent / (".soul-ring-hunter-" + hashlib.sha256(str(target).encode()).hexdigest()[:12] + ".lock")
    for attempt in range(2):
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            if attempt or lock.is_symlink():
                raise ValueError(f"TARGET_LOCKED: {lock}")
            old = read_json(lock)
            try:
                os.kill(int(old["pid"]), 0)
            except ProcessLookupError:
                lock.unlink()  # Only a demonstrably dead process's lock.
            else:
                raise ValueError(f"TARGET_LOCKED: {lock}")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"pid": os.getpid(), "run": str(run)}, f)
        yield
    finally:
        lock.unlink(missing_ok=True)


def save_status(run, data, status):
    data["status"], data["updated_at"] = status, now()
    write_json(run / "transaction.json", data)


def apply(run):
    run = ordinary(run)
    data = read_json(run / "transaction.json")
    target = ordinary(data["target"])
    with target_lock(target, run):
        if data["status"] == "applied" and digest(target) == data["candidate_sha256"]:
            return {**data, "unchanged": True}
        if data["status"] != "prepared":
            raise ValueError("RECOVERY_REQUIRED: inspect this run and use rollback first")
        if digest(target) != data["baseline_sha256"]:
            raise ValueError("BASELINE_DRIFT: target changed; compare again")
        if digest(run / "candidate") != data["candidate_sha256"]:
            raise ValueError("CANDIDATE_CHANGED: prepare and test the final candidate")
        validate_evidence(run, data)
        copy_skill(run / "candidate", run / "staged")
        save_status(run, data, "applying")  # Persist before the first active-directory write.
        if data["baseline_sha256"]:
            os.rename(target, run / "previous")
            if digest(run / "previous") != data["baseline_sha256"]:
                raise ValueError("BASELINE_DRIFT_DURING_APPLY: keep evidence; inspect before recovery")
        if target.exists() or target.is_symlink():
            raise ValueError("TARGET_APPEARED: refusing replacement; inspect recovery")
        os.rename(run / "staged", target)
        if digest(target) != data["candidate_sha256"]:
            raise ValueError("APPLIED_CONTENT_CHANGED: inspect before recovery")
        save_status(run, data, "applied")
        return data


def rollback(run):
    run = ordinary(run)
    data = read_json(run / "transaction.json")
    target = ordinary(data["target"])
    with target_lock(target, run):
        if data["status"] not in {"prepared", "applying", "applied", "reverting", "reverted"}:
            raise ValueError("UNKNOWN_STATE")
        current, baseline = digest(target), data["baseline_sha256"]
        previous = run / "previous"
        previous_hash = digest(previous)
        if current == baseline and not previous.exists():
            save_status(run, data, "reverted")
            return data
        if current not in {None, data["candidate_sha256"]}:
            raise ValueError("TARGET_DRIFT: preserve later changes; no automatic overwrite")
        if baseline and previous_hash != baseline:
            raise ValueError("BACKUP_DRIFT: original move is missing or changed; no automatic overwrite")
        if not baseline and previous.exists():
            raise ValueError("UNEXPECTED_BACKUP")
        save_status(run, data, "reverting")
        if current:
            archive = run / "reverted-candidate"
            if archive.exists():
                raise ValueError("RECOVERY_ARCHIVE_EXISTS: inspect before proceeding")
            os.rename(target, archive)
        if baseline:
            if target.exists() or target.is_symlink():
                raise ValueError("TARGET_APPEARED")
            os.rename(previous, target)
        if digest(target) != baseline:
            raise ValueError("RESTORE_MISMATCH")
        save_status(run, data, "reverted")
        return data


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    inv = sub.add_parser("inventory")
    inv.add_argument("roots", nargs="+")
    fp = sub.add_parser("fingerprint")
    fp.add_argument("path")
    prep = sub.add_parser("prepare")
    for key in ("candidate", "target", "run"):
        prep.add_argument("--" + key, required=True)
    for action in ("apply", "rollback", "status"):
        sub.add_parser(action).add_argument("--run", required=True)
    args = ap.parse_args()
    try:
        if args.command == "inventory":
            result = inventory(args.roots)
        elif args.command == "fingerprint":
            result = fingerprint(args.path)
        elif args.command == "prepare":
            result = prepare(args.candidate, args.target, args.run)
        elif args.command == "status":
            run = ordinary(args.run)
            result = read_json(run / "transaction.json")
            result["actual_target_sha256"] = digest(result["target"])
            result["actual_candidate_sha256"] = digest(run / "candidate")
        else:
            result = {"apply": apply, "rollback": rollback}[args.command](args.run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
