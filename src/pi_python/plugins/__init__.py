"""Plugins: named bundles that extend an Agent.

A plugin can contribute tools, system prompt text, hooks, event listeners, skills, prompt
templates, subagent definitions and MCP servers. It is a directory (conventional
``skills/``, ``prompts/``, ``agents/`` and ``mcp.json``, plus an optional ``plugin.py``
with ``setup(api)``), an installed package that registers an entry point in the
``pi_python.plugins`` group, or a :class:`Plugin` object built in code. It corresponds to
a Pi package; the Python code in it corresponds to a Pi extension.

    async with load_plugins(["hpc-slurm", "./lab-plugin"], services={...}) as plugins:
        agent = plugins.agent(provider=..., model=..., system_prompt="You help with HPC jobs.")
        await agent.prompt(plugins.expand("/submit run_42"))

Loading a plugin runs its code and may start its MCP servers; load only plugins you trust.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.machinery
import importlib.util
import json
import math
import os
import re
import sys
import warnings
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from copy import copy
from dataclasses import dataclass, replace
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path, PurePath, PureWindowsPath
from types import ModuleType
from typing import Any, TypeVar

from ..agent import Agent
from ..errors import ConfigurationError
from ..events import EventListener
from ..function_tools import tool as function_tool
from ..hooks import Hooks
from ..limits import RunLimits
from ..models import ModelInfo
from ..sync import run_sync
from ..tasks import TaskScope
from ..features import _names, require_features
from ..tools import Tool, ToolContext, ToolResult, invoke
from .._mcp_interaction import MCPCallbacks
from ._compose import (
    HOOK_NAMES,
    PluginFailure,
    compose_hooks,
    isolated_listener,
    log_plugin_failure,
    logger,
)
from ._resources import (
    AgentDefinition,
    PromptTemplate,
    Skill,
    expand_prompt_template,
    expand_skill_command,
    format_skills_for_prompt,
    load_agent_definitions,
    load_prompt_templates,
    load_skills,
    skill_block,
)
from ._subagents import TOOL_NAME as SUBAGENT_TOOL
from ._subagents import build_subagent, subagent_tool

__all__ = [
    "ENTRY_POINT_GROUP",
    "AgentDefinition",
    "CheckResult",
    "InstalledPlugin",
    "Plugin",
    "PluginAPI",
    "PluginFailure",
    "PluginSet",
    "PluginWarning",
    "PromptTemplate",
    "ReadinessResult",
    "Skill",
    "discover_plugins",
    "load_plugins",
]

ENTRY_POINT_GROUP = "pi_python.plugins"
READ_SKILL_TOOL = "read_skill"
MAX_SKILL_FILE_BYTES = 256 * 1024
_MCP_KEYS = {
    "process_scope",
    "type",
    "command",
    "args",
    "env",
    "cwd",
    "url",
    "headers",
    "enabled",
    "description",
    "call_metadata",
    "required_capabilities",
}
_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_MISSING: Any = object()
F = TypeVar("F", bound=Callable[..., Any])


class PluginWarning(UserWarning):
    """A plugin resource was skipped or needs attention; loading continued."""


@dataclass
class Plugin:
    """A plugin defined in code, or the description of one that was resolved.

    `setup(api)` may be sync or async. `root` is the directory searched for the
    conventional ``skills/``, ``prompts/``, ``agents/`` and ``mcp.json``. None means no
    directory, except for a Plugin exported through an entry point, where None means the
    package that exports it.
    """

    name: str
    setup: Callable[[PluginAPI], Any] | None = None
    root: str | os.PathLike[str] | None = None
    version: str | None = None
    source: str = "code"
    requires: tuple[str, ...] = ()


@dataclass(frozen=True)
class InstalledPlugin:
    """A plugin an installed distribution registers; listing it does not import it."""

    name: str
    target: str
    distribution: str | None
    version: str | None


@dataclass(frozen=True)
class CheckResult:
    """The outcome of one self-check a plugin registered."""

    plugin: str
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class ReadinessResult:
    """A snapshot after self-checks and MCP pings, not a continuous health guarantee."""

    loaded: bool
    plugins: tuple[str, ...]
    diagnostics: tuple[str, ...]
    missing_tools: tuple[str, ...]
    missing_skills: tuple[str, ...]
    missing_mcp_servers: tuple[str, ...]
    missing_checks: tuple[tuple[str, str], ...]
    checks: tuple[CheckResult, ...]
    missing_mcp_capabilities: tuple[tuple[str, str], ...] = ()

    @property
    def ready(self) -> bool:
        return (
            self.loaded
            and not (
                self.missing_tools
                or self.missing_skills
                or self.missing_mcp_servers
                or self.missing_checks
                or self.missing_mcp_capabilities
            )
            and all(check.passed for check in self.checks)
        )

    def require_ready(self) -> None:
        """Raise ConfigurationError with the unmet requirements, if any."""
        if self.ready:
            return
        reasons = []
        if not self.loaded:
            reasons.append("plugins are not open")
        for label, missing in (
            ("tools", self.missing_tools),
            ("skills", self.missing_skills),
            ("MCP servers", self.missing_mcp_servers),
            ("checks", self.missing_checks),
            ("MCP capabilities", self.missing_mcp_capabilities),
        ):
            if missing:
                reasons.append(f"missing {label}: {missing}")
        for check in self.checks:
            if not check.passed:
                reasons.append(f"check {check.plugin}/{check.name} failed: {check.detail}")
        raise ConfigurationError("Plugins are not ready: " + "; ".join(reasons))


def discover_plugins() -> list[InstalledPlugin]:
    """Plugins installed in this environment, by entry point, sorted by name."""
    found = []
    for ep in sorted(entry_points(group=ENTRY_POINT_GROUP), key=lambda e: e.name):
        dist = ep.dist
        found.append(
            InstalledPlugin(
                ep.name, ep.value, dist.name if dist else None, dist.version if dist else None
            )
        )
    return found


class PluginAPI:
    """What a plugin's ``setup(api)`` uses to register its contributions."""

    def __init__(
        self,
        plugin: Plugin,
        root: Path | None,
        services: Mapping[str, Any],
        options: Mapping[str, Any],
    ):
        self.name = plugin.name
        self.version = plugin.version
        self.root = root
        self.options: dict[str, Any] = dict(options)
        self._services = services
        self._tools: list[Tool] = []
        self._prompts_text: list[str] = []
        self._handlers: list[tuple[str, Callable[..., Any]]] = []
        self._listeners: list[EventListener] = []
        self._skills: list[Skill] = []
        self._templates: list[PromptTemplate] = []
        self._agents: list[AgentDefinition] = []
        self._mcp: dict[str, dict[str, Any]] = {}
        # Values taken from environment variables, per server, to keep out of messages.
        self._secrets: dict[str, set[str]] = {}
        self._checks: list[tuple[str, Callable[[], Any]]] = []
        self._closers: list[Callable[[], Any]] = []
        self._diagnostics: list[str] = []

    def __repr__(self) -> str:
        return f"PluginAPI({self.name!r})"

    def service(self, name: str, default: Any = _MISSING) -> Any:
        """An object the application passed in ``load_plugins(..., services={name: ...})``."""
        if name in self._services:
            return self._services[name]
        if default is not _MISSING:
            return default
        raise ConfigurationError(
            f"Plugin {self.name!r} needs the service {name!r}; pass it as"
            f" load_plugins(..., services={{{name!r}: ...}})"
        )

    def add_tool(self, tool: Tool | Callable[..., Any]) -> Tool:
        """Register a Tool, or a function that ``@tool`` turns into one."""
        made = tool if isinstance(tool, Tool) else function_tool(tool)
        if any(t.name == made.name for t in self._tools):
            raise ConfigurationError(f"Plugin {self.name!r} registers the tool {made.name!r} twice")
        self._tools.append(made)
        return made

    def task_scope(self) -> TaskScope:
        """Create owned asynchronous work, automatically closed with this plugin.

        Register resources needed by that work before creating the scope: close
        callbacks run in reverse registration order.
        """
        scope = TaskScope()
        self.on_close(scope.aclose)
        return scope

    def add_system_prompt(self, text: str) -> None:
        """Append instructions to the system prompt of agents built from this set."""
        if text.strip():
            self._prompts_text.append(text.strip())

    def on(self, hook: str, handler: F | None = None) -> Any:
        """Attach a handler to a hook slot; also usable as ``@api.on("before_tool_call")``."""
        if hook not in HOOK_NAMES:
            raise ConfigurationError(f"Unknown hook {hook!r}; hooks are {', '.join(HOOK_NAMES)}")

        def register(function: F) -> F:
            _require_callable(function, f"the {hook} handler of plugin {self.name!r}")
            self._handlers.append((hook, function))
            return function

        return register if handler is None else register(handler)

    def subscribe(self, listener: EventListener) -> None:
        """Receive the events of agents built from this set; failures are reported, not raised."""
        _require_callable(listener, f"an event listener of plugin {self.name!r}")
        self._listeners.append(listener)

    def _path(self, path: str | os.PathLike[str]) -> Path:
        resolved = Path(path).expanduser()
        if resolved.is_absolute():
            return resolved
        if self.root is None:
            raise ConfigurationError(
                f"Plugin {self.name!r} has no root directory; pass an absolute path"
            )
        return self.root / resolved

    def add_skills(self, path: str | os.PathLike[str]) -> None:
        """Skills from a SKILL.md file or a directory, relative to the plugin root."""
        self._skills.extend(load_skills(self._path(path), self.name, self._diagnostics))

    def add_prompts(self, path: str | os.PathLike[str]) -> None:
        """Prompt templates from a .md file or the .md files of a directory."""
        self._templates.extend(
            load_prompt_templates(self._path(path), self.name, self._diagnostics)
        )

    def add_agents(self, path: str | os.PathLike[str]) -> None:
        """Subagent definitions from a .md file or the .md files of a directory."""
        self._agents.extend(load_agent_definitions(self._path(path), self.name, self._diagnostics))

    def add_agent(self, definition: AgentDefinition) -> None:
        """A subagent defined in code, which can name its own provider and options."""
        self._agents.append(replace(definition, plugin=self.name))

    def add_mcp_server(self, name: str, config: Mapping[str, Any]) -> None:
        """An MCP server in ``mcp.json`` form; it connects when the set opens.

        ``${PLUGIN_ROOT}``, ``${PYTHON}`` (this interpreter) and environment variables
        are expanded in transport strings. `call_metadata` is a literal JSON object
        snapshotted at registration and sent separately from tool arguments.
        """
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ConfigurationError(
                f"MCP server name {name!r} may only use letters, digits, _ and -"
            )
        if name in self._mcp:
            raise ConfigurationError(
                f"Plugin {self.name!r} registers the MCP server {name!r} twice"
            )
        self._mcp[name] = self._mcp_config(name, config)

    def add_check(self, check: Callable[[], Any], name: str | None = None) -> None:
        """A self-check run by ``PluginSet.check()``: raising or returning False fails it."""
        _require_callable(check, f"a check of plugin {self.name!r}")
        self._checks.append((str(name or getattr(check, "__name__", "check")), check))

    def on_close(self, callback: Callable[[], Any]) -> None:
        """Run when the set closes (sync or async), in reverse registration order."""
        _require_callable(callback, f"a close callback of plugin {self.name!r}")
        self._closers.append(callback)

    def _mcp_config(self, name: str, config: Mapping[str, Any]) -> dict[str, Any]:
        from ..mcp import _copy_call_metadata

        if not isinstance(config, Mapping):
            raise ConfigurationError(f"MCP server {name!r}: the configuration must be an object")
        unknown = sorted(set(config) - _MCP_KEYS)
        if unknown:
            self._diagnostics.append(
                f"plugin {self.name!r}, MCP server {name!r}: ignoring unsupported"
                f" settings {', '.join(unknown)}"
            )
        kind = config.get("type") or (
            "stdio" if "command" in config else "http" if "url" in config else None
        )
        if kind == "sse":
            raise ConfigurationError(f"MCP server {name!r}: the SSE transport is not supported")
        if kind not in {"stdio", "http", "streamable-http"}:
            raise ConfigurationError(f"MCP server {name!r} needs a command or a url")
        result: dict[str, Any] = {"type": "stdio" if kind == "stdio" else "http"}
        if "process_scope" in config:
            if kind != "stdio" or type(config["process_scope"]) is not bool:
                raise ConfigurationError(
                    f"MCP server {name!r}: process_scope must be a boolean for stdio"
                )
            result["process_scope"] = config["process_scope"]
        if config.get("call_metadata") is not None:
            result["call_metadata"] = _copy_call_metadata(config["call_metadata"])
        required = config.get("required_capabilities", [])
        if not isinstance(required, list) or any(
            not isinstance(cap, str)
            or cap
            not in {
                "sampling",
                "sampling.tools",
                "sampling.host_state",
                "sampling.profiles",
                "sampling.delta",
                "elicitation.form",
            }
            for cap in required
        ):
            raise ConfigurationError(f"MCP server {name!r}: invalid required_capabilities")
        result["required_capabilities"] = list(required)
        result["enabled"] = config.get("enabled", True) is not False
        if kind == "stdio":
            command = config.get("command")
            args = config.get("args", [])
            if not isinstance(command, str) or not command:
                raise ConfigurationError(f"MCP server {name!r}: command must be a string")
            if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                raise ConfigurationError(f"MCP server {name!r}: args must be a list of strings")
            result["command"] = self._expand(name, command, home=True)
            result["args"] = [self._expand(name, a, home=True) for a in args]
            if config.get("cwd") is not None:
                result["cwd"] = self._expand(name, str(config["cwd"]), home=True)
            if config.get("env") is not None:
                result["env"] = self._expand_map(name, config["env"], "env")
        else:
            url = config.get("url")
            if not isinstance(url, str) or not url:
                raise ConfigurationError(f"MCP server {name!r}: url must be a string")
            result["url"] = _printable(name, "url", self._expand(name, url))
            if config.get("headers") is not None:
                headers = self._expand_map(name, config["headers"], "headers")
                result["headers"] = {
                    key: _printable(name, f"header {key!r}", value)
                    for key, value in headers.items()
                }
        return result

    def _expand_map(self, server: str, value: Any, field: str) -> dict[str, str]:
        if not isinstance(value, Mapping) or not all(isinstance(v, str) for v in value.values()):
            raise ConfigurationError(f"MCP server {server!r}: {field} must map names to strings")
        return {str(k): self._expand(server, v) for k, v in value.items()}

    def _expand(self, server: str, text: str, *, home: bool = False) -> str:
        def substitute(match: re.Match[str]) -> str:
            variable = match.group(1)
            if variable == "PLUGIN_ROOT":
                if self.root is None:
                    raise ConfigurationError(
                        f"MCP server {server!r}: ${{PLUGIN_ROOT}} needs a plugin directory"
                    )
                return str(self.root)
            if variable == "PYTHON":
                return sys.executable
            if variable not in os.environ:
                raise ConfigurationError(
                    f"MCP server {server!r}: environment variable {variable} is not set"
                )
            value = os.environ[variable]
            if len(value) >= 4:  # masking shorter values would garble messages
                self._secrets.setdefault(server, set()).add(value)
            return value

        expanded = _VARIABLE.sub(substitute, text)
        return os.path.expanduser(expanded) if home and expanded.startswith("~/") else expanded

    def _load_conventions(self) -> None:
        assert self.root is not None
        if (self.root / "skills").is_dir():
            self.add_skills("skills")
        if (self.root / "prompts").is_dir():
            self.add_prompts("prompts")
        if (self.root / "agents").is_dir():
            self.add_agents("agents")
        config_file = self.root / "mcp.json"
        if config_file.is_file():
            try:
                servers = json.loads(config_file.read_text(encoding="utf-8")).get("mcpServers", {})
                if not isinstance(servers, dict):
                    raise ValueError("mcpServers must be an object")
            except (OSError, ValueError, AttributeError) as exc:
                self._diagnostics.append(f"{config_file}: {exc}")
                return
            for name, config in servers.items():
                try:
                    self.add_mcp_server(name, config)
                except ConfigurationError as exc:
                    self._diagnostics.append(f"{config_file}: {exc}")


