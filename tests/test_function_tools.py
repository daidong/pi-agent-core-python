import asyncio
import enum
import threading
from dataclasses import dataclass
from datetime import date
from typing import Annotated, ClassVar, Literal, NotRequired, Optional, TypedDict

import pytest
from pi_python import *


class Unit(enum.Enum):
    C = "celsius"
    F = "fahrenheit"


class Window(TypedDict):
    start: date
    days: int


class Options(TypedDict):
    query: str
    lang: NotRequired[str]


@dataclass
class Node:
    label: str
    children: list["Node"]
    kind: ClassVar[str] = "node"


@dataclass
class Point:
    x: float
    y: float = 0.0


def test_schema_and_description_from_google_docstring():
    @tool
    def forecast(city: str, unit: Unit = Unit.C, days: int | None = None) -> str:
        """Forecast the weather.

        Args:
            city: City name, for example "Newark".
            unit: Temperature unit.
                Celsius unless asked otherwise.
            days: How many days ahead.
        """

    assert isinstance(forecast, Tool) and forecast.name == "forecast"
    assert forecast.description == "Forecast the weather."
    assert forecast.input_schema == {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": 'City name, for example "Newark".'},
            "unit": {
                "enum": ["celsius", "fahrenheit"],
                "description": "Temperature unit. Celsius unless asked otherwise.",
                "default": "celsius",
            },
            "days": {
                "anyOf": [{"type": "integer"}, {"type": "null"}],
                "description": "How many days ahead.",
                "default": None,
            },
        },
        "required": ["city"],
        "additionalProperties": False,
    }


@pytest.mark.parametrize(
    "doc",
    [
        "Sum numbers.\n\nParameters\n----------\nvalues : list of float\n    Numbers to add.\nscale : float\n    Factor applied to the sum.\n",
        "Sum numbers.\n\n:param values: Numbers to add.\n:param scale: Factor applied\n    to the sum.\n:returns: The total.",
    ],
    ids=["numpy", "sphinx"],
)
def test_numpy_and_sphinx_docstrings(doc):
    def total(values: list[float], scale: float = 1.0) -> float:
        return sum(values) * scale

    total.__doc__ = doc
    t = tool(total)
    assert t.description == "Sum numbers."
    assert t.input_schema["properties"]["values"]["description"] == "Numbers to add."
    assert t.input_schema["properties"]["scale"]["description"].startswith("Factor applied")


async def test_arguments_are_converted_and_sync_functions_run_in_a_thread():
    seen = {}
    loop_thread = threading.get_ident()

    @tool(name="plan", description="Plan a trip")
    def plan(
        where: Annotated[str, "Destination"],
        mode: Literal["car", "train"],
        window: Window,
        stops: list[Point],
        unit: Unit = Unit.C,
        tags: set[str] = frozenset(),
        context: ToolContext = None,
    ) -> dict:
        seen.update(
            where=where,
            mode=mode,
            window=window,
            stops=stops,
            unit=unit,
            tags=tags,
            call_id=context.call_id,
            worker=threading.get_ident() != loop_thread,
        )
        return {"ok": True}

    schema = plan.input_schema
    assert schema["properties"]["where"] == {"type": "string", "description": "Destination"}
    assert "context" not in schema["properties"]
    assert schema["properties"]["window"]["properties"]["start"]["format"] == "date"
    args = {
        "where": "Paris",
        "mode": "train",
        "window": {"start": "2026-10-03", "days": 2},
        "stops": [{"x": 1, "y": 2.5}],
        "unit": "fahrenheit",
        "tags": ["a"],
    }
    p = ScriptedProvider(
        [AssistantMessage([ToolCall("c1", "plan", args)], "tool_use"), AssistantMessage.text("ok")]
    )
    r = await Agent(provider=p, tools=[plan]).prompt("go")
    outcome = r.tool_outcomes[0]
    assert outcome.execution_status == "succeeded" and outcome.result.structured_content == {
        "ok": True
    }
    assert seen == {
        "where": "Paris",
        "mode": "train",
        "window": {"start": date(2026, 10, 3), "days": 2},
        "stops": [Point(1.0, 2.5)],
        "unit": Unit.F,
        "tags": {"a"},
        "call_id": "c1",
        "worker": True,
    }


