from dataclasses import replace
import pytest
from pi_python import *
from pi_python.providers import AnthropicProvider, OpenAIProvider, DeepSeekProvider


def test_model_catalog_provenance_copies_registration_and_thinking_clamp():
    catalog = ModelCatalog.bundled()
    assert len(catalog.list()) == 71
    model = catalog.get("openai", "gpt-5.4-mini")
    assert model and model.clamp_thinking_level("minimal") == "low"
    assert model.clamp_thinking_level("max") == "xhigh"
    model.compat["mutated"] = True
    assert "mutated" not in catalog.get("openai", model.id).compat
    catalog.register(replace(model, id="custom"))
    with pytest.raises(ConfigurationError):
        catalog.register(model)
    assert "snapshot" in catalog.provenance["source"]
    assert model.estimate_cost({"input": 1_000_000})["input"] == model.cost["input"]


def test_known_capabilities_validate_and_unknown_models_need_a_record():
    provider = OpenAIProvider(api_key="fixture")
    with pytest.raises(UnsupportedCapabilityError):
        provider.build_request(
            ModelRequest([UserMessage([ImageContent("aGk=", "image/png")])], model="gpt-4")
        )
    with pytest.raises(ConfigurationError):
        provider.build_request(ModelRequest([], model="gpt-4", options={"max_tokens": 10**9}))
    assert "reasoning" not in provider.build_request(ModelRequest([], model="gpt-4"))
    with pytest.raises(ConfigurationError, match="Unknown model openai/custom"):
        provider.build_request(ModelRequest([], model="custom"))
    custom = ModelInfo("custom", "openai", "openai-responses", "Custom", 8000, 1000)
    request = ModelRequest([], model="custom", model_info=custom)
    assert provider.build_request(request)["max_output_tokens"] == 1000
    # A record for another provider is a configuration error, not a silent mismatch.
    with pytest.raises(ConfigurationError, match="is not openai"):
        provider.build_request(
            ModelRequest([], model="custom", model_info=replace(custom, provider="x"))
        )


async def test_agent_accepts_a_model_record_and_keeps_it_across_updates():
    custom = ModelInfo("mine", "openai", "openai-responses", "Mine", 8000, 1000)
    provider = ScriptedProvider([AssistantMessage.text("a"), AssistantMessage.text("b")])
    agent = Agent(provider=provider, model=custom)
    await agent.prompt("one")
    agent.update_config(AgentConfigUpdate(model=replace(custom, max_tokens=500)))
    await agent.prompt("two")
    assert [r.model for r in provider.requests] == ["mine", "mine"]
    assert [r.model_info.max_tokens for r in provider.requests] == [1000, 500]


def test_cache_controls_and_deepseek_profile():
    request = ModelRequest(
        [SystemMessage("system"), UserMessage("go")],
        model="claude-sonnet-4-5",
        options={"session_id": "test", "cache_retention": "long"},
    )
    body = AnthropicProvider(api_key="fixture").build_request(request)
    assert body["system"][0]["cache_control"]["ttl"] == "1h"
    assert body["messages"][-1]["content"][-1]["cache_control"]["ttl"] == "1h"
    request.model = "gpt-5.5"
    assert (
        OpenAIProvider(api_key="fixture").build_request(request)["prompt_cache_retention"] == "24h"
    )
    request.options["cache_retention"] = "none"
    assert "prompt_cache_key" not in OpenAIProvider(api_key="fixture").build_request(request)
    request.options["reasoning"] = "max"
    request.model = "deepseek-flash"
    body = DeepSeekProvider(api_key="fixture").build_request(request)
    assert body["instructions"] == "system" and body["reasoning"] == {"effort": "max"}
    assert "include" not in body and body["input"][0]["role"] == "user"
