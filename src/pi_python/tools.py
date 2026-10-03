from __future__ import annotations
import asyncio
import dataclasses
import datetime
import enum
import inspect
import json
from contextvars import copy_context
from copy import copy, deepcopy
from decimal import Decimal
from functools import partial
from urllib.parse import unquote
from pathlib import PurePath
from uuid import UUID
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Protocol
from jsonschema import (
    Draft4Validator,
    Draft6Validator,
    Draft7Validator,
    Draft201909Validator,
    Draft202012Validator,
    SchemaError,
)
from .cancellation import CancelToken
from .errors import ConfigurationError, SubscriptionError, ToolOutcomeUnknownError
from .messages import (
    ImageContent,
    TextContent,
    ToolCall,
    ToolDeclaration,
    ToolResultMessage,
    _blocks,
    message_to_dict,
    validate_json,
)

# JSON Schema drafts accepted in tool schemas, keyed by $schema without scheme or "#".
# Pydantic emits 2020-12; MCP servers commonly declare draft-07. No $schema means 2020-12.
_DRAFTS: dict[str, Any] = {
    "json-schema.org/draft-04/schema": Draft4Validator,
    "json-schema.org/draft-06/schema": Draft6Validator,
    "json-schema.org/draft-07/schema": Draft7Validator,
    "json-schema.org/draft/2019-09/schema": Draft201909Validator,
    "json-schema.org/draft/2020-12/schema": Draft202012Validator,
    "json-schema.org/schema": Draft202012Validator,  # "the latest draft"
}
# Where subschemas live; other keywords hold data or vendor extensions and are not followed.
_SUBSCHEMA = {
    "items",
    "additionalItems",
    "contains",
    "additionalProperties",
    "propertyNames",
    "unevaluatedItems",
    "unevaluatedProperties",
    "not",
    "if",
    "then",
    "else",
    "allOf",
    "anyOf",
    "oneOf",
    "prefixItems",
}
_SCHEMA_MAPS = {
    "properties",
    "patternProperties",
    "$defs",
    "definitions",
    "dependentSchemas",
    "dependencies",
}


def schema_validator(schema: dict[str, Any]) -> Any:
    """A validator for the schema's draft that also checks `format`, like Pi's validator.

    Formats are checked when jsonschema can check them; some (for example `uri`)
    need jsonschema's optional format dependencies.
    """
    uri = schema.get("$schema") if isinstance(schema, dict) else None
    cls: Any = Draft202012Validator
    if uri is not None:
        cls = _DRAFTS.get(str(uri).split("://", 1)[-1].rstrip("#"))
        if cls is None:
            raise ConfigurationError(f"Unsupported $schema: {uri}")
    return cls(schema, format_checker=cls.FORMAT_CHECKER)


def _without_empty_required(node: Any) -> Any:
    """Draft-04 forbids `required: []`, which many generators emit; it means nothing."""
    if isinstance(node, list):
        return [_without_empty_required(item) for item in node]
    if not isinstance(node, dict):
        return node
    return {
        k: _without_empty_required(v) for k, v in node.items() if not (k == "required" and v == [])
    }


def _resolve(schema: dict[str, Any], ref: str) -> None:
    """Check that a local reference points somewhere: "#", "#/json/pointer" or "#anchor"."""
    fragment = unquote(ref[1:])
    if fragment and not fragment.startswith("/"):  # a plain-name anchor
        found = []

        def find(node: Any) -> None:
            if isinstance(node, dict):
                if node.get("$anchor") == fragment or node.get("$id") == f"#{fragment}":
                    found.append(node)
                for value in node.values():
                    find(value)
            elif isinstance(node, list):
                for value in node:
                    find(value)

        find(schema)
        if not found:
            raise ConfigurationError(f"Unresolved local schema reference: {ref}")
        return
    target: Any = schema
    try:
        for part in fragment.split("/")[1:] if fragment else []:
            part = part.replace("~1", "/").replace("~0", "~")
            target = target[int(part)] if isinstance(target, list) else target[part]
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise ConfigurationError(f"Unresolved local schema reference: {ref}") from exc


# Counted without recursion before anything recursive walks the schema. Relying on
# RecursionError alone gave interpreter-dependent limits, and PyPy can crash first.
_MAX_SCHEMA_DEPTH = 100


