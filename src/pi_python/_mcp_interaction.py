"""Opt-in MCP reverse requests. SDK objects are imported only when this feature is used."""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field, replace
from importlib.metadata import version
from typing import Any, Literal, TypeVar
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from ._mcp_host import (
    SAMPLING_EXTENSION,
    SamplingProfile,
    SamplingRetryPolicy,
    SamplingObservation,
    SamplingFailure,
    _SamplingState,
    visible_message,
)
from ._mcp_sampling import from_sampling, from_sampling_result, to_sampling, to_sampling_result
from .cancellation import CancelToken
from .errors import (
    ConfigurationError,
    MessageValidationError,
    ProviderProtocolError,
    UnsupportedCapabilityError,
)
from .messages import AssistantMessage, CustomMessage, validate_json
from .models import ModelInfo
from .provider import ModelEvent, ModelRequest, Provider
from .stream import checked_events
from .tools import ToolContext

PROTOCOL_VERSION = "2025-11-25"
T = TypeVar("T")


def _require_sdk() -> None:
    try:
        parts = tuple(int(n) for n in version("mcp").split(".")[:2])
    except Exception as exc:
        raise ConfigurationError("MCP interactions need pi-python-core[mcp-interactive]") from exc
    if not (2, 3) <= parts < (3, 0):
        raise ConfigurationError(
            "MCP interactions require SDK >=2.3,<3; install pi-python-core[mcp-interactive]"
        )


def _timeout(value: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (float, int))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ConfigurationError("MCP callback timeout must be finite and positive")


async def _cancel_tasks(tasks: list[asyncio.Task[Any]]) -> None:
    # MCP callbacks run inside AnyIO cancellation scopes. Shield just the cleanup,
    # otherwise repeated cancellation can interrupt joining our asyncio children.
    import anyio

    for task in tasks:
        task.cancel()
    with anyio.CancelScope(shield=True):
        await asyncio.gather(*tasks, return_exceptions=True)


async def _await_cancel(awaitable: Awaitable[T], cancel: CancelToken) -> T:
    task = asyncio.ensure_future(awaitable)
    waiter = asyncio.create_task(cancel.wait())
    try:
        done, _ = await asyncio.wait([task, waiter], return_when=asyncio.FIRST_COMPLETED)
        if waiter in done:
            raise asyncio.CancelledError(cancel.reason)
        return await task
    finally:
        await _cancel_tasks([task, waiter])


def _copy_request(request: ModelRequest) -> ModelRequest:
    # Copy model data, not host executable objects. deepcopy of a bound method
    # clones its owner and would silently divert credentials/hooks to a copy.
    return replace(
        request,
        messages=deepcopy(request.messages),
        tools=deepcopy(request.tools),
        options=deepcopy(request.options),
    )


@dataclass(frozen=True)
class MCPRequestContext:
    """Host identity and available association; request_id is the reverse MCP request."""

    server: str
    plugin: str | None
    request_id: int | str
    protocol_version: str
    run_id: str
    tool_call_id: str
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False)
    _state: _SamplingState | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class MCPCallbackEvent:
    """Content-free audit event. No prompts, user answers, or credentials are included."""

    context: MCPRequestContext
    kind: Literal["sampling", "elicitation"]
    status: str


@dataclass(frozen=True)
class ElicitationRequest:
    context: MCPRequestContext
    message: str = field(repr=False)
    schema: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class ElicitationResponse:
    action: Literal["accept", "decline", "cancel"]
    content: dict[str, Any] | None = field(default=None, repr=False)


