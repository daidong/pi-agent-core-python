"""Fetch only the fixed source; verify tag and every design hash before installing deps."""

import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "compat/contracts/upstream-baseline.json"


def main():
    baseline = json.loads(BASELINE.read_text())
    commit = baseline["release"]["commit"]
    tag = baseline["release"]["tag"]
    tag_data = json.load(
        urlopen(f"https://api.github.com/repos/earendil-works/pi/git/ref/tags/{tag}")
    )
    if tag_data["object"]["type"] != "commit" or tag_data["object"]["sha"] != commit:
        raise RuntimeError("Pinned tag moved or now requires annotated-tag review")
    (ROOT / "reference/tag.json").write_text(json.dumps(tag_data, indent=2) + "\n")
    target = ROOT / "reference/pi"
    if not target.exists():
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "pi.tar.gz"
            archive.write_bytes(
                urlopen(f"https://codeload.github.com/earendil-works/pi/tar.gz/{commit}").read()
            )
            with tarfile.open(archive) as tar:
                for entry in tar.getmembers():
                    if (
                        entry.name.startswith("/")
                        or ".." in Path(entry.name).parts
                        or entry.issym()
                        or entry.islnk()
                    ):
                        raise RuntimeError("Unexpected archive member")
                tar.extractall(tmp, filter="data")
            (Path(tmp) / f"pi-{commit}").rename(target)
    records = []
    extension = json.loads((ROOT / "reference/provider-source-manifest.json").read_text())
    if extension["commit"] != commit:
        raise RuntimeError("Provider reference commit differs from core baseline")
    source_files = {
        item["path"]: item for item in baseline["source_files"] + extension["source_files"]
    }
    for item in source_files.values():
        data = (target / item["path"]).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != item["sha256"] or len(data) != item["bytes"]:
            raise RuntimeError(f"Source mismatch: {item['path']}")
        records.append({"path": item["path"], "sha256": digest, "match": True})
    (ROOT / "compat/results/source-verification.json").write_text(
        json.dumps(records, indent=2) + "\n"
    )
    subprocess.run(["npm", "ci", "--ignore-scripts"], cwd=target, check=True)
    # Generated catalog data is archived with this project for repeatable upstream unit tests.
    data_archive = ROOT / "reference/model-data.tar.gz"
    manifest = json.loads((ROOT / "reference/model-data.json").read_text())
    if hashlib.sha256(data_archive.read_bytes()).hexdigest() != manifest["sha256"]:
        raise RuntimeError("Model-data archive hash mismatch")
    with tarfile.open(data_archive) as tar:
        tar.extractall(target / "packages/ai/src/providers", filter="data")
    for package, command in [("telemetry", "build"), ("ai", "build:offline")]:
        subprocess.run(
            ["npm", "run", command, "--prefix", str(target / "packages" / package)], check=True
        )


if __name__ == "__main__":
    main()
