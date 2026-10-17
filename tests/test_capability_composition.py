"""C29: public-interface composition; domain decisions stay outside core."""

import pytest
from pi_python import *
from examples.capability_composition import CheckPack, compare, compose


async def test_C29_completed_does_not_mean_domain_passed():
    result = await compare()
    assert result["domain_passed"] is False
    assert result["model"].tool_outcomes[0].origin == "model"
    assert result["application"].tool_outcomes[0].origin == "application"
    assert result["model_requests"] == 2 and result["application_requests"] == 1


def test_C29_duplicate_instructions_and_tools_rejected():
    provider = ScriptedProvider([])
    with pytest.raises(ConfigurationError, match="Duplicate instruction"):
        compose([CheckPack(), CheckPack()], provider, "application")
    with pytest.raises(ConfigurationError, match="Duplicate tool names"):
        compose([CheckPack(name="first"), CheckPack(name="second")], provider, "application")
    agent = compose([CheckPack()], provider, "application")
    with pytest.raises(ConfigurationError, match="Duplicate tool names"):
        agent.update_config(AgentConfigUpdate(tools=CheckPack().tools() * 2))


async def test_C29_state_configuration_and_veto_isolation():
    first, second = CheckPack(minimum=0), CheckPack(minimum=10)
    a = compose([first], ScriptedProvider([]), "application")
    b = compose([second], ScriptedProvider([]), "application")
    for agent, expected in ((a, True), (b, False)):
        result = await agent.execute_tools([ToolInvocation("check_sample", {"value": 5})])
        assert result.tool_outcomes[0].result.structured_content["domain_passed"] is expected
    assert first.calls == second.calls == [5] and first.calls is not second.calls
    blocked = await a.execute_tools([ToolInvocation("check_sample", {"value": 999})])
    assert blocked.tool_outcomes[0].execution_status == "not_started"
    assert first.calls == [5]
    metadata = [m for m in b.state.messages if isinstance(m, CustomMessage)][0]
    assert metadata.data["packs"][0]["minimum"] == 10
    metadata.data["packs"][0]["minimum"] = -100
    assert second.minimum == 10
    assert [m for m in b.state.messages if isinstance(m, CustomMessage)][0].data["packs"][0][
        "minimum"
    ] == 10


async def test_C29_later_allow_cannot_override_veto():
    class AllowPack(CheckPack):
        def tools(self):
            return []

        def before(self, call, args, context):
            return True

    first = CheckPack()
    agent = compose([first, AllowPack(name="allow")], ScriptedProvider([]), "application")
    result = await agent.execute_tools([ToolInvocation("check_sample", {"value": 999})])
    assert result.tool_outcomes[0].result.error_code == "blocked"
    assert not first.calls
