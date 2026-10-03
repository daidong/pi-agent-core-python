"""Validate watcher data against independently obtained release and complete tree evidence.

No network, persistence or baseline acceptance occurs here. The caller owns evidence
acquisition and stores decisions; release notes are never interpreted as instructions.
"""

from __future__ import annotations
import hashlib
import json
from pathlib import Path
from typing import Any
from jsonschema import Draft202012Validator, FormatChecker
from pi_python.errors import CandidateValidationError

SCHEMA = Path(__file__).resolve().parents[1] / "compat/contracts/release-candidate.schema.json"


def candidate_id(candidate: dict[str, Any]) -> str:
    material = "\n".join(
        [
            candidate["repository"],
            candidate["base"]["commit"],
            candidate["target"]["tag"],
            candidate["target"]["commit"],
        ]
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _path(path: str) -> None:
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or any(p in {"..", ".", ""} for p in path.split("/"))
    ):
        raise CandidateValidationError(f"Unsafe path: {path!r}")


def digest(value: bytes | None) -> str | None:
    return None if value is None else hashlib.sha256(value).hexdigest()


def validate_candidate(
    candidate: dict[str, Any],
    *,
    baseline: dict[str, str],
    release: dict[str, Any],
    resolved_commit: str,
    before: dict[str, bytes],
    after: dict[str, bytes],
    seen_ids: set[str] | None = None,
) -> str:
    """Return validated, duplicate or no_op. Never advances accepted baseline.

    before/after must be complete source-tree maps obtained independently of the
    candidate. release is the independently queried official release metadata.
    resolved_commit is the peeled remote tag commit, not target_commitish.
    """
    try:
        Draft202012Validator(
            json.loads(SCHEMA.read_text()), format_checker=FormatChecker()
        ).validate(candidate)
    except Exception as exc:
        raise CandidateValidationError(f"Candidate schema: {exc}") from exc
    if candidate_id(candidate) != candidate["candidate_id"]:
        raise CandidateValidationError("Candidate content digest mismatch")
    if candidate["base"] != baseline:
        raise CandidateValidationError("Stale accepted baseline")
    target = candidate["target"]
    expected_url = candidate["repository"] + "/releases/tag/" + target["tag"]
    if target["release_url"] != expected_url:
        raise CandidateValidationError("Release URL/tag mismatch")
    if resolved_commit != target["commit"]:
        raise CandidateValidationError("Tag moved or commit mismatch")
    if (
        release.get("tag_name") != target["tag"]
        or release.get("draft") is not False
        or release.get("prerelease") is not False
        or release.get("published_at") != target["published_at"]
        or release.get("html_url") != expected_url
    ):
        raise CandidateValidationError("Official release evidence mismatch")
    for path in before.keys() | after.keys():
        _path(path)
    expected = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
    represented: set[str] = set()
    listed: set[str] = set()
    for change in candidate["changed_files"]:
        path = change["path"]
        _path(path)
        if path in listed:
            raise CandidateValidationError("Duplicate changed path")
        listed.add(path)
        kind = change["change"]
        previous = change.get("previous_path", path)
        _path(previous)
        if kind != "renamed" and "previous_path" in change:
            raise CandidateValidationError("previous_path only valid for rename")
        if kind == "renamed":
            if (
                previous == path
                or previous not in before
                or previous in after
                or path in before
                or path not in after
            ):
                raise CandidateValidationError("Invalid rename")
            if previous in listed:
                raise CandidateValidationError("Duplicate renamed path")
            listed.add(previous)
        if kind == "added" and (path in before or path not in after):
            raise CandidateValidationError("Invalid addition")
        if kind == "deleted" and (path not in before or path in after):
            raise CandidateValidationError("Invalid deletion")
        if kind == "modified" and (
            path not in before or path not in after or before[path] == after[path]
        ):
            raise CandidateValidationError("Invalid modification")
        if (
            digest(before.get(previous)) != change["sha256_before"]
            or digest(after.get(path)) != change["sha256_after"]
        ):
            raise CandidateValidationError("Source digest mismatch")
        represented.update({previous, path})
    if represented != expected:
        raise CandidateValidationError("Changed file list is incomplete or contains extra paths")
    if candidate["candidate_id"] in (seen_ids or set()):
        return "duplicate"
    if target["tag"] == baseline["tag"] and target["commit"] == baseline["commit"]:
        if expected:
            raise CandidateValidationError("Identical commit with different source trees")
        return "no_op"
    return "validated"