class SamplingHandler:
    """Host-owned Provider bridge. Reuse one handler to serialize a shared Provider.

    `authorize` sees the request before model access and must return True to approve.
    Supplying the handler is the host's service-level grant when no policy is supplied.
    Provider-specific options come only from the host, never from MCP metadata.
    """

    def __init__(
        self,
        provider: Provider,
        *,
        model: str | ModelInfo,
        options: Mapping[str, Any] | None = None,
        max_tokens: int = 4096,
        timeout: float = 60,
        max_concurrency: int = 1,
        allow_tools: bool = True,
        allow_images: bool = False,
        allow_temperature: bool = False,
        tool_choice_format: Literal["openai", "anthropic"] = "openai",
        authorize: Callable[[MCPRequestContext, ModelRequest], Awaitable[bool]] | None = None,
        profiles: Mapping[str, SamplingProfile] | None = None,
        retain_state: bool = True,
        prepare: Callable[[MCPRequestContext, str, ModelRequest], Awaitable[None]] | None = None,
        observe: Callable[[SamplingObservation], Awaitable[None]] | None = None,
        retry: SamplingRetryPolicy | None = None,
    ) -> None:
        _timeout(timeout)
        if (
            type(max_tokens) is not int
            or max_tokens <= 0
            or type(max_concurrency) is not int
            or max_concurrency <= 0
        ):
            raise ConfigurationError("Sampling limits must be positive integers")
        if tool_choice_format not in {"openai", "anthropic"}:
            raise ConfigurationError("Unknown sampling tool_choice_format")
        self.provider, self.model = provider, model
        self.options = deepcopy(dict(options or {}))
        if "max_tokens" in self.options and (
            type(self.options["max_tokens"]) is not int or self.options["max_tokens"] <= 0
        ):
            raise ConfigurationError("Host max_tokens must be a positive integer")
        self.max_tokens, self.timeout = max_tokens, timeout
        self.allow_tools, self.allow_images = allow_tools, allow_images
        self.allow_temperature, self.tool_choice_format = allow_temperature, tool_choice_format
        self.authorize = authorize
        self.retain_state, self.prepare, self.observe = retain_state, prepare, observe
        self.retry = retry or SamplingRetryPolicy()
        self.profiles = deepcopy(dict(profiles or {}))
        if "default" in self.profiles:
            raise ConfigurationError("The default sampling profile is configured by the handler")
        self.profiles["default"] = SamplingProfile(model, self.options, tool_choice_format)
        for name, profile in self.profiles.items():
            if not isinstance(name, str) or not name or not isinstance(profile, SamplingProfile):
                raise ConfigurationError("Sampling profiles need a name and SamplingProfile")
            if profile.tool_choice_format not in {"openai", "anthropic"}:
                raise ConfigurationError("Unknown sampling tool_choice_format")
            if profile.options.get("transport", "sse") != "sse":
                raise ConfigurationError(
                    "Sampling profiles currently support Provider SSE transport only"
                )
            maximum = profile.options.get("max_tokens", max_tokens)
            if type(maximum) is not int or maximum <= 0:
                raise ConfigurationError("Host max_tokens must be a positive integer")
            choice = profile.options.get("tool_choice", "auto")
            if isinstance(choice, dict) and profile.tool_choice_format == "anthropic":
                if set(choice) - {"type", "disable_parallel_tool_use"}:
                    raise ConfigurationError("Unsupported host sampling tool choice")
                choice = "required" if choice.get("type") == "any" else choice.get("type")
            if choice not in ("auto", "required", "none"):
                raise ConfigurationError("Host tool_choice must be auto, required or none")
        self._gate = asyncio.Semaphore(max_concurrency)

    async def __call__(self, context: MCPRequestContext, params: Any, cancel: CancelToken) -> Any:
        return await _await_cancel(self._sample(context, params, cancel), cancel)

    async def _sample(self, context: MCPRequestContext, params: Any, cancel: CancelToken) -> Any:
        from .messages import ImageContent

        data = params.model_dump(by_alias=True, exclude_none=True)
        extension = (data.get("_meta") or {}).get(SAMPLING_EXTENSION)
        profile_name, conversation = "default", ""
        if extension is not None:
            if not self.retain_state or context._state is None:
                raise UnsupportedCapabilityError("Host sampling state is unavailable")
            if not isinstance(extension, dict) or set(extension) != {
                "profile",
                "conversation",
                "history",
            }:
                raise UnsupportedCapabilityError("Invalid sampling extension")
            profile_name, conversation = extension["profile"], extension["conversation"]
            if (
                not isinstance(profile_name, str)
                or not isinstance(conversation, str)
                or not conversation
            ):
                raise UnsupportedCapabilityError("Invalid sampling profile or conversation")
        if profile_name not in self.profiles:
            raise PermissionError("Sampling profile is not authorized")
        profile = self.profiles[profile_name]
        if extension is None and (
            profile.options.get("reasoning") not in (None, "off")
            or profile.options.get("thinking") is not None
            or getattr(self.provider, "api", None) in {"openai-responses", "openai-codex-responses"}
        ):
            raise UnsupportedCapabilityError(
                "This Provider/mode requires the sampling host-state extension"
            )
        request = from_sampling(data)
        if extension is not None:
            assistants = [m for m in request.messages if isinstance(m, AssistantMessage)]
            refs = extension["history"]
            if (
                not isinstance(refs, list)
                or len(refs) != len(assistants)
                or any(not isinstance(r, str) for r in refs)
            ):
                raise UnsupportedCapabilityError(
                    "Sampling history requires retained response references"
                )
            assert context._state is not None
            state = context._state
            restored = iter(
                state.restore(ref, conversation, profile_name, m)
                for ref, m in zip(refs, assistants)
            )
            request.messages = [
                next(restored) if isinstance(m, AssistantMessage) else m for m in request.messages
            ]
        if not self.allow_tools and (
            request.tools
            or "tool_choice" in request.options
            or any(isinstance(m, AssistantMessage) and m.tool_calls for m in request.messages)
        ):
            raise UnsupportedCapabilityError("Sampling tools are not enabled")
        if not self.allow_images and any(
            isinstance(b, ImageContent)
            for m in request.messages
            if not isinstance(m, CustomMessage)
            for b in (m.content if isinstance(m.content, list) else [])
        ):
            raise UnsupportedCapabilityError("Sampling images are not enabled")
        if "temperature" in request.options and not self.allow_temperature:
            raise UnsupportedCapabilityError("Sampling temperature is not authorized")
        tokens = request.options["max_tokens"]
        if type(tokens) is not int or tokens <= 0:
            raise UnsupportedCapabilityError("Sampling maxTokens must be positive")
        request.model = profile.model.id if isinstance(profile.model, ModelInfo) else profile.model
        request.model_info = profile.model if isinstance(profile.model, ModelInfo) else None
        choice = request.options.get("tool_choice", "auto")
        host_choice = profile.options.get("tool_choice")
        host_mode = host_choice.get("type") if isinstance(host_choice, dict) else host_choice
        host_mode = "required" if host_mode == "any" else host_mode
        if host_mode is not None and choice != host_mode:
            raise PermissionError("Sampling tool choice denied by host")
        request.options = {**request.options, **deepcopy(dict(profile.options))}
        request.options["max_tokens"] = min(
            tokens, self.max_tokens, profile.options.get("max_tokens", self.max_tokens)
        )
        if request.tools:
            if profile.tool_choice_format == "anthropic":
                request.options["tool_choice"] = {
                    **(host_choice if isinstance(host_choice, dict) else {}),
                    "type": "any" if choice == "required" else choice,
                }
            else:
                request.options["tool_choice"] = choice
        else:
            for key in ("tool_choice", "parallel_tool_calls", "disable_parallel_tool_use"):
                request.options.pop(key, None)
                if isinstance(request.options.get("sampling_params"), dict):
                    request.options["sampling_params"].pop(key, None)
        async with asyncio.timeout(self.timeout):
            if self.prepare:
                await self.prepare(context, profile_name, request)
            if request.options.get("transport", "sse") != "sse":
                raise UnsupportedCapabilityError(
                    "Sampling profiles currently support Provider SSE transport only"
                )
            if (
                self.authorize is not None
                and await self.authorize(context, _copy_request(request)) is not True
            ):
                raise PermissionError("Sampling request denied")
            async with self._gate:
                cancel.raise_if_cancelled()
                if request.model_info:
                    request.model_info.validate_request(request)
                response = await self._generate(context, profile_name, request, cancel)
        names = {t.name for t in request.tools}
        if any(call.name not in names for call in response.tool_calls):
            raise ProviderProtocolError("Provider called an undeclared sampling tool")
        if (choice == "none" and response.tool_calls) or (
            choice == "required" and not response.tool_calls
        ):
            raise ProviderProtocolError("Provider did not honor sampling tool choice")
        result = to_sampling_result(
            visible_message(response) if extension is not None else response,
            params.tools is not None or params.tool_choice is not None,
            request.model,
        )
        from_sampling_result(result.model_dump(by_alias=True, exclude_none=True))
        if extension is not None:
            assert context._state is not None
            ref = context._state.save(conversation, profile_name, response)
            result.meta = {SAMPLING_EXTENSION: {"ref": ref}}
        return result

    async def _generate(
        self, context: MCPRequestContext, profile: str, request: ModelRequest, cancel: CancelToken
    ) -> AssistantMessage:
        from .providers.transport import ProviderHTTPError

        for attempt in range(1, self.retry.max_attempts + 1):
            response = None
            failure = None
            aborted = False
            source = None
            events = None
            try:
                source = self.provider.stream(_copy_request(request), cancel)
                events = checked_events(source, AssistantMessage([], model=request.model))
                async for event in events:
                    if event.type == "error":
                        response = event.message
                        if event.reason == "aborted" or (
                            response and response.stop_reason == "aborted"
                        ):
                            aborted = True
                        diagnostic = (
                            next(
                                (
                                    d
                                    for d in (response.diagnostics or [])
                                    if d.get("type") == "provider_http_error"
                                ),
                                {},
                            )
                            if response
                            else {}
                        )
                        status = diagnostic.get("status")
                        failure = self._failure(status, diagnostic.get("retry_after"), attempt)
                    if event.type == "done":
                        response = event.message
            except ProviderHTTPError as exc:
                failure = self._failure(exc.status, exc.retry_after, attempt)
            except Exception:
                failure = SamplingFailure(attempts=attempt)
            finally:
                if events is not None:
                    await events.aclose()
                if source is not None and hasattr(source, "aclose"):
                    await source.aclose()
            if self.observe:
                await self.observe(
                    SamplingObservation(
                        context,
                        profile,
                        attempt,
                        "cancelled" if aborted else "error" if failure else "completed",
                        deepcopy(response.usage)
                        if response
                        and response.usage
                        and not any(
                            d.get("type") == "usage_unavailable"
                            for d in (response.diagnostics or [])
                        )
                        else None,
                        response.response_id if response else None,
                        failure.category if failure else None,
                        failure.retry_after if failure else None,
                    )
                )
            if aborted:
                raise asyncio.CancelledError()
            if failure is None:
                assert response is not None
                return response
            if not failure.retryable or attempt == self.retry.max_attempts:
                raise failure
            delay = min(self.retry.max_delay, self.retry.initial_delay * 2 ** (attempt - 1))
            # A longer Retry-After is never shortened; the overall timeout may end it.
            await asyncio.sleep(max(delay, failure.retry_after or 0))
            cancel.raise_if_cancelled()
        raise AssertionError("unreachable")

    @staticmethod
    def _failure(status: Any, retry_after: Any, attempt: int) -> SamplingFailure:
        category = (
            "rate_limit"
            if status == 429
            else "authentication"
            if status in (401, 403)
            else "server"
            if isinstance(status, int) and status >= 500
            else "request"
            if isinstance(status, int)
            else "provider"
        )
        delay = (
            retry_after
            if isinstance(retry_after, (int, float))
            and math.isfinite(retry_after)
            and retry_after >= 0
            else None
        )
        return SamplingFailure(
            category,
            retryable=status in (429, 500, 502, 503, 504),
            retry_after=delay,
            attempts=attempt,
        )