def _deeper_than(value: Any, limit: int) -> bool:
    """Whether objects and arrays nest more than `limit` levels; also ends on cycles."""
    stack = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            children: Any = node.values()
        elif isinstance(node, list):
            children = node
        else:
            continue
        if depth > limit:
            return True
        stack.extend((child, depth + 1) for child in children)
    return False


def validate_schema(schema: dict[str, Any]) -> None:
    """Accept standard JSON Schema; reject what cannot be checked without leaving the schema."""
    if _deeper_than(schema, _MAX_SCHEMA_DEPTH):
        raise ConfigurationError(
            f"Tool schema is nested too deeply (more than {_MAX_SCHEMA_DEPTH} levels)"
        )
    try:
        validate_json(schema)
    except RecursionError as exc:
        raise ConfigurationError("Tool schema is nested too deeply") from exc
    if not isinstance(schema, dict):
        raise ConfigurationError("Tool schema must be an object")
    validator = schema_validator(schema)
    try:
        if isinstance(validator, Draft4Validator):
            validator.check_schema(_without_empty_required(schema))
        else:
            validator.check_schema(schema)
    except SchemaError as exc:
        raise ConfigurationError(exc.message) from exc
    except RecursionError as exc:
        raise ConfigurationError("Tool schema is nested too deeply") from exc

    # Only local references, resolved at registration: never a network or file fetch.
    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if key in {"$ref", "$dynamicRef", "$recursiveRef"}:
                if not isinstance(value, str) or not value.startswith("#"):
                    raise ConfigurationError("Only local schema references are supported")
                _resolve(schema, value)
            elif key in _SCHEMA_MAPS and isinstance(value, dict):
                for sub in value.values():
                    visit(sub)
            elif key in _SUBSCHEMA:
                visit(value)

    try:
        visit(schema)
    except RecursionError as exc:
        raise ConfigurationError("Tool schema is nested too deeply") from exc


@dataclass
class ToolResult:
    content: list[TextContent | ImageContent]
    details: Any = None
    structured_content: Any = None
    is_error: bool = False
    terminate: bool = False
    error_code: str | None = None
    usage: dict[str, Any] | None = None
    nested_calls: dict[str, Any] | None = None

    @classmethod
    def text(cls, text: str, **kwargs: Any) -> ToolResult:
        return cls([TextContent(text)], **kwargs)


def error_result(code: str, text: str) -> ToolResult:
    return ToolResult.text(text, is_error=True, error_code=code)


def aborted_result() -> ToolResult:
    """Pi's result for a call stopped by abort; the text matches upstream."""
    return error_result("aborted", "Operation aborted")


