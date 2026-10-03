"""Install built wheel and run the example/tests offline in disposable Linux containers."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "src/pi_python/_version.py").read_text().split('"')[1]
WHEEL = f"/work/dist/pi_python_core-{VERSION}-py3-none-any.whl"


def verify(version):
    image = f"python:{version}-slim"
    common = ["docker", "run", "--rm", "-v", f"{ROOT}:/work", "-w", "/work"]
    commands = [
        [
            *common,
            image,
            "sh",
            "-ec",
            f"python -m pip download --only-binary=:all: -d /work/.wheelhouse/{version} -r requirements-providers.lock -r requirements-dev.lock",
        ],
        [
            *common,
            "--network",
            "none",
            image,
            "sh",
            "-ec",
            f"""
python -m venv /tmp/verify
/tmp/verify/bin/pip install --no-index --find-links=/work/.wheelhouse/{version} {WHEEL}
/tmp/verify/bin/python -c 'import importlib.util,pi_python; from pi_python.providers import OpenAIProvider; assert importlib.util.find_spec("httpx"); assert importlib.util.find_spec("jwt") is None; print("plain install passed")' 
cd /tmp
/tmp/verify/bin/python -c 'import platform,sys,pi_python; print(platform.platform(),sys.version); print(pi_python.__file__)'
/tmp/verify/bin/python /work/examples/in_memory.py
/tmp/verify/bin/pip check
/tmp/verify/bin/pip install --no-index --find-links=/work/.wheelhouse/{version} -r /work/requirements-providers.lock -r /work/requirements-dev.lock
/tmp/verify/bin/pip install --no-index --find-links=/work/.wheelhouse/{version} '{WHEEL}[providers]'
/tmp/verify/bin/pip check
cd /work
/tmp/verify/bin/python -m pytest -q -o cache_dir=/tmp/pytest-cache
""",
        ],
    ]
    steps = []
    for command in commands:
        started = time.monotonic()
        p = subprocess.run(command, capture_output=True, text=True, timeout=300)
        steps.append(
            {
                "command": command,
                "exit_code": p.returncode,
                "seconds": time.monotonic() - started,
                "stdout": p.stdout,
                "stderr": p.stderr,
            }
        )
        if p.returncode:
            break
    image_info = subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, text=True, check=True
    )
    metadata = json.loads(image_info.stdout)[0]
    runtime_platform = json.loads(
        subprocess.check_output(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                image,
                "python",
                "-c",
                "import json,platform; print(json.dumps({'os':platform.system(),'machine':platform.machine(),'python':platform.python_version()}))",
            ],
            text=True,
        )
    )
    record = {
        "python_series": version,
        "image": image,
        "image_id": metadata["Id"],
        "platform": runtime_platform,
        "image_platform": metadata.get("Os", "") + "/" + metadata.get("Architecture", ""),
        "steps": steps,
        "status": "passed"
        if len(steps) == 2 and all(s["exit_code"] == 0 for s in steps)
        else "failed",
    }
    (ROOT / f"compat/results/linux-{version}.json").write_text(json.dumps(record, indent=2) + "\n")
    print(version, record["status"], flush=True)
    return record


if __name__ == "__main__":
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(verify, ["3.11", "3.12", "3.13", "3.14"]))
    sys.exit(int(any(r["status"] != "passed" for r in records)))
