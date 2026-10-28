"""Local baseline for evaluating Pi's main-only incremental JSON parser followup.

Measures our preview parser only; excludes transport, event copies, UI and final
strict validation. This is not a comparison with upstream or an end-to-end claim.
"""

import hashlib
import json
from pathlib import Path
import platform
import statistics
import time

from pi_python.stream import partial_json

ROOT = Path(__file__).resolve().parents[1]


def main():
    rows = []
    for size in (4096, 16384, 65536):
        text = json.dumps({"text": "x" * size}, separators=(",", ":"))
        samples = []
        for _ in range(3):
            start = time.perf_counter()
            for end in range(256, len(text), 256):
                partial_json(text[:end])
            assert partial_json(text) == {"text": "x" * size}
            samples.append((time.perf_counter() - start) * 1000)
        rows.append(
            {
                "text_chars": size,
                "chunk_chars": 256,
                "samples_ms": samples,
                "median_ms": statistics.median(samples),
            }
        )
    report = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "source_sha256": hashlib.sha256(
            (ROOT / "src/pi_python/stream.py").read_bytes()
        ).hexdigest(),
        "upstream_candidate": "f715a15c101ca8a44d759bceeb7ec2a51024c81c",
        "scope": "单个字符串参数，256 字符分片，纯 Python 预览解析；不含传输和事件复制；未比较上游耗时",
        "results": rows,
    }
    (ROOT / "compat/results/partial-json-baseline.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
