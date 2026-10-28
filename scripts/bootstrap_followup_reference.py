"""Install the separately pinned v1.4 provider/plugin reference, never move core's pin."""

import base64
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "reference/followup/source-manifest.json"


def verify_sources():
    manifest = json.loads(MANIFEST.read_text())
    target = ROOT / "reference/pi-followup"
    for item in manifest["source_files"]:
        if hashlib.sha256((target / item["path"]).read_bytes()).hexdigest() != item["sha256"]:
            raise RuntimeError(f"Followup source mismatch: {item['path']}")
    return manifest


def main():
    manifest = json.loads(MANIFEST.read_text())
    target = ROOT / "reference/pi-followup"
    if not target.exists():
        archive = urlopen(manifest["source_url"], timeout=60).read()
        if hashlib.sha256(archive).hexdigest() != manifest["source_sha256"]:
            raise RuntimeError("Upstream source archive mismatch")
        with tempfile.TemporaryDirectory(dir=ROOT / "reference") as tmp:
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                for member in tar.getmembers():
                    if member.issym() or member.islnk():
                        raise RuntimeError("Unexpected source archive link")
                tar.extractall(tmp, filter="data")
            (Path(tmp) / f"pi-{manifest['commit']}").rename(target)
    data = urlopen(manifest["model_data_url"], timeout=60).read()
    if base64.b64encode(hashlib.sha512(data).digest()).decode() != manifest["model_data_sha512"]:
        raise RuntimeError("npm model-data integrity mismatch")
    data_dir = target / "packages/ai/src/providers/data"
    data_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for member in tar.getmembers():
            if member.isfile() and member.name.startswith("package/dist/providers/data/"):
                path = Path(member.name)
                if (
                    path.suffix == ".json"
                    and path.parent.as_posix() == "package/dist/providers/data"
                ):
                    stream = tar.extractfile(member)
                    assert stream is not None
                    (data_dir / path.name).write_bytes(stream.read())
    verify_sources()
    subprocess.run(["npm", "ci", "--ignore-scripts"], cwd=target, check=True)
    subprocess.run(
        ["npm", "run", "build", "--prefix", str(target / "packages/telemetry")], check=True
    )


if __name__ == "__main__":
    main()
