"""Build a Tool from a typed Python function: the schema comes from the signature and
the description from the docstring, so a tool is written like any other function."""

from __future__ import annotations

import dataclasses
import enum
import functools
import inspect
import re
import types
import typing
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, Union
from uuid import UUID

from .errors import ConfigurationError
from .messages import validate_json
from .tools import Tool, ToolContext, schema_validator

_PRIMITIVES: dict[Any, dict[str, Any]] = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
    type(None): {"type": "null"},
    datetime: {"type": "string", "format": "date-time"},
    date: {"type": "string", "format": "date"},
    UUID: {"type": "string", "format": "uuid"},
    Path: {"type": "string"},
}
_PARAM_SECTIONS = {
    "args",
    "arguments",
    "parameters",
    "params",
    "keyword args",
    "keyword arguments",
    "other parameters",
}
_SECTION = re.compile(
    r"^(args|arguments|parameters|params|keyword args|keyword arguments|other parameters"
    r"|returns?|raises|yields|examples?|notes?|see also|warnings?|references)\s*:?\s*$",
    re.I,
)
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")  # what model APIs accept
# TypedDict and dataclass field wrappers that carry no schema of their own.
_WRAPPERS = {typing.Annotated, typing.Required, typing.NotRequired}
if hasattr(typing, "ReadOnly"):  # Python 3.13+
    _WRAPPERS.add(typing.ReadOnly)


@dataclasses.dataclass
class FunctionTool(Tool):
    """A Tool made by `@tool`; calling it calls the original function, as in unit tests."""

    function: Callable | None = dataclasses.field(default=None, repr=False, compare=False)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        assert self.function is not None
        return self.function(*args, **kwargs)

    @property
    def __wrapped__(self) -> Callable | None:
        return self.function


def tool(
    function: Callable | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    execution_mode: str = "parallel",
    output_schema: dict[str, Any] | None = None,
) -> Any:
    """Turn a function into a Tool. Use as ``@tool`` or ``@tool(name=...)``.

    Parameters become the input schema: str, int, float, bool, None, list, tuple, set,
    dict, Literal, Enum, Optional/Union, Annotated[T, "description"], TypedDict,
    dataclasses, datetime, date, UUID, Path and pydantic models. A parameter annotated
    ``ToolContext`` receives the call context instead. Google, NumPy or Sphinx docstrings
    describe the parameters. Sync functions run in a worker thread. The function may
    return a ToolResult, a string, a JSON value, None, or a date, dataclass or pydantic
    model. The resulting tool can still be called like the function itself.
    """

    def build(target: Callable) -> Tool:
        return _build(target, name, description, execution_mode, output_schema)

    return build(function) if function is not None else build


def _target(function: Callable) -> Any:
    """The plain function behind a partial or a callable object, for hints and docs."""
    target: Any = function
    while isinstance(target, functools.partial):
        target = target.func
    if not (inspect.isfunction(target) or inspect.ismethod(target)) and hasattr(target, "__call__"):
        target = target.__call__
    return target