def _form_validator(schema: dict[str, Any]) -> Draft202012Validator:
    """Flat form subset; never fetch schema references or infer consent from defaults."""
    validate_json(schema)
    # Check the subset before JSON Schema's meta-validator, which can compile
    # remote regexes too. Python regex execution cannot be bounded by asyncio.
    if schema.get("type") != "object" or set(schema) - {
        "type",
        "properties",
        "required",
        "title",
        "description",
        "additionalProperties",
    }:
        raise UnsupportedCapabilityError("Elicitation requires a flat object form schema")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise UnsupportedCapabilityError("Invalid elicitation properties")
    if type(schema.get("additionalProperties", False)) is not bool:
        raise UnsupportedCapabilityError("Elicitation additionalProperties must be boolean")
    sensitive = {
        "password",
        "passwd",
        "apikey",
        "accesstoken",
        "refreshtoken",
        "clientsecret",
        "secret",
        "cardnumber",
        "creditcard",
        "cvv",
        "cvc",
    }
    if any(re.sub(r"[^a-z]", "", name.lower()) in sensitive for name in properties):
        raise UnsupportedCapabilityError(
            "Form elicitation cannot collect credentials or payment secrets"
        )
    formats = FormatChecker()
    for prop in properties.values():
        if not isinstance(prop, dict) or prop.get("type") not in {
            "string",
            "boolean",
            "number",
            "integer",
        }:
            raise UnsupportedCapabilityError("Elicitation supports primitive form fields only")
        if set(prop) - {
            "type",
            "title",
            "description",
            "default",
            "enum",
            "enumNames",
            "oneOf",
            "minLength",
            "maxLength",
            "format",
            "minimum",
            "maximum",
        }:
            raise UnsupportedCapabilityError("Unsupported elicitation field constraint")
        if prop.get("format") not in (None, "email", "uri", "date", "date-time"):
            raise UnsupportedCapabilityError("Unsupported or sensitive elicitation format")
        if prop.get("format") is not None and prop["format"] not in formats.checkers:
            raise UnsupportedCapabilityError(
                "Elicitation format needs jsonschema[format-nongpl]; install pi-python-core[mcp-interactive]"
            )
        if "oneOf" in prop and (
            not isinstance(prop["oneOf"], list)
            or any(
                not isinstance(p, dict)
                or set(p) - {"const", "title"}
                or not isinstance(p.get("const"), str)
                for p in prop["oneOf"]
            )
        ):
            raise UnsupportedCapabilityError(
                "Elicitation oneOf supports titled string choices only"
            )
    checked = deepcopy(schema)
    # Never send fields that the server did not ask the user to review.
    checked["additionalProperties"] = False
    try:
        Draft202012Validator.check_schema(checked)
    except SchemaError:
        raise UnsupportedCapabilityError("Invalid elicitation form schema") from None
    return Draft202012Validator(checked, format_checker=formats)


