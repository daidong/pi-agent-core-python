"""Requiredness must use resolved annotations, including postponed inherited hints."""

from __future__ import annotations

from typing import Annotated, NotRequired, Required, TypedDict

import pytest

from pi_python import CancelToken, ToolCall, ToolContext, run_tool_call, tool


class OptionalFields(TypedDict):
    value: NotRequired[int]


class RequiredFields(TypedDict, total=False):
    value: Required[int]


class InheritedFields(OptionalFields, total=False):
    required: Annotated[Required[int], "Required even in a partial TypedDict"]
    extra: int


class RequiredBase(TypedDict):
    base: int


class PartialChild(RequiredBase, total=False):
    optional: Annotated[NotRequired[int], "Optional"]


@pytest.mark.parametrize(
    "annotation, arguments, accepted",
    [
        (OptionalFields, {}, True),
        (RequiredFields, {}, False),
        (RequiredFields, {"value": 1}, True),
        (InheritedFields, {"required": 1}, True),
        (InheritedFields, {}, False),
        (PartialChild, {}, False),
        (PartialChild, {"base": 1}, True),
    ],
)
async def test_postponed_typeddict_arguments(annotation, arguments, accepted):
    seen = []

    async def capture(data):
        seen.append(data)
        return data

    capture.__annotations__ = {"data": annotation}
    wrapped = tool(capture)
    outcome = await run_tool_call(
        wrapped,
        ToolCall("call", "capture", {"data": arguments}),
        ToolContext("run", "call", CancelToken(), None),
    )
    assert (outcome.execution_status == "succeeded") is accepted
    assert seen == ([arguments] if accepted else [])
    if not accepted:
        assert outcome.result.error_code == "invalid_arguments"