class PluginSet:
    """Plugins loaded together; open it with ``async with`` (or ``with`` in plain scripts)."""

    def __init__(
        self,
        sources: Iterable[str | os.PathLike[str] | Plugin] | str | os.PathLike[str] | Plugin,
        *,
        services: Mapping[str, Any] | None = None,
        options: Mapping[str, Mapping[str, Any]] | None = None,
        on_error: Callable[[PluginFailure], Any] | None = None,
        strict: bool = False,
        mcp_callbacks: Mapping[tuple[str, str], MCPCallbacks] | None = None,
    ):
        if isinstance(sources, (str, os.PathLike, Plugin)):
            sources = [sources]
        self._sources = list(sources)
        self._services = dict(services or {})
        self._options = {name: dict(value) for name, value in (options or {}).items()}
        self._on_error = on_error or log_plugin_failure
        self._strict = strict
        self._mcp_callbacks = dict(mcp_callbacks or {})
        self._state = "new"
        self._apis: list[PluginAPI] = []
        self._closing: asyncio.Event | None = None
        self._servers: list[asyncio.Task[None]] = []
        self._connected_servers: set[str] = set()
        self._mcp_checks: dict[str, Callable[[], Awaitable[Any]]] = {}
        self.plugins: list[Plugin] = []
        self.tools: list[Tool] = []
        self.skills: list[Skill] = []
        self.prompts: list[PromptTemplate] = []
        self.agents: list[AgentDefinition] = []
        self.diagnostics: list[str] = []

    def __repr__(self) -> str:
        return f"PluginSet({[p.name for p in self.plugins]!r}, {self._state})"

    async def __aenter__(self) -> PluginSet:
        return await self.open()

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

    def __enter__(self) -> PluginSet:
        return run_sync(self.open())

    def __exit__(self, *args: Any) -> None:
        run_sync(self.aclose())

    async def open(self) -> PluginSet:
        """Load every plugin, run its setup and connect its MCP servers."""
        if self._state != "new":
            raise ConfigurationError("A PluginSet can be opened only once")
        self._state = "opening"
        try:
            await self._load()
            if self._strict and self.diagnostics:
                raise ConfigurationError(
                    "Strict plugin loading failed: " + "; ".join(self.diagnostics)
                )
        except BaseException:
            await self._shutdown()
            self._state = "closed"
            raise
        self._state = "open"
        for message in self.diagnostics:
            warnings.warn(message, PluginWarning, stacklevel=2)
        return self

    async def aclose(self) -> None:
        """Disconnect MCP servers and run the plugins' close callbacks."""
        if self._state in {"new", "closed"}:
            self._state = "closed"
            return
        await self._shutdown()
        self._state = "closed"

    async def _load(self) -> None:
        resolved = [_resolve(source) for source in self._sources]
        seen: set[str] = set()
        for plugin in resolved:
            plugin.requires = _names(plugin.requires)
            if plugin.root is not None:
                requirement_root = Path(plugin.root).expanduser().resolve()
                plugin.requires = _names(
                    (*plugin.requires, *_directory_requirements(requirement_root))
                )
            require_features(plugin.requires, where=f"Plugin {plugin.name!r}")
            if plugin.name in seen:
                raise ConfigurationError(f"Two plugins are named {plugin.name!r}")
            seen.add(plugin.name)
        unknown = sorted(set(self._options) - seen)
        if unknown:
            raise ConfigurationError(f"Options given for plugins that are not loaded: {unknown}")
        for plugin in resolved:
            root = Path(plugin.root).expanduser().resolve() if plugin.root is not None else None
            if root is not None and not root.is_dir():
                raise ConfigurationError(f"Plugin {plugin.name!r}: {root} is not a directory")
            api = PluginAPI(plugin, root, self._services, self._options.get(plugin.name, {}))
            self._apis.append(api)
            if root is not None:
                api._load_conventions()
            if plugin.setup is not None:
                try:
                    await invoke(plugin.setup, api)
                except Exception as exc:
                    exc.add_note(f"while setting up plugin {plugin.name!r} ({plugin.source})")
                    raise
            self.plugins.append(replace(plugin, root=root))
        self._merge()
        servers = {(api.name, name) for api in self._apis for name in api._mcp}
        if set(self._mcp_callbacks) - servers:
            raise ConfigurationError("MCP callbacks name an unknown (plugin, server) pair")
        if any(not isinstance(value, MCPCallbacks) for value in self._mcp_callbacks.values()):
            raise ConfigurationError("mcp_callbacks values must be MCPCallbacks runtime objects")
        for api in self._apis:
            for name, config in api._mcp.items():
                if not config["enabled"]:
                    continue
                callbacks = self._mcp_callbacks.get((api.name, name))
                available = set(callbacks.capabilities) if callbacks else set()
                missing = set(config["required_capabilities"]) - available
                if missing:
                    raise ConfigurationError(
                        f"MCP server {name!r} lacks host capabilities: {sorted(missing)}"
                    )
        await self._connect_servers()

    def _merge(self) -> None:
        owners: dict[str, str] = {}
        for api in self._apis:
            self.diagnostics.extend(api._diagnostics)
            for tool in api._tools:
                if tool.name in owners:
                    raise ConfigurationError(
                        f"Plugins {owners[tool.name]!r} and {api.name!r} both provide the tool"
                        f" {tool.name!r}"
                    )
                owners[tool.name] = api.name
                self.tools.append(tool)
            for skill in api._skills:
                first = next((s for s in self.skills if s.name == skill.name), None)
                if first is None:
                    self.skills.append(skill)
                elif first.path != skill.path:
                    self.diagnostics.append(
                        f"skill {skill.name!r} of plugin {api.name!r} ({skill.path}) is hidden by"
                        f" the one of plugin {first.plugin!r} ({first.path})"
                    )
            for template in api._templates:
                first_template = next((t for t in self.prompts if t.name == template.name), None)
                if first_template is None:
                    self.prompts.append(template)
                else:
                    self.diagnostics.append(
                        f"prompt template {template.name!r} of plugin {api.name!r} is hidden by the"
                        f" one of plugin {first_template.plugin!r}"
                    )
            for definition in api._agents:
                first_agent = next((a for a in self.agents if a.name == definition.name), None)
                if first_agent is None:
                    self.agents.append(definition)
                else:
                    self.diagnostics.append(
                        f"agent {definition.name!r} of plugin {api.name!r} is hidden by the one of"
                        f" plugin {first_agent.plugin!r}"
                    )
        servers: dict[str, str] = {}
        for api in self._apis:
            for name in api._mcp:
                key = name.replace("-", "_")
                if key in servers:
                    raise ConfigurationError(
                        f"MCP server {name!r} of plugin {api.name!r} conflicts with a server of"
                        f" plugin {servers[key]!r}"
                    )
                servers[key] = api.name

    async def _connect_servers(self) -> None:
        self._closing = asyncio.Event()
        pending: list[tuple[PluginAPI, str, asyncio.Future[list[Tool]]]] = []
        for api in self._apis:
            for name, config in api._mcp.items():
                if not config["enabled"]:
                    continue
                ready: asyncio.Future[list[Tool]] = asyncio.get_running_loop().create_future()
                self._servers.append(
                    asyncio.create_task(
                        self._serve(api.name, name, config, ready, api._secrets.get(name))
                    )
                )
                pending.append((api, name, ready))
        owners = {t.name: "a plugin tool" for t in self.tools}
        for api, name, ready in pending:
            try:
                tools = await ready
            except ConfigurationError:
                # All connections were started together. Retrieve other failures too
                # before aborting so no readiness future is left with an unhandled error.
                await asyncio.gather(*(future for _, _, future in pending), return_exceptions=True)
                raise
            except Exception as exc:
                reason = _redact(f"{type(exc).__name__}: {exc}", api._secrets.get(name))
                self.diagnostics.append(
                    f"plugin {api.name!r}: MCP server {name!r} did not connect: {reason}"
                )
                continue
            for tool in tools:
                if tool.name in owners:
                    raise ConfigurationError(
                        f"MCP tool {tool.name!r} of server {name!r} conflicts with {owners[tool.name]}"
                    )
                owners[tool.name] = f"MCP server {name!r}"
                self.tools.append(tool)

    async def _serve(
        self,
        plugin: str,
        name: str,
        config: dict[str, Any],
        ready: asyncio.Future[list[Tool]],
        secrets: set[str] | None,
    ) -> None:
        # Each server lives in its own task, so its connection is entered and exited in one
        # task even when the set is opened and closed from different ones (``with`` blocks).
        from ..mcp import connect_http, connect_stdio

        prefix = f"mcp__{name.replace('-', '_')}_"
        assert self._closing is not None

        def on_session(session: Any) -> None:
            self._mcp_checks[name] = session.send_ping

        try:
            callbacks = self._mcp_callbacks.get((plugin, name))
            callback_options: dict[str, Any] = (
                {"callbacks": callbacks, "server_name": name, "plugin_name": plugin}
                if callbacks
                else {}
            )
            if config["type"] == "stdio":
                connection = connect_stdio(
                    config["command"],
                    config["args"],
                    env=config.get("env"),
                    cwd=config.get("cwd"),
                    process_scope=config.get("process_scope", False),
                    prefix=prefix,
                    call_metadata=config.get("call_metadata"),
                    _on_session=on_session,
                    **callback_options,
                )
            else:
                connection = connect_http(
                    config["url"],
                    headers=config.get("headers"),
                    prefix=prefix,
                    call_metadata=config.get("call_metadata"),
                    _on_session=on_session,
                    **callback_options,
                )
            async with connection as tools:
                self._connected_servers.add(name)
                ready.set_result(tools)
                await self._closing.wait()
        except asyncio.CancelledError:
            if not ready.done():
                ready.cancel()
            raise
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)
            else:
                # The connection dropped later; report it without values from the environment.
                reason = _redact(f"{type(exc).__name__}: {exc}", secrets)
                await self._report(
                    PluginFailure(plugin, f"MCP server {name}", ConnectionError(reason))
                )
        finally:
            self._connected_servers.discard(name)
            self._mcp_checks.pop(name, None)

    async def _shutdown(self) -> None:
        if self._closing is not None:
            self._closing.set()
        if self._servers:
            await asyncio.gather(*self._servers, return_exceptions=True)
            self._servers.clear()
        for api in reversed(self._apis):
            for callback in reversed(api._closers):
                try:
                    await invoke(callback)
                except Exception as exc:
                    await self._report(PluginFailure(api.name, "on_close", exc))

    async def _report(self, failure: PluginFailure) -> None:
        try:
            await invoke(self._on_error, failure)
        except Exception:
            logger.exception("on_error raised while reporting: %s", failure)

    def _require_open(self) -> None:
        if self._state != "open":
            raise ConfigurationError(
                "Open the PluginSet first: `async with load_plugins(...) as plugins:`"
            )

    def system_prompt(self, base: str = "") -> str:
        """`base`, then each plugin's instructions, then the listing of available skills."""
        self._require_open()
        parts = [base, *(text for api in self._apis for text in api._prompts_text)]
        parts.append(format_skills_for_prompt(self.skills, READ_SKILL_TOOL))
        return "\n\n".join(p for p in parts if p)

    def hooks(self, base: Hooks | None = None) -> Hooks:
        """`base` (the application's hooks) combined with every plugin handler."""
        self._require_open()
        handlers = [(api.name, hook, h) for api in self._apis for hook, h in api._handlers]
        return compose_hooks(base, handlers, self._report)

    def skill_tool(self) -> Tool | None:
        """The ``read_skill`` tool, or None when no skill is visible to the model."""
        self._require_open()
        return _skill_reader(self.skills)

    def subagent_tool(
        self,
        *,
        tools: Sequence[Tool] = (),
        hooks: Hooks | None = None,
        provider: Any = None,
        stream_fn: Callable[..., Any] | None = None,
        model: str | ModelInfo | None = None,
        limits: RunLimits | None = None,
    ) -> Tool | None:
        """The ``subagent`` tool, or None without agent definitions.

        `tools` are the tools subagents may be given; `hooks` and `provider` (or
        `stream_fn`) are the main agent's, and `limits` (the main agent's RunLimits) apply
        to each subagent run. A subagent uses its definition's model, else `model`, else
        the calling agent's model at the time of the call.
        """
        self._require_open()
        if not self.agents:
            return None
        available = {t.name: t for t in tools if t.name != SUBAGENT_TOOL}
        for definition in self.agents:
            missing = [n for n in definition.tools or [] if n not in available]
            if missing:
                message = (
                    f"agent {definition.name!r} of plugin {definition.plugin!r} asks for tools"
                    f" that are not available: {', '.join(missing)}"
                )
                if message not in self.diagnostics:
                    self.diagnostics.append(message)
                warnings.warn(message, PluginWarning, stacklevel=3)
        shared_hooks = hooks if hooks is not None else self.hooks()

        def skills_prompt(chosen: list[Tool]) -> str:
            if any(t.name == READ_SKILL_TOOL for t in chosen):
                return format_skills_for_prompt(self.skills, READ_SKILL_TOOL)
            return ""

        def make_agent(definition: AgentDefinition, main_model: Any) -> Agent:
            return build_subagent(
                definition,
                tools=available,
                provider=provider,
                stream_fn=stream_fn,
                model=model or main_model or "mock",
                limits=limits,
                hooks=shared_hooks,
                skills_prompt=skills_prompt,
            )

        return subagent_tool(self.agents, make_agent=make_agent)

    def agent(
        self,
        *,
        system_prompt: str = "",
        tools: Sequence[Tool] = (),
        hooks: Hooks | None = None,
        **kwargs: Any,
    ) -> Agent:
        """An Agent with everything the plugins contribute added to your own configuration.

        Takes the same arguments as Agent. Your system prompt comes first, your hooks run
        before the plugins' handlers, and your tools must not share names with theirs.
        """
        self._require_open()
        base = copy(hooks) if hooks else Hooks()
        for name in ("get_api_key", "on_payload", "on_response", "on_provider_stream_event"):
            value = kwargs.pop(name, None)
            if value is not None:
                setattr(base, name, value)
        composed = self.hooks(base)
        chosen: dict[str, Tool] = {}
        for tool in [*tools, *self.tools]:
            if tool.name in chosen:
                raise ConfigurationError(f"Two tools are named {tool.name!r}")
            chosen[tool.name] = tool
        extras = [self.skill_tool()]
        extras.append(
            self.subagent_tool(
                tools=[*chosen.values(), *(t for t in extras if t)],
                hooks=composed,
                provider=kwargs.get("provider"),
                stream_fn=kwargs.get("stream_fn"),
                limits=kwargs.get("limits"),
            )
        )
        for extra in extras:
            if extra is None:
                continue
            if extra.name in chosen:
                raise ConfigurationError(f"Two tools are named {extra.name!r}")
            chosen[extra.name] = extra
        agent = Agent(
            system_prompt=self.system_prompt(system_prompt),
            tools=list(chosen.values()),
            hooks=composed,
            **kwargs,
        )
        for api in self._apis:
            for listener in api._listeners:
                agent.subscribe(isolated_listener(api.name, listener, self._report))
        return agent

    def expand(self, text: str) -> str:
        """Expand ``/skill:name args`` and ``/template args`` as Pi does; other text is unchanged."""
        self._require_open()
        return expand_prompt_template(expand_skill_command(text, self.skills), self.prompts)

    async def check(self) -> list[CheckResult]:
        """Run every plugin's self-checks, in order."""
        self._require_open()
        results = []
        for api in self._apis:
            for name, check in api._checks:
                try:
                    value = await invoke(check)
                except Exception as exc:
                    results.append(
                        CheckResult(api.name, name, False, f"{type(exc).__name__}: {exc}")
                    )
                else:
                    passed = value is not False
                    results.append(
                        CheckResult(api.name, name, passed, "" if passed else "returned False")
                    )
        return results

    async def readiness(
        self,
        *,
        required_tools: Iterable[str] = (),
        required_skills: Iterable[str] = (),
        required_mcp_servers: Iterable[str] = (),
        required_checks: Iterable[tuple[str, str]] = (),
        required_mcp_capabilities: Mapping[str, Iterable[str]] | None = None,
        mcp_timeout: float = 5.0,
    ) -> ReadinessResult:
        """Run self-checks and compare available resources with explicit requirements.

        Check identities are `(plugin_name, check_name)` pairs. An empty check list
        is acceptable only when no checks are required. Diagnostics remain visible
        but do not themselves fail readiness; use `strict=True` to reject them on load.
        This checks the tools contributed by plugins, not tools later passed to Agent.
        MCP sessions are pinged concurrently, with `mcp_timeout` seconds per server.
        """
        if (
            isinstance(mcp_timeout, bool)
            or not isinstance(mcp_timeout, (int, float))
            or not math.isfinite(mcp_timeout)
            or mcp_timeout <= 0
        ):
            raise ConfigurationError("mcp_timeout must be finite and positive")
        checks = tuple(await self.check()) if self._state == "open" else ()

        async def probe(name: str, ping: Callable[[], Awaitable[Any]]) -> None:
            try:
                async with asyncio.timeout(mcp_timeout):
                    await ping()
            except Exception:
                healthy = False
            else:
                healthy = True
            # Closing may have removed the session while the ping was pending.
            if self._state == "open" and self._mcp_checks.get(name) is ping:
                if healthy:
                    self._connected_servers.add(name)
                else:
                    self._connected_servers.discard(name)

        if self._state == "open":
            await asyncio.gather(*(probe(name, ping) for name, ping in self._mcp_checks.items()))
        loaded = self._state == "open"
        tools = {tool.name for tool in self.tools} if loaded else set()
        skills = {skill.name for skill in self.skills} if loaded else set()
        if loaded and any(not skill.disable_model_invocation for skill in self.skills):
            tools.add(READ_SKILL_TOOL)
        if loaded and self.agents:
            tools.add(SUBAGENT_TOOL)
        servers = self._connected_servers if loaded else set()
        executed = {(check.plugin, check.name) for check in checks}
        return ReadinessResult(
            loaded=loaded,
            plugins=tuple(plugin.name for plugin in self.plugins) if loaded else (),
            diagnostics=tuple(self.diagnostics),
            missing_tools=tuple(sorted(set(required_tools) - tools)),
            missing_skills=tuple(sorted(set(required_skills) - skills)),
            missing_mcp_servers=tuple(sorted(set(required_mcp_servers) - servers)),
            missing_checks=tuple(sorted(set(required_checks) - executed)),
            checks=checks,
            missing_mcp_capabilities=tuple(
                sorted(
                    (server, cap)
                    for server, caps in (required_mcp_capabilities or {}).items()
                    for cap in caps
                    if cap not in self.mcp_capabilities.get(server, ())
                )
            ),
        )

    @property
    def mcp_capabilities(self) -> dict[str, tuple[str, ...]]:
        """Host grants for servers available at connection or the latest readiness check."""
        return {
            name: callbacks.capabilities
            for (_, name), callbacks in self._mcp_callbacks.items()
            if self._state == "open" and name in self._connected_servers
        }