class ElicitationHandler:
    """Form-only host UI bridge. Handlers must allow review, decline and cancel.

    Form elicitation must never collect passwords, API keys, access tokens or payment
    credentials. The UI/policy must reject sensitive questions, including those not
    identifiable from schema field names. URL mode is not supported or advertised.
    """

    def __init__(
        self,
        handler: Callable[[ElicitationRequest, CancelToken], Awaitable[ElicitationResponse]],
        *,
        timeout: float = 300,
        authorize: Callable[[ElicitationRequest], Awaitable[bool]] | None = None,
    ) -> None:
        _timeout(timeout)
        self.handler, self.timeout, self.authorize = handler, timeout, authorize

    async def __call__(self, context: MCPRequestContext, params: Any, cancel: CancelToken) -> Any:
        return await _await_cancel(self._elicit(context, params, cancel), cancel)

    async def _elicit(self, context: MCPRequestContext, params: Any, cancel: CancelToken) -> Any:
        from mcp import types

        if params.mode != "form" or params.task is not None:
            raise UnsupportedCapabilityError("Only ordinary form elicitation is enabled")
        validator = _form_validator(params.requested_schema)
        request = ElicitationRequest(context, params.message, deepcopy(params.requested_schema))
        async with asyncio.timeout(self.timeout):
            if self.authorize is not None and await self.authorize(request) is not True:
                raise PermissionError("Elicitation request denied")
            response = await self.handler(request, cancel)
        if not isinstance(response, ElicitationResponse) or response.action not in {
            "accept",
            "decline",
            "cancel",
        }:
            raise ValueError("Invalid elicitation response")
        if response.action == "accept":
            validate_json(response.content)
            if not validator.is_valid(response.content):
                raise ValueError("Elicitation answer does not satisfy the requested form")
        elif response.content is not None:
            raise ValueError("Declined or cancelled elicitation must not include data")
        return types.ElicitResult(action=response.action, content=deepcopy(response.content))


