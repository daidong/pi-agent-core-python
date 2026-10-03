"""The classifiers must match the retained output of the pinned upstream functions."""

import json
from pathlib import Path

from pi_python import (
    AssistantMessage,
    is_context_overflow,
    is_recoverable_length,
    is_retryable_error,
)

CASES = json.loads(Path("compat/recovery-cases.json").read_text())
UPSTREAM = json.loads(Path("compat/results/recovery-conformance.upstream.json").read_text())


def classify(case):
    message = AssistantMessage(
        [],
        case["stop_reason"],
        provider=case["provider"],
        error=case.get("error"),
        usage=case.get("usage", {}),
    )
    desired = case.get("desired_max_output")
    return {
        "overflow": is_context_overflow(message, case.get("context_window")),
        "retryable": is_retryable_error(message),
        "recoverable_length": None if desired is None else is_recoverable_length(message, desired),
    }


def test_classifiers_match_upstream():
    assert len(CASES) == len(UPSTREAM)
    mismatches = [
        (case, classify(case), expected)
        for case, expected in zip(CASES, UPSTREAM)
        if classify(case) != expected
    ]
    assert not mismatches
