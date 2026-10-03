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
from .tools import Tool, ToolContext

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
    converters: dict[str, Any] = {}
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
        schema = schemas.schema(annotation, parameter.name)
        if parameter.name in documented and "description" not in schema:
            schema["description"] = documented[parameter.name]
        if parameter.default is parameter.empty:
            required.append(parameter.name)
        else:
            default = _jsonable(parameter.default)
            if default is not _MISSING:
                schema["default"] = default
        properties[parameter.name] = schema
        converters[parameter.name] = annotation
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
    if schemas.definitions:
        input_schema["$defs"] = schemas.definitions

    def arguments(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        values = {key: _convert(converters[key], value) for key, value in args.items()}
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
        return [(key, hint, key in keys) for key, hint in hints.items()]
    return [
        (
            field.name,
            hints.get(field.name, Any),
            field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING,
        )
        for field in dataclasses.fields(annotation)
        if field.init  # ClassVar and init=False fields are not arguments
    ]


class _Schemas:
    """JSON Schema for parameter types; recursive types go to $defs."""

    def __init__(self) -> None:
        self.definitions: dict[str, Any] = {}
        self.names: dict[Any, str] = {}
        self.building: list[Any] = []
        self.recursive: set[Any] = set()

    def _name(self, annotation: Any) -> str:
        if annotation not in self.names:
            base = re.sub(r"[^A-Za-z0-9_.-]", "_", annotation.__qualname__)
            name, n = base, 2
            while name in self.names.values():
                name, n = f"{base}_{n}", n + 1
            self.names[annotation] = name
        return self.names[annotation]

    def schema(self, annotation: Any, where: str) -> dict[str, Any]:
        origin = typing.get_origin(annotation)
        args = typing.get_args(annotation)
        schema: dict[str, Any]
        if origin is typing.Annotated:
            schema = self.schema(args[0], where)
            text = next((a for a in args[1:] if isinstance(a, str)), None)
            if text:
                schema["description"] = text
            return schema
        if origin in _WRAPPERS:
            return self.schema(args[0], where)
        if annotation is Any or annotation is inspect.Parameter.empty:
            return {}
        if annotation in _PRIMITIVES:
            return dict(_PRIMITIVES[annotation])
        if origin is Literal:
            return {"enum": list(args)}
        if origin in (Union, types.UnionType):
            options = [self.schema(a, where) for a in args]
            return options[0] if len(options) == 1 else {"anyOf": options}
        if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
            return {"enum": [member.value for member in annotation]}
        if origin in (list, set, frozenset, Sequence) or annotation in (list, set, frozenset):
            schema = {"type": "array"}
            if args:
                schema["items"] = self.schema(args[0], where)
            if origin in (set, frozenset):
                schema["uniqueItems"] = True
            return schema
        if origin is tuple or annotation is tuple:
            if len(args) == 2 and args[1] is Ellipsis:
                return {"type": "array", "items": self.schema(args[0], where)}
            if not args:
                return {"type": "array"}
            return {
                "type": "array",
                "prefixItems": [self.schema(a, where) for a in args],
                "minItems": len(args),
                "maxItems": len(args),
            }
        if origin in (dict, Mapping) or annotation is dict:
            schema = {"type": "object"}
            if len(args) == 2:
                if args[0] is not str:
                    raise ConfigurationError(f"{where}: dictionary keys must be str")
                schema["additionalProperties"] = self.schema(args[1], where)
            return schema
        if _is_pydantic(annotation):
            # Namespace this parameter's definitions so two models named alike cannot collide.
            scope = re.sub(r"[^A-Za-z0-9_-]", "_", where)
            model = annotation.model_json_schema(ref_template=f"#/$defs/{scope}.{{model}}")
            for key, value in model.pop("$defs", {}).items():
                self.definitions[f"{scope}.{key}"] = value
            return model
        if _is_typeddict(annotation) or dataclasses.is_dataclass(annotation):
            return self._object(annotation, where)
        raise ConfigurationError(f"{where}: unsupported parameter type {annotation!r}")

    def _object(self, annotation: Any, where: str) -> dict[str, Any]:
        ref = {"$ref": f"#/$defs/{self._name(annotation)}"}
        if annotation in self.building:  # a type that contains itself
            self.recursive.add(annotation)
            return ref
        self.building.append(annotation)
        try:
            fields = _fields(annotation)
            schema = {
                "type": "object",
                "properties": {key: self.schema(hint, f"{where}.{key}") for key, hint, _ in fields},
                "required": [key for key, _, required in fields if required],
                "additionalProperties": False,
            }
        finally:
            self.building.pop()
        if annotation in self.recursive:
            self.definitions[self._name(annotation)] = schema
            return ref
        return schema


def _convert(annotation: Any, value: Any) -> Any:
    """Turn validated JSON into the Python value the annotation asks for."""
    annotation = _strip(annotation)
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if value is None:
        return None
    if origin in (Union, types.UnionType):
        for option in args:
            if option is type(None):
                continue
            try:
                return _convert(option, value)
            except Exception:
                continue
        return value
    if isinstance(annotation, type):
        if issubclass(annotation, enum.Enum):
            return annotation(value)
        if annotation is datetime:
            return datetime.fromisoformat(value)
        if annotation is date:
            return date.fromisoformat(value)
        if annotation is UUID:
            return UUID(value)
        if annotation is Path:
            return Path(value)
        if annotation is float and isinstance(value, int):
            return float(value)
        if annotation is int and isinstance(value, float) and value.is_integer():
            return int(value)  # JSON Schema counts 3.0 as an integer
        if _is_pydantic(annotation):
            return annotation.model_validate(value)  # type: ignore[attr-defined]
        if _is_typeddict(annotation):
            hints = {key: hint for key, hint, _ in _fields(annotation)}
            return {key: _convert(hints.get(key, Any), item) for key, item in value.items()}
        if dataclasses.is_dataclass(annotation):
            hints = {key: hint for key, hint, _ in _fields(annotation)}
            return annotation(**{k: _convert(hints.get(k, Any), v) for k, v in value.items()})
    if origin in (list, Sequence) and args:
        return [_convert(args[0], item) for item in value]
    if origin in (set, frozenset):
        return origin(_convert(args[0], item) if args else item for item in value)
    if origin is tuple and args:
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_convert(args[0], item) for item in value)
        return tuple(_convert(a, item) for a, item in zip(args, value))
    if origin in (dict, Mapping) and len(args) == 2:
        return {key: _convert(args[1], item) for key, item in value.items()}
    return value


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