@dataclass(frozen=True)
class MCPCallbacks:
    """A host's explicit per-service grant. Never store this object in mcp.json."""

    sampling: SamplingHandler | None = None
    elicitation: ElicitationHandler | None = None
    max_requests_per_call: int = 32
    on_event: Callable[[MCPCallbackEvent], Awaitable[None]] | None = None

    def __post_init__(self) -> None:
        if type(self.max_requests_per_call) is not int or self.max_requests_per_call <= 0:
            raise ConfigurationError("max_requests_per_call must be a positive integer")

    @property
    def capabilities(self) -> tuple[str, ...]:
        result = []
        if self.sampling is not None:
            result.append("sampling")
            if self.sampling.allow_tools:
                result.append("sampling.tools")
            if self.sampling.retain_state:
                result.extend(("sampling.host_state", "sampling.profiles"))
        if self.elicitation is not None:
            result.append("elicitation.form")
        return tuple(result)


@dataclass
class _Scope:
    context: ToolContext
    token: CancelToken = field(default_factory=CancelToken)
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    requests: int = 0
    state: _SamplingState = field(default_factory=_SamplingState)


class _Interaction:
    def __init__(self, callbacks: MCPCallbacks, server: str, plugin: str | None) -> None:
        _require_sdk()
        self.callbacks, self.server, self.plugin = callbacks, server, plugin
        self.protocol_version: str | None = None
        self._gate = asyncio.Lock()
        self.scope: _Scope | None = None
        self.closed = False

    @asynccontextmanager
    async def call(self, context: ToolContext) -> AsyncIterator[None]:
        async with self._gate:
            if self.closed:
                raise ConnectionError("MCP connection is closed")
            context.cancel.raise_if_cancelled()
            scope = _Scope(context)
            self.scope = scope
            try:
                yield
            finally:
                self.scope = None
                scope.token.cancel("Outer MCP tool call ended")
                await _cancel_tasks(list(scope.tasks))
                scope.state.clear()

    async def aclose(self) -> None:
        self.closed = True
        if self.scope:
            self.scope.token.cancel("MCP connection closed")
            await _cancel_tasks(list(self.scope.tasks))
            self.scope.state.clear()

    async def sampling(self, context: Any, params: Any) -> Any:
        return await self._dispatch("sampling", context, params)

    async def elicitation(self, context: Any, params: Any) -> Any:
        return await self._dispatch("elicitation", context, params)

    async def _dispatch(
        self, kind: Literal["sampling", "elicitation"], raw: Any, params: Any
    ) -> Any:
        from mcp import types

        scope = self.scope
        handler = getattr(self.callbacks, kind)
        if (
            self.closed
            or scope is None
            or scope.token.cancelled
            or self.protocol_version != PROTOCOL_VERSION
        ):
            return types.ErrorData(
                code=-32600, message="MCP reverse request requires an active tool call"
            )
        if handler is None:
            return types.ErrorData(code=-32601, message="MCP capability is not authorized")
        scope.requests += 1
        if scope.requests > self.callbacks.max_requests_per_call:
            return types.ErrorData(code=-32000, message="MCP reverse request budget exceeded")
        context = MCPRequestContext(
            self.server,
            self.plugin,
            raw.request_id,
            self.protocol_version,
            scope.context.run_id,
            scope.context.call_id,
            deepcopy(raw.meta or {}),
            scope.state,
        )
        token = CancelToken()
        task = asyncio.create_task(handler(context, params, token), name=f"mcp-{kind}-callback")
        scope.tasks.add(task)
        status = "completed"
        try:
            response = await task
            if kind == "elicitation":
                status = response.action
            return response
        except SamplingFailure as exc:
            status = "error"
            return types.ErrorData(
                code=-32002,
                message="MCP sampling model request failed",
                data={
                    "category": exc.category,
                    "retryable": exc.retryable,
                    "retryAfter": exc.retry_after,
                    "attempts": exc.attempts,
                    "retryOwner": "host",
                },
            )
        except TimeoutError:
            status = "timeout"
            return types.ErrorData(code=-32001, message=f"MCP {kind} timed out")
        except PermissionError:
            status = "denied"
            return types.ErrorData(code=-1, message=f"MCP {kind} denied by host policy")
        except (ValueError, MessageValidationError):
            status = "invalid"
            return types.ErrorData(
                code=-32602, message=f"Unsupported or invalid MCP {kind} request or response"
            )
        except asyncio.CancelledError:
            status = "cancelled"
            # Cancelling our child is a single-request outcome, not cancellation of
            # the SDK dispatcher's task group. Its own cancel scopes still govern
            # peer cancellation and connection teardown.
            return types.ErrorData(code=-32800, message=f"MCP {kind} request cancelled")
        except Exception:
            status = "error"
            # Provider exceptions may contain response bodies or credentials. Never
            # forward exception text to the server or emit it to a default logger.
            return types.ErrorData(code=-32603, message=f"MCP {kind} handler failed")
        finally:
            token.cancel(status)
            await _cancel_tasks([task])
            scope.tasks.discard(task)
            if self.callbacks.on_event:
                # Audit failures cannot replace the protocol outcome or stall cleanup.
                import anyio

                with anyio.move_on_after(1, shield=True):
                    try:
                        await self.callbacks.on_event(MCPCallbackEvent(context, kind, status))
                    except Exception:
                        pass

    def session(self, read: Any, write: Any) -> Any:
        from mcp import ClientSession, types

        interaction = self

        class InteractiveSession(ClientSession):
            # SDK 2.3's callback registration advertises URL mode unconditionally.
            # Amend the public initialize request via public send_request, rather
            # than modifying SDK private attributes or replacing its dispatcher.
            async def send_request(
                self, request: Any, result_type: Any, *args: Any, **kwargs: Any
            ) -> Any:
                if isinstance(request, types.InitializeRequest):
                    request = request.model_copy(deep=True)
                    request.params.protocol_version = PROTOCOL_VERSION
                    if (
                        interaction.callbacks.sampling
                        and interaction.callbacks.sampling.retain_state
                    ):
                        request.params.capabilities.experimental = {
                            **(request.params.capabilities.experimental or {}),
                            SAMPLING_EXTENSION: {},
                        }
                    if request.params.capabilities.elicitation:
                        request.params.capabilities.elicitation = types.ElicitationCapability(
                            form=types.FormElicitationCapability()
                        )
                result = await super().send_request(request, result_type, *args, **kwargs)
                if isinstance(request, types.InitializeRequest):
                    if result.protocol_version != PROTOCOL_VERSION:
                        raise ConfigurationError("MCP interactions require protocol 2025-11-25")
                    interaction.protocol_version = result.protocol_version
                return result

        kwargs: dict[str, Any] = {}
        if self.callbacks.sampling:
            kwargs["sampling_callback"] = self.sampling
            kwargs["sampling_capabilities"] = types.SamplingCapability(
                tools=types.SamplingToolsCapability()
                if self.callbacks.sampling.allow_tools
                else None,
            )
        if self.callbacks.elicitation:
            kwargs["elicitation_callback"] = self.elicitation
        return InteractiveSession(read, write, **kwargs)


