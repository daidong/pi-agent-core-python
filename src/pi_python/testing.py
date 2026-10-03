"""Deterministic offline provider; records detached request snapshots."""

from copy import deepcopy
from collections.abc import AsyncIterator, Iterable
from .cancellation import CancelToken
from .provider import ModelEvent, ModelRequest
from .messages import AssistantMessage


class ScriptedProvider:
    def __init__(self, responses: Iterable[AssistantMessage | list[ModelEvent] | Exception]):
        self.responses = iter(responses)
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest, cancel: CancelToken) -> AsyncIterator[ModelEvent]:
        self.requests.append(deepcopy(request))
        response = next(self.responses, None)
        if response is None:
            raise RuntimeError("ScriptedProvider exhausted")
        if isinstance(response, Exception):
            raise response
        events = [ModelEvent.done(response)] if isinstance(response, AssistantMessage) else response
        for event in events:
            cancel.raise_if_cancelled()
            yield deepcopy(event)
