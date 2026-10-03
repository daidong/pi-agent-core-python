import asyncio
import subprocess
import sys
import textwrap

import pytest
from pi_python import *


class LoopBound:
    """Keeps loop-bound state between requests, as cached connections do."""

    def __init__(self):
        self.loops = []
        self.lock = None

    async def stream(self, request, cancel):
        self.lock = self.lock or asyncio.Lock()
        async with self.lock:
            self.loops.append(asyncio.get_running_loop())
        yield ModelEvent.done(AssistantMessage.text(f"answer {len(self.loops)}"))


def test_prompt_sync_reuses_one_loop_across_calls():
    provider = LoopBound()
    agent = Agent(provider=provider)
    first = agent.prompt_sync("one")
    second = agent.prompt_sync("two")
    assert (first.status, second.status) == ("completed", "completed")
    assert second.messages[-1].content[0].text == "answer 2"
    assert provider.loops[0] is provider.loops[1]
    assert run_sync(asyncio.sleep(0, result=7)) == 7


async def test_blocking_call_inside_a_running_loop_is_refused():
    agent = Agent(provider=LoopBound())
    with pytest.raises(RuntimeError, match="await the async API"):
        agent.prompt_sync("no")


@pytest.mark.skipif(sys.platform == "win32", reason="os.kill cannot deliver SIGINT on Windows")
def test_ctrl_c_aborts_the_run_then_raises():
    script = textwrap.dedent(
        """
        import asyncio, os, signal, threading
        from pi_python import *

        class Slow:
            async def stream(self, request, cancel):
                yield ModelEvent.boundary("start", 0, TextContent(""))
                yield ModelEvent.text("partial", 0)
                await asyncio.Event().wait()

        agent = Agent(provider=Slow())
        threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGINT)).start()
        try:
            agent.prompt_sync("go")
        except KeyboardInterrupt:
            last = agent.state.messages[-1]
            print("interrupted", last.stop_reason, last.content[0].text, agent.state.is_running)
        """
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert out.stdout.strip() == "interrupted aborted partial False", out.stderr
