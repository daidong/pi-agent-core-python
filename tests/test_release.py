import json
from pathlib import Path
import pytest
from compat.release import candidate_id, digest, validate_candidate
from pi_python import CandidateValidationError


def example():
    c = json.loads(
        Path("compat/contracts/release-candidate.example.json").read_text(encoding="utf-8")
    )
    release = {
        "tag_name": c["target"]["tag"],
        "draft": False,
        "prerelease": False,
        "published_at": c["target"]["published_at"],
        "html_url": c["target"]["release_url"],
    }
    return c, {
        "baseline": c["base"],
        "release": release,
        "resolved_commit": c["target"]["commit"],
        "before": {},
        "after": {},
    }


def test_C26_noop_duplicate_and_synthetic_upgrade():
    c, k = example()
    assert validate_candidate(c, **k) == "no_op"
    assert validate_candidate(c, **k, seen_ids={c["candidate_id"]}) == "duplicate"
    c["target"]["tag"] = "synthetic-v2"
    c["target"]["commit"] = "b" * 40
    c["target"]["release_url"] = c["repository"] + "/releases/tag/synthetic-v2"
    c["candidate_id"] = candidate_id(c)
    k["release"].update(tag_name="synthetic-v2", html_url=c["target"]["release_url"])
    k["resolved_commit"] = "b" * 40
    k["before"] = {"a": b"old", "gone": b"delete", "oldname": b"rename"}
    k["after"] = {"a": b"new", "new": b"added", "newname": b"rename"}
    c["changed_files"] = [
        {
            "path": "a",
            "change": "modified",
            "sha256_before": digest(b"old"),
            "sha256_after": digest(b"new"),
        },
        {
            "path": "gone",
            "change": "deleted",
            "sha256_before": digest(b"delete"),
            "sha256_after": None,
        },
        {"path": "new", "change": "added", "sha256_before": None, "sha256_after": digest(b"added")},
        {
            "path": "newname",
            "previous_path": "oldname",
            "change": "renamed",
            "sha256_before": digest(b"rename"),
            "sha256_after": digest(b"rename"),
        },
    ]
    assert validate_candidate(c, **k) == "validated"
    c["changed_files"].pop()
    with pytest.raises(CandidateValidationError, match="incomplete"):
        validate_candidate(c, **k)


@pytest.mark.parametrize(
    "case",
    [
        "version",
        "id",
        "base",
        "tag",
        "draft",
        "digest",
        "duplicate",
        "traversal",
        "missing",
        "timestamp",
    ],
)
def test_C26_rejects_invalid_candidates(case):
    c, k = example()
    k["before"] = {"a": b"old"}
    k["after"] = {"a": b"new"}
    c["changed_files"] = [
        {
            "path": "a",
            "change": "modified",
            "sha256_before": digest(b"old"),
            "sha256_after": digest(b"new"),
        }
    ]
    if case == "version":
        c["schema_version"] = 2
    if case == "id":
        c["candidate_id"] = "f" * 64
    if case == "base":
        k["baseline"] = {"tag": "old", "commit": "c" * 40}
    if case == "tag":
        k["resolved_commit"] = "c" * 40
    if case == "draft":
        k["release"]["draft"] = True
    if case == "digest":
        c["changed_files"][0]["sha256_after"] = "0" * 64
    if case == "duplicate":
        c["changed_files"] *= 2
    if case == "traversal":
        c["changed_files"][0]["path"] = "a/../secret"
    if case == "missing":
        c["changed_files"] = []
    if case == "timestamp":
        c["detected_at"] = "not-a-date"
    with pytest.raises(CandidateValidationError):
        validate_candidate(c, **k)
