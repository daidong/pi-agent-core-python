"""Read-only candidate receiver: independently fetch release, peeled tag and source trees.

Never executes candidate content or accepts/updates a baseline. Run from the repository
root with `uv run python scripts/check_candidate.py candidate.json`.
"""

import argparse
from io import BytesIO
import json
from pathlib import Path
import sys
import tarfile
from urllib.parse import quote
from urllib.request import urlopen
from jsonschema import Draft202012Validator, FormatChecker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compat.release import SCHEMA, validate_candidate

REPO = "https://api.github.com/repos/earendil-works/pi"


def get_json(url):
    with urlopen(url, timeout=30) as response:
        return json.load(response)


def tree(commit):
    with urlopen(
        f"https://codeload.github.com/earendil-works/pi/tar.gz/{commit}", timeout=60
    ) as response:
        data = response.read()
    result = {}
    with tarfile.open(fileobj=BytesIO(data), mode="r:gz") as archive:
        for member in archive.getmembers():
            path = member.name.partition("/")[2]
            if member.isfile():
                result[path] = archive.extractfile(member).read()
            elif member.issym():
                result[path] = member.linkname.encode()
            elif not member.isdir():
                raise ValueError("Unexpected source archive member")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--baseline", type=Path, default=Path("compat/baseline.json"))
    parser.add_argument("--seen", type=Path, help="Optional JSON array of previously received IDs")
    args = parser.parse_args()
    candidate = json.loads(args.candidate.read_text())
    Draft202012Validator(json.loads(SCHEMA.read_text()), format_checker=FormatChecker()).validate(
        candidate
    )
    stored = json.loads(args.baseline.read_text())["release"]
    baseline = {key: stored[key] for key in ("tag", "commit")}
    if candidate["base"] != baseline:
        raise ValueError("Stale baseline; refusing to fetch unrelated source")
    tag = quote(candidate["target"]["tag"], safe="")
    release = get_json(f"{REPO}/releases/tags/{tag}")
    ref = get_json(f"{REPO}/git/ref/tags/{tag}")["object"]
    for _ in range(8):
        if ref["type"] == "commit":
            break
        if ref["type"] != "tag":
            raise ValueError("Unexpected tag object type")
        ref = get_json(f"{REPO}/git/tags/{ref['sha']}")["object"]
    else:
        raise ValueError("Excessive annotated tag nesting")
    if ref["sha"] != candidate["target"]["commit"]:
        raise ValueError("Tag moved or declared commit is incorrect")
    before = tree(baseline["commit"])
    after = before if ref["sha"] == baseline["commit"] else tree(ref["sha"])
    status = validate_candidate(
        candidate,
        baseline=baseline,
        release=release,
        resolved_commit=ref["sha"],
        before=before,
        after=after,
        seen_ids=set(json.loads(args.seen.read_text())) if args.seen else None,
    )
    print(
        json.dumps(
            {
                "status": status,
                "candidate_id": candidate["candidate_id"],
                "baseline_updated": False,
                "source_files_before": len(before),
                "source_files_after": len(after),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