def _build(
    function: Callable,
    name: str | None,
    description: str | None,
    execution_mode: str,
    output_schema: dict[str, Any] | None,
) -> Tool:
    target = _target(function)
    tool_name = name or getattr(function, "__name__", None) or getattr(target, "__name__", None)
    if not tool_name or not _TOOL_NAME.match(tool_name):
        raise ConfigurationError(
            f"Tool name {tool_name!r} must be 1-64 letters, digits, '_' or '-'; pass @tool(name=...)"
        )
    try:
        hints = typing.get_type_hints(target, include_extras=True)
    except Exception as exc:
        raise ConfigurationError(f"Cannot resolve type hints of {tool_name}: {exc}") from exc
    doc = inspect.getdoc(target) or inspect.getdoc(function) or ""
    summary, documented = _parse_docstring(doc)
    schemas = _Schemas()
    properties: dict[str, Any] = {}
    required: list[str] = []
    converters: dict[str, _Argument] = {}
    context_name = None
    parameters = list(inspect.signature(function).parameters.values())
    if parameters and parameters[0].name in {"self", "cls"}:
        raise ConfigurationError(
            f"{tool_name}: decorate a bound method instead, for example tool(instance.{tool_name})"
        )
    for parameter in parameters:
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            raise ConfigurationError(f"{tool_name}: *args and **kwargs cannot be tool parameters")
        if parameter.kind is parameter.POSITIONAL_ONLY:
            raise ConfigurationError(f"{tool_name}: positional-only parameters are not supported")
        annotation = hints.get(parameter.name, Any)
        if _is_context(annotation):
            context_name = parameter.name
            continue
        plan = schemas.compile(annotation, parameter.name)
        schema = plan.schema
        if parameter.name in documented and "description" not in schema:
            schema["description"] = documented[parameter.name]
        if parameter.default is parameter.empty:
            required.append(parameter.name)
        else:
            default = _jsonable(parameter.default)
            if default is not _MISSING:
                schema["default"] = default
        properties[parameter.name] = schema
        converters[parameter.name] = plan
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
    if schemas.definitions:
        input_schema["$defs"] = schemas.definitions

    schemas.finish()

    def arguments(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        values = {key: converters[key](value) for key, value in args.items()}
        if context_name is not None:
            values[context_name] = context
        return values

    if inspect.iscoroutinefunction(function) or inspect.iscoroutinefunction(target):

        async def execute(args: dict[str, Any], context: ToolContext) -> Any:
            return await function(**arguments(args, context))

    else:

        def execute(args: dict[str, Any], context: ToolContext) -> Any:  # type: ignore[misc]
            return function(**arguments(args, context))

    return FunctionTool(
        tool_name,
        description if description is not None else summary,
        input_schema,
        execute,
        output_schema=output_schema,
        execution_mode=execution_mode,
        function=function,
    )


_MISSING = object()


def _jsonable(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        value = value.value
    try:
        validate_json(value)
    except Exception:
        return _MISSING
    return value


def _strip(annotation: Any) -> Any:
    """Remove Annotated, Required, NotRequired and ReadOnly wrappers."""
    while typing.get_origin(annotation) in _WRAPPERS:
        annotation = typing.get_args(annotation)[0]
    return annotation


def _is_context(annotation: Any) -> bool:
    annotation = _strip(annotation)
    if annotation is ToolContext:
        return True
    if typing.get_origin(annotation) in (Union, types.UnionType):
        return ToolContext in typing.get_args(annotation)
    return False


def _is_pydantic(annotation: Any) -> bool:
    return isinstance(annotation, type) and hasattr(annotation, "model_json_schema")


def _is_typeddict(annotation: Any) -> bool:
    return isinstance(annotation, type) and typing.is_typeddict(annotation)


def _fields(annotation: Any) -> list[tuple[str, Any, bool]]:
    """(name, type, required) for the fields of a TypedDict or dataclass."""
    hints = typing.get_type_hints(annotation, include_extras=True)
    if _is_typeddict(annotation):
        keys = annotation.__required_keys__
        fields = []
        for key, hint in hints.items():
            required = key in keys  # Preserve inherited total=True/False defaults.
            wrapped = hint
            while typing.get_origin(wrapped) in _WRAPPERS:
                origin = typing.get_origin(wrapped)
                if origin in (typing.Required, typing.NotRequired):
                    required = origin is typing.Required
                wrapped = typing.get_args(wrapped)[0]
            # __required_keys__ cannot see Required/NotRequired inside strings
            # when annotations are postponed; resolved qualifiers take precedence.
            fields.append((key, hint, required))
        return fields
    return [
        (
            field.name,
            hints.get(field.name, Any),
            field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING,
        )
        for field in dataclasses.fields(annotation)
        if field.init  # ClassVar and init=False fields are not arguments
    ]


def _identity(value: Any) -> Any:
    return value


@dataclasses.dataclass
class _Argument:
    """The model's declaration and its Python decoder, compiled together."""

    schema: dict[str, Any]
    decode: Callable[[Any], Any] = _identity

    def __call__(self, value: Any) -> Any:
        return self.decode(value)


class _Schemas:
    """Compile annotations once; bind union validators after recursive definitions exist."""

    def __init__(self) -> None:
        self.definitions: dict[str, Any] = {}
        self.names: dict[Any, str] = {}
        self.building: dict[Any, _Argument] = {}
        self.recursive: set[Any] = set()
        self.scopes: set[str] = set()
        self.unions: list[tuple[list[_Argument], list[Any]]] = []

    def _name(self, annotation: Any) -> str:
        if annotation not in self.names:
            base = re.sub(r"[^A-Za-z0-9_.-]", "_", annotation.__qualname__)
            name, n = base, 2
            while name in self.names.values():
                name, n = f"{base}_{n}", n + 1
            self.names[annotation] = name
        return self.names[annotation]

    def finish(self) -> None:
        for options, validators in self.unions:
            validators.extend(
                schema_validator({**option.schema, "$defs": self.definitions}) for option in options
            )

    def compile(self, annotation: Any, where: str) -> _Argument:
        origin = typing.get_origin(annotation)
        args = typing.get_args(annotation)
        if origin is typing.Annotated:
            plan = self.compile(args[0], where)
            schema = dict(plan.schema)
            text = next((a for a in args[1:] if isinstance(a, str)), None)
            if text:
                schema["description"] = text
            return _Argument(schema, plan)
        if origin in _WRAPPERS:
            return self.compile(args[0], where)
        if annotation is Any or annotation is inspect.Parameter.empty:
            return _Argument({})
        if annotation in _PRIMITIVES:
            decoder = (
                annotation.fromisoformat
                if annotation in (date, datetime)
                else annotation
                if annotation in (UUID, Path, int, float)
                else _identity
            )
            return _Argument(dict(_PRIMITIVES[annotation]), decoder)
        if origin is Literal:
            return _Argument({"enum": list(args)})
        if origin in (Union, types.UnionType):
            options = [self.compile(a, where) for a in args]
            validators: list[Any] = []
            self.unions.append((options, validators))

            def union(value: Any) -> Any:
                for plan, validator in zip(options, validators):
                    if validator.is_valid(value):
                        try:
                            return plan(value)
                        except (TypeError, ValueError):
                            continue
                raise ValueError("No union branch can convert the supplied value")

            return _Argument({"anyOf": [p.schema for p in options]}, union)
        if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
            return _Argument({"enum": [member.value for member in annotation]}, annotation)
        # Canonicalize bare and parameterized containers in one place.
        kind = origin or annotation
        if kind in (list, set, frozenset, Sequence):
            item = self.compile(args[0], where) if args else _Argument({})
            schema = {"type": "array"}
            if args:
                schema["items"] = item.schema
            if kind in (set, frozenset):
                schema["uniqueItems"] = True
            container = list if kind is Sequence else kind
            return _Argument(schema, lambda value: container(item(v) for v in value))
        if kind is tuple:
            if not args:
                return _Argument({"type": "array"}, tuple)
            if len(args) == 2 and args[1] is Ellipsis:
                item = self.compile(args[0], where)
                return _Argument(
                    {"type": "array", "items": item.schema},
                    lambda value: tuple(item(v) for v in value),
                )
            items = [self.compile(a, where) for a in args]
            return _Argument(
                {
                    "type": "array",
                    "prefixItems": [p.schema for p in items],
                    "minItems": len(args),
                    "maxItems": len(args),
                },
                lambda value: tuple(plan(v) for plan, v in zip(items, value)),
            )
        if kind in (dict, Mapping):
            schema = {"type": "object"}
            item = _Argument({})
            if len(args) == 2:
                if args[0] is not str:
                    raise ConfigurationError(f"{where}: dictionary keys must be str")
                item = self.compile(args[1], where)
                schema["additionalProperties"] = item.schema
            return _Argument(schema, lambda value: {k: item(v) for k, v in value.items()})
        if _is_pydantic(annotation):
            # Allocate a scope per occurrence, including same-named union branches
            # and paths whose punctuation normalizes to the same string.
            base = re.sub(r"[^A-Za-z0-9_-]", "_", where)
            scope, n = base, 2
            while scope in self.scopes:
                scope, n = f"{base}_{n}", n + 1
            self.scopes.add(scope)
            model = annotation.model_json_schema(ref_template=f"#/$defs/{scope}.{{model}}")
            for key, value in model.pop("$defs", {}).items():
                self.definitions[f"{scope}.{key}"] = value
            return _Argument(model, annotation.model_validate)
        if _is_typeddict(annotation) or dataclasses.is_dataclass(annotation):
            return self._object(annotation, where)
        raise ConfigurationError(f"{where}: unsupported parameter type {annotation!r}")

    def _object(self, annotation: Any, where: str) -> _Argument:
        ref = {"$ref": f"#/$defs/{self._name(annotation)}"}
        if annotation in self.building:
            self.recursive.add(annotation)
            return _Argument(ref, self.building[annotation])
        plan = _Argument({})
        self.building[annotation] = plan
        try:
            fields = _fields(annotation)
            children = {key: self.compile(hint, f"{where}.{key}") for key, hint, _ in fields}
            schema = {
                "type": "object",
                "properties": {key: child.schema for key, child in children.items()},
                "required": [key for key, _, required in fields if required],
                "additionalProperties": False,
            }
        finally:
            del self.building[annotation]
        typed_dict = _is_typeddict(annotation)

        def decode(value: Any) -> Any:
            values = {key: children[key](v) for key, v in value.items()}
            return values if typed_dict else annotation(**values)

        plan.decode = decode
        if annotation in self.recursive:
            self.definitions[self._name(annotation)] = schema
            plan.schema = ref
        else:
            plan.schema = schema
        return plan


def _parse_docstring(doc: str) -> tuple[str, dict[str, str]]:
    """Summary text and per-parameter descriptions (Google, NumPy or Sphinx style)."""
    lines = doc.splitlines()
    summary: list[str] = []
    params: dict[str, str] = {}
    section = None  # None while reading the summary
    numpy = False  # NumPy sections are underlined; their entries read "name : type"
    current = None
    base = 0  # indentation of a Google-style entry
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        underline = index + 1 < len(lines) and set(lines[index + 1].strip()) == {"-"}
        sphinx = re.match(r":param\s+(?:[^:]*\s)?(\w+):\s*(.*)", stripped)
        if sphinx:
            section, current = "sphinx", sphinx.group(1)
            params[current] = sphinx.group(2).strip()
        elif _SECTION.match(stripped) and (stripped.endswith(":") or underline):
            section, numpy, current = stripped.rstrip(":").strip().lower(), underline, None
            index += 2 if underline else 1
            continue
        elif stripped.startswith(":"):
            section, current = "other", None
        elif section is None:
            summary.append(line)
        elif not stripped:
            pass
        elif section in _PARAM_SECTIONS:
            entry = re.match(r"(\w+)\s*(?:\([^)]*\))?\s*:\s*(.*)", stripped)
            if numpy and indent == 0:
                current = re.match(r"\w+", stripped).group(0)  # type: ignore[union-attr]
                params[current] = ""
            elif not numpy and entry and (current is None or indent <= base):
                current = entry.group(1)
                params[current] = entry.group(2).strip()
                base = indent
            elif current is not None:
                params[current] = (params[current] + " " + stripped).strip()
        elif section == "sphinx" and current is not None:
            params[current] = (params[current] + " " + stripped).strip()
        index += 1
    return "\n".join(summary).strip(), {k: v for k, v in params.items() if v}
