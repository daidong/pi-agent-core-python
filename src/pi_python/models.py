"""Explicit, replaceable model capabilities. Catalog prices are snapshot estimates."""

from __future__ import annotations
from collections.abc import Iterable
from copy import deepcopy
from typing import TYPE_CHECKING, Any
from dataclasses import dataclass, field
from importlib.resources import files
import json
from .errors import ConfigurationError, UnsupportedCapabilityError
from .messages import ImageContent, validate_json

if TYPE_CHECKING:
    from .provider import ModelRequest

LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class ModelInfo:
    id: str
    provider: str
    api: str
    name: str
    context_window: int
    max_tokens: int
    reasoning: bool = False
    input: tuple[str, ...] = ("text",)
    thinking_level_map: dict[str, str | None] = field(default_factory=dict)
    cost: dict[str, float] = field(default_factory=dict)
    compat: dict = field(default_factory=dict)
    input_limits: dict = field(default_factory=dict)
    prompt_cache: dict = field(default_factory=dict)
    base_url: str = ""

    def supported_thinking_levels(self) -> tuple[str, ...]:
        if not self.reasoning:
            return ("off",)
        return tuple(
            level
            for level in LEVELS
            if self.thinking_level_map.get(level, "") is not None
            and (level not in {"xhigh", "max"} or level in self.thinking_level_map)
        )

    def clamp_thinking_level(self, level: str) -> str:
        if level not in LEVELS:
            raise ConfigurationError("Unknown thinking level")
        available = self.supported_thinking_levels()
        index = LEVELS.index(level)
        return next(
            (v for v in (*LEVELS[index:], *reversed(LEVELS[:index])) if v in available), "off"
        )

    def provider_effort(self, level: str) -> str:
        level = self.clamp_thinking_level(level)
        return self.thinking_level_map.get(level) or ("none" if level == "off" else level)

    def estimate_cost(self, usage: dict) -> dict[str, float]:
        """USD at captured base rates; excludes tiers, subscriptions and discounts."""
        result = {
            name: usage.get(name, 0) * self.cost.get(source, 0) / 1_000_000
            for name, source in [
                ("input", "input"),
                ("output", "output"),
                ("cache_read", "cacheRead"),
                ("cache_write", "cacheWrite"),
            ]
        }
        result["total"] = sum(result.values())
        return result

    def validate_request(self, request: ModelRequest) -> None:
        if "max_tokens" in request.options:
            value = request.options["max_tokens"]
            if type(value) is not int or not 0 < value <= self.max_tokens:
                raise ConfigurationError(f"max_tokens must be between 1 and {self.max_tokens}")
        images = []
        for message in request.messages:
            content = getattr(message, "content", None)
            if isinstance(content, list):
                images += [b for b in content if isinstance(b, ImageContent)]
        if images and "image" not in self.input:
            raise UnsupportedCapabilityError(f"{self.provider}/{self.id} does not support images")
        limit = self.input_limits.get("images", {}).get("maxPerRequest")
        if limit and len(images) > limit:
            raise UnsupportedCapabilityError("Too many images for model")


class ModelCatalog:
    def __init__(
        self, models: Iterable[ModelInfo] = (), *, provenance: dict[str, Any] | None = None
    ) -> None:
        self._models: dict[tuple[str, str], ModelInfo] = {}
        self._provenance = deepcopy(provenance or {})
        for model in models:
            self.register(model)

    @property
    def provenance(self) -> dict[str, Any]:
        return deepcopy(self._provenance)

    def register(self, model: ModelInfo, *, replace: bool = False) -> None:
        if not isinstance(model, ModelInfo) or not all((model.id, model.provider, model.api)):
            raise ConfigurationError("Invalid model identity")
        if (
            type(model.max_tokens) is not int
            or type(model.context_window) is not int
            or min(model.max_tokens, model.context_window) <= 0
        ):
            raise ConfigurationError("Model limits must be positive integers")
        validate_json(model.compat)
        if any(
            k not in LEVELS or (v is not None and not isinstance(v, str))
            for k, v in model.thinking_level_map.items()
        ):
            raise ConfigurationError("Invalid thinking level map")
        key = (model.provider, model.id)
        if key in self._models and not replace:
            raise ConfigurationError("Model already registered")
        self._models[key] = deepcopy(model)

    def get(self, provider: str, model: str) -> ModelInfo | None:
        return deepcopy(self._models.get((provider, model)))

    def list(self, provider: str | None = None) -> list[ModelInfo]:
        return deepcopy(
            [m for m in self._models.values() if provider is None or m.provider == provider]
        )

    @classmethod
    def bundled(cls) -> ModelCatalog:
        data = json.loads(
            files("pi_python").joinpath("data/models.json").read_text(encoding="utf-8")
        )
        models = [
            ModelInfo(
                id=m["id"],
                provider=m["provider"],
                api=m["api"],
                name=m["name"],
                context_window=m["contextWindow"],
                max_tokens=m["maxTokens"],
                reasoning=m["reasoning"],
                input=tuple(m["input"]),
                thinking_level_map=m.get("thinkingLevelMap", {}),
                cost=m.get("cost", {}),
                compat=m.get("compat", {}),
                input_limits=m.get("inputLimits", {}),
                prompt_cache=m.get("promptCache", {}),
                base_url=m.get("baseUrl", ""),
            )
            for m in data["models"]
        ]
        return cls(models, provenance=data["provenance"])
