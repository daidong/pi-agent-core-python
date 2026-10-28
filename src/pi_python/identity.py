"""JSON client identity; Python product identity, with Pi's attribution mappings."""

from __future__ import annotations

import platform
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Literal, NotRequired, TypedDict
from urllib.parse import urlsplit

from ._version import __version__
from .errors import ConfigurationError


class ClientAttribution(TypedDict):
    name: str
    url: str
    categories: NotRequired[list[str]]


class ClientIdentity(TypedDict, total=False):
    user_agent: Literal["short", "detailed"] | dict[str, str]
    attribution: ClientAttribution | None
    version: str


def validate_identity(identity: Any) -> None:
    if identity is None:
        return
    if not isinstance(identity, dict) or set(identity) - {"user_agent", "attribution", "version"}:
        raise ConfigurationError("Invalid client identity fields")
    mode = identity.get("user_agent", "detailed")
    if not (
        mode in ("short", "detailed")
        or (isinstance(mode, dict) and set(mode) == {"custom"} and isinstance(mode["custom"], str))
    ):
        raise ConfigurationError("user_agent must be short, detailed, or {custom: string}")
    attribution = identity.get("attribution")
    if attribution is not None:
        if (
            not isinstance(attribution, dict)
            or not {"name", "url"} <= attribution.keys()
            or set(attribution) - {"name", "url", "categories"}
            or not all(isinstance(attribution[k], str) for k in ("name", "url"))
            or not isinstance(attribution.get("categories", []), list)
            or not all(isinstance(c, str) for c in attribution.get("categories", []))
        ):
            raise ConfigurationError("Invalid client attribution")
    if "version" in identity and not isinstance(identity["version"], str):
        raise ConfigurationError("identity version must be a string")


def format_user_agent(identity: ClientIdentity | None = None) -> str:
    validate_identity(identity)
    identity = identity or {}
    mode = identity.get("user_agent", "detailed")
    if isinstance(mode, dict):
        return mode["custom"]
    if mode == "short":
        return "pi-python"
    return (
        f"pi-python/{identity.get('version', __version__)} "
        f"({platform.system().lower()}; python/{platform.python_version()}; {platform.machine()})"
    )


def get_identity_headers(
    provider: str, base_url: str, identity: ClientIdentity | None = None
) -> dict[str, str]:
    headers = {"user-agent": format_user_agent(identity)}
    attribution = (identity or {}).get("attribution")
    if attribution:
        host = urlsplit(base_url).hostname
        name = attribution["name"]
        if provider == "openrouter" or host == "openrouter.ai":
            headers.update({"http-referer": attribution["url"], "x-openrouter-title": name})
            if attribution.get("categories"):
                headers["x-openrouter-categories"] = ",".join(attribution["categories"])
        if provider == "nvidia" or host == "integrate.api.nvidia.com":
            headers["x-billing-invoke-origin"] = name[:1].upper() + name[1:]
        if provider in {"opencode", "opencode-go"} or host == "opencode.ai":
            headers["x-opencode-client"] = name
    return headers


def apply_identity(
    headers: Mapping[str, str | None],
    provider: str,
    base_url: str,
    options: dict[str, Any],
) -> dict[str, str | None]:
    """Identity below existing protocol headers, explicit request headers last."""
    identity = options.get("identity")
    validate_identity(identity)
    result: dict[str, str | None] = dict(get_identity_headers(provider, base_url, identity))
    result.update({k.lower(): v for k, v in headers.items()})
    for key, value in (options.get("headers") or {}).items():
        key = key.lower()
        result.pop(key, None)
        result[key] = value
    return deepcopy(result)