def load_plugins(
    sources: Iterable[str | os.PathLike[str] | Plugin] | str | os.PathLike[str] | Plugin,
    *,
    services: Mapping[str, Any] | None = None,
    options: Mapping[str, Mapping[str, Any]] | None = None,
    on_error: Callable[[PluginFailure], Any] | None = None,
    strict: bool = False,
    mcp_callbacks: Mapping[tuple[str, str], MCPCallbacks] | None = None,
) -> PluginSet:
    """Plugins to load, in order; nothing runs until the set is opened.

    A source is the name of an installed plugin, a path to a plugin directory or a
    ``.py`` file, or a :class:`Plugin`. `services` are objects plugins may ask for with
    ``api.service(name)``; `options` maps a plugin name to the options it reads from
    ``api.options``. `on_error` (sync or async) receives a :class:`PluginFailure` for each
    plugin handler that fails; by default failures are logged.
    `strict=True` rejects any loading diagnostics and closes resources on failure.
    Self-checks and required capabilities are evaluated separately by `readiness()`.
    """
    return PluginSet(
        sources,
        services=services,
        options=options,
        on_error=on_error,
        strict=strict,
        mcp_callbacks=mcp_callbacks,
    )


def _printable(server: str, what: str, value: str) -> str:
    """Reject control characters, such as the trailing newline of a pasted token: HTTP
    clients reject them with an error that quotes the whole value."""
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ConfigurationError(
            f"MCP server {server!r}: the {what} contains a control character"
            " (often a trailing newline in an environment variable)"
        )
    return value


