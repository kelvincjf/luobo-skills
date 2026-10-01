#!/usr/bin/env python3
"""Explicit, local, version-bound usage records. This is not a background monitor."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from skill_ops import digest, now, ordinary, read_json, sha, write_json


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("TIMEZONE_REQUIRED")
    return result


def initialize(record, skill, expectation, scope, due=None):
    record, skill = ordinary(record), ordinary(skill)
    if record.exists():
        raise ValueError("RECORD_EXISTS: preserve prior history")
    version = digest(skill)
    if not version:
        raise ValueError("MISSING_SKILL")
    if not expectation.strip() or not scope.strip():
        raise ValueError("EXPECTATION_AND_SCOPE_REQUIRED")
    due_time = timestamp(due) if due else datetime.now(timezone.utc) + timedelta(days=7)
    data = {"schema": 1, "created_at": now(), "skill": str(skill), "version": version,
            "expectation": expectation, "observation_scope": scope,
            "observation_connected": False, "scheduler": {"state": "on_next_invocation"},
            "due_at": due_time.isoformat(), "minimum_comparable_runs": 3,
            "uses": [], "reviews": []}
    write_json(record, data)
    return data


def append_use(record, evidence, result, kind, invocation, runtime):
    record, evidence = ordinary(record), ordinary(evidence)
    data = read_json(record)
    if not evidence.is_file() or not invocation.strip() or not runtime.strip():
        raise ValueError("ACTUAL_EVIDENCE_AND_INVOCATION_REQUIRED")
    version = digest(data["skill"])
    if version != data["version"]:
        raise ValueError("VERSION_CHANGED: initialize a new version record")
    item = {"at": now(), "version": version, "result": result, "kind": kind,
            "invocation": invocation, "runtime": runtime,
            "evidence": str(evidence), "sha256": sha(evidence)}
    key = (item["version"], item["sha256"], item["invocation"], kind)
    if any((u["version"], u["sha256"], u["invocation"], u["kind"]) == key for u in data["uses"]):
        return {"unchanged": True, "reason": "same evidence and invocation already recorded"}
    data["uses"].append(item)
    if kind == "actual":
        data["observation_connected"] = True
    write_json(record, data)
    return item


def check(data, at=None):
    at = at or datetime.now(timezone.utc)
    actual = [u for u in data["uses"] if u["kind"] == "actual" and u["version"] == data["version"]]
    broken = [u["evidence"] for u in actual
              if not Path(u["evidence"]).is_file() or sha(u["evidence"]) != u["sha256"]]
    due = at >= timestamp(data["due_at"])
    current = digest(data["skill"])
    if current != data["version"]:
        state = "version_changed"
    elif broken:
        state = "evidence_changed"
    elif any(u["result"] == "fail" for u in actual):
        state = "needs_review"
    elif not due:
        state = "not_due"
    elif not data["observation_connected"]:
        state = "unobserved"
    elif len([u for u in actual if u["result"] in {"pass", "fail"}]) < data["minimum_comparable_runs"]:
        state = "insufficient_data"
    else:
        state = "needs_review"
    return {"checked_at": at.isoformat(), "state": state, "due": due,
            "actual_runs": len(actual), "replay_runs": sum(u["kind"] == "replay" for u in data["uses"]),
            "observation_scope": data["observation_scope"], "scheduler": data["scheduler"],
            "broken_evidence": broken, "automatic_skill_changes": False}


def review(record, conclusion, evidence):
    record, evidence = ordinary(record), ordinary(evidence)
    data = read_json(record)
    result = check(data)
    if not evidence.is_file() or not conclusion.strip():
        raise ValueError("REVIEW_EVIDENCE_REQUIRED")
    item = {"at": now(), "check": result, "conclusion": conclusion,
            "evidence": str(evidence), "sha256": sha(evidence)}
    data["reviews"].append(item)
    write_json(record, data)
    return item


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    for field in ("record", "skill", "expectation", "scope"):
        init.add_argument("--" + field, required=True)
    init.add_argument("--due")
    use = sub.add_parser("record")
    for field in ("record", "evidence", "invocation", "runtime"):
        use.add_argument("--" + field, required=True)
    use.add_argument("--result", choices=["pass", "fail", "unknown"], required=True)
    use.add_argument("--kind", choices=["actual", "replay"], required=True)
    sub.add_parser("check").add_argument("--record", required=True)
    rev = sub.add_parser("review")
    for field in ("record", "conclusion", "evidence"):
        rev.add_argument("--" + field, required=True)
    args = ap.parse_args()
    try:
        if args.command == "init":
            result = initialize(args.record, args.skill, args.expectation, args.scope, args.due)
        elif args.command == "record":
            result = append_use(args.record, args.evidence, args.result, args.kind, args.invocation, args.runtime)
        elif args.command == "check":
            result = check(read_json(args.record))
        else:
            result = review(args.record, args.conclusion, args.evidence)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
