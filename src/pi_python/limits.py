from dataclasses import dataclass
import math
from .errors import ConfigurationError


@dataclass(frozen=True)
class RunLimits:
    # Like Pi, no request, tool-call or concurrency limit unless the application sets one.
    max_model_requests: int | None = None
    max_tool_calls: int | None = None
    max_concurrency: int | None = None
    tool_timeout: float | None = None
    run_timeout: float | None = None
    cleanup_timeout: float = 1.0

    def __post_init__(self) -> None:
        for name in ("max_model_requests", "max_tool_calls", "max_concurrency"):
            value = getattr(self, name)
            if value is None:
                continue
            if type(value) is not int or value < (1 if name == "max_concurrency" else 0):
                raise ConfigurationError(f"Invalid {name}")
        for name in ("tool_timeout", "run_timeout", "cleanup_timeout"):
            value = getattr(self, name)
            if value is None and name != "cleanup_timeout":
                continue
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"Invalid {name}")