async def test_async_function_and_invalid_arguments_reach_the_model_as_errors():
    @tool
    async def double(n: int) -> int:
        """Double a number."""
        await asyncio.sleep(0)
        return n * 2

    calls = [ToolCall("good", "double", {"n": 4}), ToolCall("bad", "double", {"n": "4"})]
    p = ScriptedProvider([AssistantMessage(calls, "tool_use"), AssistantMessage.text("ok")])
    r = await Agent(provider=p, tools=[double]).prompt("go")
    good, bad = r.tool_outcomes
    assert good.result.content[0].text == "8" and good.result.structured_content == 8
    assert bad.result.error_code == "invalid_arguments"


def test_pydantic_model_parameters():
    pydantic = pytest.importorskip("pydantic")

    class Address(pydantic.BaseModel):
        street: str
        zip: str = pydantic.Field(pattern=r"^[0-9]{5}$")

    class Person(pydantic.BaseModel):
        name: str
        home: Address

    @tool
    def register(person: Person) -> str:
        """Register a person."""
        return f"{person.name} at {person.home.zip}"

    assert "person.Address" in register.input_schema["$defs"]
    outcome = asyncio.run(
        run_tool_call(
            register,
            ToolCall(
                "c", "register", {"person": {"name": "A", "home": {"street": "S", "zip": "19716"}}}
            ),
            ToolContext("run", "c", CancelToken(), lambda value: None),
        )
    )
    assert outcome.result.content[0].text == "A at 19716"


def test_unsupported_signatures_are_rejected_at_registration():
    with pytest.raises(ConfigurationError):
        tool(lambda *items: None)
    with pytest.raises(ConfigurationError):

        @tool
        def bad(handle: object) -> None: ...


def test_reviewed_signatures_and_types():
    import functools

    @tool
    def tree(root: Node, options: Options, n: int, context: Optional[ToolContext] = None) -> str:
        """Walk a tree.

        Keyword Args:
            n: Depth.
        """
        return f"{root.children[0].label}:{options.get('lang', 'en')}:{n}:{context.call_id}"

    schema = tree.input_schema
    assert schema["properties"]["root"] == {"$ref": "#/$defs/Node"}
    node = schema["$defs"]["Node"]
    assert node["required"] == ["label", "children"] and "kind" not in node["properties"]
    assert schema["properties"]["options"]["required"] == ["query"]
    assert schema["properties"]["n"]["description"] == "Depth."
    assert "context" not in schema["properties"]
    args = {
        "root": {"label": "a", "children": [{"label": "b", "children": []}]},
        "options": {"query": "q"},
        "n": 3.0,
    }
    out = asyncio.run(
        run_tool_call(tree, ToolCall("c", "tree", args), ToolContext("r", "c", CancelToken(), None))
    )
    assert out.result.content[0].text == "b:en:3:c"
    assert (
        tree(
            Node("x", [Node("y", [])]),
            {"query": "q"},
            1,
            ToolContext("r", "z", CancelToken(), None),
        )
        == "y:en:1:z"
    )

    def scaled(value: float, factor: float) -> float:
        """Scale a value."""
        return value * factor

    assert tool(functools.partial(scaled, factor=2), name="double").input_schema["required"] == [
        "value"
    ]

    class Counter:
        def __init__(self):
            self.count = 0

        def bump(self, by: int = 1) -> int:
            """Increase the counter."""
            self.count += by
            return self.count

        async def __call__(self, text: str) -> int:
            """Count characters."""
            return len(text)

    counter = Counter()
    assert tool(counter.bump).input_schema["properties"] == {
        "by": {"type": "integer", "default": 1}
    }
    assert tool(counter, name="chars").input_schema["required"] == ["text"]
    with pytest.raises(ConfigurationError, match="bound method"):
        tool(Counter.bump)
    with pytest.raises(ConfigurationError, match="pass @tool"):
        tool(lambda x: x)


def test_pydantic_models_with_the_same_name_do_not_collide():
    pydantic = pytest.importorskip("pydantic")

    def make(field):
        # Two different models that pydantic both names "Item".
        Item = pydantic.create_model("Item", **{field: (str, ...)})
        return pydantic.create_model("Wrapper", item=(Item, ...))

    A, B = make("a"), make("b")

    @tool
    def both(first: A, second: B) -> str:
        """Use two models."""
        return first.item.a + second.item.b

    out = asyncio.run(
        run_tool_call(
            both,
            ToolCall("c", "both", {"first": {"item": {"a": "x"}}, "second": {"item": {"b": "y"}}}),
            ToolContext("r", "c", CancelToken(), None),
        )
    )
    assert out.result.content[0].text == "xy"
