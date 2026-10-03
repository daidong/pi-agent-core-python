import asyncio
import json
import pytest
from pi_python import *


def test_multimodal_codec_roundtrip_and_rejects_invalid_image():
    messages = [
        UserMessage([TextContent("see"), ImageContent("aGk=", "image/png")]),
        AssistantMessage(
            [ThinkingContent("reason", "opaque"), TextContent("done", "signed")], api="x"
        ),
    ]
    assert decode_messages(encode_messages(messages)) == messages
    assert json.loads(encode_messages(messages))["schema_version"] == 3
    with pytest.raises(MessageValidationError):
        message_to_dict(UserMessage([ImageContent("!!", "image/png")]))


async def test_default_stream_and_reset_preserve_system_tools():
    provider = ScriptedProvider([AssistantMessage.text("done")])
    set_default_stream_fn(provider)
    try:
        agent = Agent(system_prompt="base")
        result = await agent.prompt("hello")
        assert result.status == "completed"
        agent.steer("first")
        agent.follow_up("second")
        peek = agent.peek_queued_messages()
        peek[0].content = "mutation"
        assert agent.peek_queued_messages()[0].content == "first"
        agent.clear_steering_queue()
        assert agent.peek_queued_messages()[0].content == "second"
        agent.reset()
        assert not agent.has_queued_messages()
        assert current_system_prompt(list(agent.state.messages)) == "base"
        assert len(agent.state.messages) == 1 and agent.signal is None
    finally:
        set_default_stream_fn(None)


async def test_lowlevel_prompt_continue_share_engine_and_context_contract():
    context = AgentContext(system_prompt="base")
    config = AgentLoopConfig(provider=ScriptedProvider([AssistantMessage.text("one")]))
    stream = agent_loop([UserMessage("go")], context, config)
    seen = [event.type async for event in stream]
    result = await stream.result()
    assert seen[0] == "agent_start" and seen[-1] == "agent_end"
    assert result[-1].content[0].text == "one" and context.messages == []
    context.messages = [UserMessage("resume")]
    result = await run_agent_loop_continue(
        context, AgentLoopConfig(provider=ScriptedProvider([AssistantMessage.text("two")]))
    )
    assert result[-1].content[0].text == "two" and context.messages[-1] == result[-1]


async def test_lowlevel_external_queues_and_callbacks():
    steering = [[UserMessage("steer")], []]
    following = [[UserMessage("follow")], []]
    config = AgentLoopConfig(
        provider=ScriptedProvider([AssistantMessage.text("one"), AssistantMessage.text("two")]),
        get_steering_messages=lambda: steering.pop(0) if steering else [],
        get_follow_up_messages=lambda: following.pop(0) if following else [],
    )
    result = await agent_loop([UserMessage("go")], AgentContext(), config).result()
    assert [m.content for m in result if isinstance(m, UserMessage)] == ["go", "steer", "follow"]


async def test_lowlevel_cancel_and_close_before_start():
    stream = agent_loop([UserMessage("go")], AgentContext(), AgentLoopConfig())
    await stream.aclose()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(stream.result(), 1)
    cancel = CancelToken()
    cancel.cancel()
    assert (
        await run_agent_loop([UserMessage("go")], AgentContext(), AgentLoopConfig(), cancel=cancel)
        == []
    )


async def test_reset_busy_and_prompt_images():
    gate = asyncio.Event()

    async def stream(request, cancel):
        assert isinstance(request.messages[-1].content[1], ImageContent)
        gate.set()
        await cancel.wait()
        cancel.raise_if_cancelled()
        yield ModelEvent.done(AssistantMessage.text("never"))

    agent = Agent(stream_fn=stream)
    task = asyncio.create_task(agent.prompt("see", [ImageContent("aGk=", "image/png")]))
    await gate.wait()
    with pytest.raises(AgentBusyError):
        agent.reset()
    assert agent.signal is not None
    agent.abort()
    result = await task
    assert result.status == "cancelled"