def _redact(text: str, secrets: set[str] | None) -> str:
    for secret in sorted(secrets or (), key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


def _require_callable(value: Any, what: str) -> None:
    if not callable(value):
        raise ConfigurationError(f"{what} must be callable, not {type(value).__name__}")


def _looks_like_path(text: str) -> bool:
    return (
        text.startswith((".", "~"))
        or "/" in text
        or "\\" in text
        or text.endswith(".py")
        or os.path.isabs(text)
    )


def _resolve(source: Any) -> Plugin:
    if isinstance(source, Plugin):
        return replace(source)
    if isinstance(source, os.PathLike) or (isinstance(source, str) and _looks_like_path(source)):
        return _from_path(Path(source))
    if isinstance(source, str):
        return _from_entry_point(source)
    raise TypeError(
        f"A plugin source is an installed plugin's name, a path or a Plugin,"
        f" not {type(source).__name__}"
    )


def _module_name(path: Path) -> str:
    slug = re.sub(r"\W", "_", path.stem)
    return f"_pi_plugin_{slug}_{hashlib.sha1(str(path).encode()).hexdigest()[:8]}"


def _from_path(path: Path) -> Plugin:
    path = path.expanduser().resolve()
    if path.is_dir():
        requirements = _directory_requirements(path)
        require_features(requirements, where=f"Plugin {path.name!r}")
        code = path / "plugin.py"
        if not code.is_file():
            return Plugin(path.name, None, path, source=str(path), requires=requirements)
        # The directory becomes a package, so plugin.py can import its neighbours
        # with relative imports (``from .ops import dedup``).
        package = _module_name(path)
        if package not in sys.modules:
            spec = importlib.machinery.ModuleSpec(package, None, is_package=True)
            spec.submodule_search_locations = [str(path)]
            sys.modules[package] = importlib.util.module_from_spec(spec)
        try:
            module = importlib.import_module(f"{package}.plugin")
        except Exception as exc:
            exc.add_note(f"while importing plugin {path.name!r} from {code}")
            raise
        return Plugin(
            path.name,
            _setup_of(module, code),
            path,
            _version_of(module),
            str(path),
            _names((*requirements, *_requirements_of(module))),
        )
    if path.is_file() and path.suffix == ".py":
        name = _module_name(path)
        loaded = sys.modules.get(name)
        if loaded is None:
            file_spec = importlib.util.spec_from_file_location(name, path)
            assert file_spec is not None and file_spec.loader is not None
            loaded = importlib.util.module_from_spec(file_spec)
            sys.modules[name] = loaded
            try:
                file_spec.loader.exec_module(loaded)
            except BaseException as exc:
                sys.modules.pop(name, None)
                exc.add_note(f"while importing plugin {path.stem!r} from {path}")
                raise
        return Plugin(
            path.stem,
            _setup_of(loaded, path),
            None,
            _version_of(loaded),
            str(path),
            _requirements_of(loaded),
        )
    if not path.exists():
        raise ConfigurationError(f"Plugin path {path} does not exist")
    raise ConfigurationError(f"A plugin path must be a directory or a .py file: {path}")


def _setup_of(module: ModuleType, where: Path | str) -> Callable[[PluginAPI], Any]:
    setup = getattr(module, "setup", None)
    if not callable(setup):
        raise ConfigurationError(f"{where} defines no setup(api) function")
    return setup


def _version_of(module: ModuleType) -> str | None:
    version = getattr(module, "__version__", None)
    return version if isinstance(version, str) else None


def _requirements_of(target: Any) -> tuple[str, ...]:
    return _names(getattr(target, "__requires__", ()))


def _directory_requirements(path: Path) -> tuple[str, ...]:
    manifest = path / "pi-plugin.json"
    if not manifest.exists():
        return ()
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ConfigurationError(f"Invalid plugin manifest: {manifest}") from exc
    if not isinstance(data, dict) or set(data) - {"requires"}:
        raise ConfigurationError(
            f"Plugin manifest must be an object with only 'requires': {manifest}"
        )
    return _names(data.get("requires", ()))


def _from_entry_point(name: str) -> Plugin:
    matches = [ep for ep in entry_points(group=ENTRY_POINT_GROUP) if ep.name == name]
    if not matches:
        installed = ", ".join(p.name for p in discover_plugins()) or "none"
        raise ConfigurationError(
            f"No installed plugin is named {name!r} (installed: {installed}); for a local"
            f" directory pass a path such as './{name}'"
        )
    if len(matches) > 1:
        sources = ", ".join(ep.value for ep in matches)
        raise ConfigurationError(
            f"Several installed packages provide the plugin {name!r}: {sources}"
        )
    ep: EntryPoint = matches[0]
    try:
        target = ep.load()
    except Exception as exc:
        exc.add_note(f"while importing plugin {name!r} (entry point {ep.value})")
        raise
    version = ep.dist.version if ep.dist else None
    package_dir = _package_dir(ep.module)
    source = f"entry point {ep.value}"
    if isinstance(target, Plugin):
        root = target.root if target.root is not None else package_dir
        return replace(
            target, name=name, root=root, version=target.version or version, source=source
        )
    if isinstance(target, ModuleType):
        setup = getattr(target, "setup", None)
        if setup is None and package_dir is None:
            raise ConfigurationError(f"{source} has no setup(api) function and no directory")
        return Plugin(name, setup, package_dir, version, source, _requirements_of(target))
    if callable(target):
        return Plugin(name, target, package_dir, version, source, _requirements_of(target))
    raise ConfigurationError(f"{source} is not a module, a setup function or a Plugin")


def _package_dir(module_name: str) -> Path | None:
    """The directory of the package that holds `module_name` (the module itself, if it is
    a package). A top-level single-file module has none: its folder is site-packages."""
    module = sys.modules.get(module_name)
    if module is not None and not hasattr(module, "__path__"):
        module = sys.modules.get(module.__name__.rpartition(".")[0])
    if module is None or not hasattr(module, "__path__") or not module.__file__:
        return None
    return Path(module.__file__).parent


def _skill_reader(skills: list[Skill]) -> Tool | None:
    visible = {s.name: s for s in skills if not s.disable_model_invocation}
    if not visible:
        return None
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "enum": list(visible), "description": "The skill's name"},
            "path": {
                "type": "string",
                "description": "A file in the skill's directory, relative to it; defaults to"
                " SKILL.md. A directory lists its files.",
            },
        },
        "required": ["name"],
        "additionalProperties": False,
    }

    async def read_skill(args: dict[str, Any], context: ToolContext) -> ToolResult:
        # Files are at most 256 KiB, so reading them inline does not stall the loop.
        skill = visible[args["name"]]
        base = skill.base_dir.resolve()
        relative = args.get("path") or "SKILL.md"
        outside = ToolResult.text(f"{relative} is outside the skill directory", is_error=True)
        # Check the text before touching the filesystem: on Windows, resolving a path such
        # as //host/share would already contact that host.
        anchored = PurePath(relative).anchor or PureWindowsPath(relative).anchor
        if anchored or "\0" in relative:
            return outside
        joined = os.path.normpath(os.path.join(base, relative))
        if os.path.commonpath([joined, str(base)]) != str(base):
            return outside
        target = Path(joined).resolve()
        if not target.is_relative_to(base):  # a symbolic link that leads out
            return outside
        if target.is_file() and target.stat().st_size > MAX_SKILL_FILE_BYTES:
            return ToolResult.text(
                f"{relative} is larger than {MAX_SKILL_FILE_BYTES // 1024} KiB; read it with"
                " another tool",
                is_error=True,
            )
        if target == skill.path.resolve():
            return ToolResult.text(skill_block(skill))
        if target.is_dir():
            entries = sorted(
                f"{p.relative_to(base).as_posix()}{'/' if p.is_dir() else ''}"
                for p in target.iterdir()
                if not p.name.startswith(".")
            )
            return ToolResult.text("\n".join(entries) or "(empty directory)")
        if not target.is_file():
            return ToolResult.text(f"No file {relative} in skill {skill.name}", is_error=True)
        try:
            return ToolResult.text(target.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            return ToolResult.text(f"{relative} is not a UTF-8 text file", is_error=True)

    return Tool(
        READ_SKILL_TOOL,
        "Read a skill's instructions (its SKILL.md) or another file in the skill's directory.",
        schema,
        read_skill,
    )