def _plain(value: Any) -> Any:
    """Common Python values as JSON values: dates, UUIDs, paths, enums, tuples, sets,
    dataclasses and pydantic models, also nested inside dicts and lists."""
    if isinstance(value, enum.Enum):
        return _plain(value.value)
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (UUID, PurePath, Decimal)):
        return str(value)
    if hasattr(value, "model_dump") and not isinstance(value, type):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _plain(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    return value


def as_tool_result(value: Any) -> Any:
    """Accept what a plain Python function naturally returns.

    A string becomes text and None empty content. Other values become their JSON text and
    the structured result; dates, UUIDs, enums, tuples, dataclasses and pydantic models are
    converted first. Anything else is left for check_result to reject.
    """
    if isinstance(value, ToolResult):
        return value
    if value is None:
        return ToolResult([])
    value = _plain(value)
    if isinstance(value, str):
        return ToolResult.text(value)
    if isinstance(value, (dict, list, int, float)):
        validate_json(value)
        return ToolResult.text(json.dumps(value, ensure_ascii=False), structured_content=value)
    return value


async def call_tool_function(function: Callable, *args: Any) -> Any:
    """Await an async tool; run a sync one in a worker thread, off the event loop."""
    if inspect.iscoroutinefunction(function) or inspect.iscoroutinefunction(
        getattr(function, "__call__", None)
    ):
        return await function(*args)
    future = asyncio.get_running_loop().run_in_executor(
        None, partial(copy_context().run, function, *args)
    )
    # A thread cannot be interrupted. On cancellation keep waiting: if it returns, its
    # real result stands, as when a Pi tool ignores the abort signal; while it runs,
    # cleanup sees the work as unfinished.
    swallowed = 0
    while True:
        try:
            value = await asyncio.shield(future)
            break
        except asyncio.CancelledError:
            if future.cancelled():
                raise
            swallowed += 1
    task = asyncio.current_task()
    for _ in range(swallowed if task is not None else 0):
        task.uncancel()  # type: ignore[union-attr]
    return await value if inspect.isawaitable(value) else value


def check_result(result: ToolResult, schema: dict[str, Any] | None = None) -> None:
    if not isinstance(result, ToolResult):
        raise TypeError("Tool must return ToolResult")
    validate_json(asdict(result))
    _blocks([asdict(b) for b in result.content], images=True)
    message_to_dict(
        ToolResultMessage(
            "validation",
            "validation",
            result.content,
            usage=result.usage,
            nested_calls=result.nested_calls,
        )
    )
    if type(result.is_error) is not bool or type(result.terminate) is not bool:
        raise TypeError("Tool result flags must be boolean")
    if schema is not None and not result.is_error:
        schema_validator(schema).validate(result.structured_content)


@dataclass
class ToolResultUpdate:
    content: list[TextContent | ImageContent] | None = None
    details: Any = None
    structured_content: Any = None
    is_error: bool | None = None
    terminate: bool | None = None
    usage: dict[str, Any] | None = None
    nested_calls: dict[str, Any] | None = None


@dataclass
class ToolContext:
    run_id: str
    call_id: str
    cancel: CancelToken
    _emit: Callable = field(repr=False)
    assistant_message: Any = None
    agent_context: Any = None
    tool_call: ToolCall | None = None
    args: dict[str, Any] | None = None
    result: ToolResult | None = None
    is_error: bool = False

    async def emit_update(self, value: Any) -> None:
        validate_json(value)
        await self._emit(value)


class ToolExecutor(Protocol):
    async def __call__(self, args: dict[str, Any], context: ToolContext) -> ToolResult: ...


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    execute: ToolExecutor
    output_schema: dict[str, Any] | None = None
    execution_mode: str = "parallel"
    prepare_arguments: Callable | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name or not isinstance(self.description, str):
            raise ConfigurationError("Invalid tool name or description")
        if self.execution_mode not in {"parallel", "sequential"}:
            raise ConfigurationError("Invalid tool execution mode")
        if not callable(self.execute):
            raise ConfigurationError("Tool execute must be callable")
        validate_schema(self.input_schema)
        if self.output_schema is not None:
            validate_schema(self.output_schema)

    def __deepcopy__(self, memo: dict[int, Any]) -> Tool:
        # Executable callables can own clients, locks or other application resources.
        # Copy declarations, never clone the callable's owner or its connections.
        result = copy(self)
        memo[id(self)] = result
        result.input_schema = deepcopy(self.input_schema, memo)
        result.output_schema = deepcopy(self.output_schema, memo)
        return result

    def declaration(self) -> ToolDeclaration:
        return ToolDeclaration(self.name, self.description, deepcopy(self.input_schema))


@dataclass
class ToolOutcome:
    call: ToolCall
    execution_status: str = "not_started"
    original_arguments: dict[str, Any] = field(default_factory=dict)
    prepared_arguments: dict[str, Any] | None = None
    raw_result: ToolResult | None = None
    result: ToolResult | None = None
    error: str | None = None
    _settled: bool = field(default=False, repr=False)


async def invoke(function: Callable | None, *args: Any) -> Any:
    if function is None:
        return None
    value = function(*args)
    return await value if inspect.isawaitable(value) else value


async def prepare_tool_call(
    tool: Tool | None, outcome: ToolOutcome, context: ToolContext, before: Callable | None = None
) -> None:
    if tool is None:
        outcome.result = error_result("unknown_tool", f"Unknown tool: {outcome.call.name}")
        return
    try:
        args = deepcopy(outcome.original_arguments)
        if tool.prepare_arguments:
            args = await invoke(tool.prepare_arguments, args)
        validate_json(args)
        if not isinstance(args, dict):
            raise ValueError("Tool arguments must be an object")
        outcome.prepared_arguments = deepcopy(args)
        # Name each problem by its path; the full schema would only bloat the model's context.
        problems = [
            f"{error.json_path}: {error.message}"
            for error in schema_validator(tool.input_schema).iter_errors(args)
        ]
        if problems:
            more = f" (and {len(problems) - 5} more)" if len(problems) > 5 else ""
            raise ValueError("; ".join(problems[:5]) + more)
    except Exception as exc:
        outcome.result = error_result("invalid_arguments", f"{type(exc).__name__}: {exc}")
        return
    context.args = deepcopy(args)
    context.tool_call = deepcopy(outcome.call)
    try:
        decision = await invoke(before, deepcopy(outcome.call), deepcopy(args), context)
    except SubscriptionError:
        raise
    except Exception as exc:
        # As in Pi, a failing preflight hook fails this call, not the whole run.
        outcome.error = f"{type(exc).__name__}: {exc}"
        outcome.result = error_result("hook_error", outcome.error)
        return
    if decision is False:
        outcome.result = error_result("blocked", "Tool call blocked by before_tool_call")
    elif isinstance(decision, ToolResult):
        check_result(decision)
        outcome.result = deepcopy(decision)
    elif decision is not None and decision is not True:
        raise ConfigurationError("before_tool_call must return bool, ToolResult or None")


async def run_tool_call(
    tool: Tool | None,
    call: ToolCall,
    context: ToolContext,
    *,
    before_tool_call: Callable | None = None,
    after_tool_call: Callable | None = None,
    outcome: ToolOutcome | None = None,
    prepared: bool = False,
) -> ToolOutcome:
    """Shared programmatic/agent path. Caller cancellation is never swallowed."""
    outcome = outcome or ToolOutcome(deepcopy(call), original_arguments=deepcopy(call.arguments))
    if not prepared:
        await prepare_tool_call(tool, outcome, context, before_tool_call)
    if outcome.result is not None:
        return outcome
    assert tool is not None
    context.cancel.raise_if_cancelled()
    outcome.execution_status = "running"
    try:
        assert outcome.prepared_arguments is not None
        value = await call_tool_function(
            tool.execute, deepcopy(outcome.prepared_arguments), context
        )
        if outcome._settled:
            return outcome
        # Execution succeeded even if conversion, serializability or output validation fails.
        outcome.execution_status = "succeeded"
        try:
            outcome.raw_result = deepcopy(as_tool_result(value))
        except Exception as exc:
            outcome.error = f"{type(exc).__name__}: {exc}"
            outcome.result = error_result("finalization_error", outcome.error)
            return outcome
    except asyncio.CancelledError:
        if outcome._settled:
            raise
        # Pi's abort: the tool saw the signal and stopped; record an ordinary error result.
        outcome.execution_status = "cancelled"
        outcome.error = "Operation aborted"
        outcome.result = aborted_result()
        raise
    except ToolOutcomeUnknownError as exc:
        if outcome._settled:
            return outcome
        outcome.execution_status = "unknown"
        outcome.error = str(exc)
        outcome.result = error_result("outcome_unknown", str(exc))
        return outcome
    except SubscriptionError:
        raise
    except Exception as exc:
        if outcome._settled:
            return outcome
        outcome.execution_status = "failed"
        outcome.error = f"{type(exc).__name__}: {exc}"
        outcome.result = error_result("tool_error", outcome.error)
        outcome.raw_result = deepcopy(outcome.result)
    try:
        result = deepcopy(outcome.raw_result)
        check_result(result, tool.output_schema)
        context.args = deepcopy(outcome.prepared_arguments)
        context.tool_call = deepcopy(call)
        context.result = deepcopy(result)
        context.is_error = result.is_error
        replacement = await invoke(after_tool_call, deepcopy(call), deepcopy(result), context)
        if outcome._settled:
            return outcome
        if isinstance(replacement, ToolResultUpdate):
            if replacement.content is not None:
                result.content = replacement.content
                result.structured_content = replacement.structured_content
            elif replacement.structured_content is not None:
                result.structured_content = replacement.structured_content
            if replacement.details is not None:
                result.details = replacement.details
            if replacement.is_error is not None:
                result.is_error = replacement.is_error
            for metadata_key in ("usage", "nested_calls"):
                if getattr(replacement, metadata_key) is not None:
                    setattr(result, metadata_key, deepcopy(getattr(replacement, metadata_key)))
            if replacement.terminate is not None:
                result.terminate = replacement.terminate
        elif replacement is not None:
            if not isinstance(replacement, ToolResult):
                raise TypeError("after_tool_call must return ToolResult, ToolResultUpdate or None")
            result = replacement
        check_result(result, tool.output_schema)
        outcome.result = deepcopy(result)
    except asyncio.CancelledError:
        if outcome._settled:
            raise
        outcome.result = error_result(
            "finalization_cancelled", "Execution finished; result finalization cancelled"
        )
        raise
    except SubscriptionError:
        raise
    except Exception as exc:
        if not outcome._settled:
            outcome.error = f"{type(exc).__name__}: {exc}"
            outcome.result = error_result("finalization_error", outcome.error)
    return outcome