class SamplingProvider:
    """Request-scoped server Provider; one final event, no fabricated streaming.

    Use `async with SamplingProvider(ctx.request_context) as provider` inside an MCP
    request handler. Exiting the block revokes the provider and cancels its requests.
    The SDK session and host Provider remain owned by their respective callers.
    """

    name = "mcp-sampling"

    def __init__(
        self,
        context: Any,
        *,
        max_tokens: int = 4096,
        timeout: float = 120,
        profile: str | None = None,
        require_host_state: bool = False,
    ) -> None:
        _require_sdk()
        _timeout(timeout)
        if context.protocol_version != PROTOCOL_VERSION or context.request_id is None:
            raise ConfigurationError(
                "SamplingProvider needs an active 2025-11-25 MCP request context"
            )
        if type(max_tokens) is not int or max_tokens <= 0:
            raise ConfigurationError("Sampling max_tokens must be positive")
        self.context, self.max_tokens, self.timeout = context, max_tokens, timeout
        self.profile, self.require_host_state = profile, require_host_state
        self._conversation = uuid4().hex
        self._open = False
        self._used = False
        self._tasks: set[asyncio.Task[Any]] = set()

    async def __aenter__(self) -> SamplingProvider:
        if self._used:
            raise ConfigurationError("A SamplingProvider request scope can be entered only once")
        capabilities = self.context.session.client_capabilities
        if (self.require_host_state or self.profile is not None) and (
            not capabilities or SAMPLING_EXTENSION not in (capabilities.experimental or {})
        ):
            raise UnsupportedCapabilityError("MCP host must enable sampling host state/profiles")
        self._open = self._used = True
        return self

    async def __aexit__(self, *args: Any) -> None:
        self._open = False
        await _cancel_tasks(list(self._tasks))

    async def stream(self, request: ModelRequest, cancel: CancelToken) -> AsyncIterator[ModelEvent]:
        from mcp import types

        if not self._open:
            raise ConfigurationError(
                "Use SamplingProvider inside its request-scoped async with block"
            )
        capabilities = self.context.session.client_capabilities
        extended = bool(
            capabilities
            and SAMPLING_EXTENSION in (getattr(capabilities, "experimental", None) or {})
        )
        outgoing = _copy_request(request)
        selected = outgoing.options.pop("sampling_profile", self.profile or "default")
        if selected != "default" and not extended:
            raise UnsupportedCapabilityError("MCP host has not enabled sampling profiles")
        data = to_sampling(outgoing, self.max_tokens)
        if extended:
            history = []
            for message in request.messages:
                if isinstance(message, AssistantMessage):
                    if message.provider != self.name or not message.response_id:
                        raise UnsupportedCapabilityError(
                            "Host state needs unmodified SamplingProvider history"
                        )
                    history.append(message.response_id)
            data["_meta"] = {
                SAMPLING_EXTENSION: {
                    "profile": selected,
                    "conversation": self._conversation,
                    "history": history,
                }
            }
        params = types.CreateMessageRequestParams.model_validate(data)
        capabilities = self.context.session.client_capabilities
        if not capabilities or capabilities.sampling is None:
            raise UnsupportedCapabilityError("MCP host has not enabled sampling")
        if (
            params.tools is not None or params.tool_choice is not None
        ) and capabilities.sampling.tools is None:
            raise UnsupportedCapabilityError("MCP host has not enabled sampling.tools")

        async def sample() -> Any:
            async with asyncio.timeout(self.timeout):
                if extended:
                    from mcp.shared.message import ServerMessageMetadata

                    return await self.context.session.send_request(
                        request=types.CreateMessageRequest(params=params),
                        result_type=types.CreateMessageResultWithTools
                        if params.tools is not None or params.tool_choice is not None
                        else types.CreateMessageResult,
                        metadata=ServerMessageMetadata(related_request_id=self.context.request_id),
                    )
                return await self.context.session.create_message(
                    params.messages,
                    max_tokens=params.max_tokens,
                    system_prompt=params.system_prompt,
                    include_context="none",
                    temperature=params.temperature,
                    tools=params.tools,
                    tool_choice=params.tool_choice,
                    related_request_id=self.context.request_id,
                )

        task = asyncio.create_task(sample(), name="mcp-sampling-provider")
        self._tasks.add(task)
        try:
            result = await _await_cancel(task, cancel)
            result_data = result.model_dump(by_alias=True, exclude_none=True)
            meta = result_data.pop("_meta", {})
            message = from_sampling_result(result_data)
            if extended:
                ref = meta.get(SAMPLING_EXTENSION, {}).get("ref")
                if not isinstance(ref, str) or not ref:
                    raise ProviderProtocolError("Missing host sampling state reference")
                message.response_id = ref
            elif meta:
                raise UnsupportedCapabilityError("Unnegotiated sampling response metadata")
            yield ModelEvent.done(message)
        finally:
            self._tasks.discard(task)
            await _cancel_tasks([task])
