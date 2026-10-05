"""Versioned implementation capabilities, independent of package version strings.

These describe APIs present in this build, not installed optional dependencies or
permissions granted by a host. MCP session capabilities still need negotiation.
"""

from collections.abc import Iterable, Mapping

from ._mcp_host import MCP_FEATURES
from .errors import ConfigurationError

FEATURES = MCP_FEATURES | frozenset(
    {
        "directory-relative-imports",
        "strict-plugin-loading",
        "named-readiness-checks",
        "mcp-call-metadata",
        "structured-tool-results-v1",
        "task-scope-v1",
        "loop-portal-v1",
        "plugin-requires-v1",
    }
)


def _names(required: Iterable[str]) -> tuple[str, ...]:
    if isinstance(required, (str, bytes, Mapping)):
        raise ConfigurationError("Required features must be a collection of nonempty strings")
    try:
        names = tuple(required)
    except TypeError as exc:
        raise ConfigurationError("Required features must be a collection of strings") from exc
    if any(not isinstance(name, str) or not name.strip() for name in names):
        raise ConfigurationError("Required features must be nonempty strings")
    return tuple(sorted(set(names)))


def require_features(required: Iterable[str], *, where: str = "Application") -> None:
    """Raise ConfigurationError listing unavailable APIs. Unknown names fail closed."""
    missing = set(_names(required)) - FEATURES
    if missing:
        raise ConfigurationError(
            f"{where} requires unavailable pi-python features: {sorted(missing)}"
        )
