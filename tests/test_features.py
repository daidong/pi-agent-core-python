import asyncio
import json

import pytest

from pi_python import FEATURES, ConfigurationError, require_features
from pi_python.mcp import MCP_FEATURES
from pi_python.plugins import Plugin, load_plugins


def test_feature_inventory_and_unknown_requirement():
    assert MCP_FEATURES <= FEATURES
    require_features(
        ["task-scope-v1", "loop-portal-v1", "plugin-requires-v1", "nested-agent-ownership-v1"]
    )
    with pytest.raises(ConfigurationError, match="missing-feature"):
        require_features(["missing-feature"], where="Example")


@pytest.mark.parametrize("value", [None, 42, "task-scope-v1", [""], [1], {"a": True}])
def test_invalid_requirements(value):
    with pytest.raises(ConfigurationError):
        require_features(value)


@pytest.mark.parametrize("strict", [True, False])
async def test_code_requirements_checked_before_any_setup(strict):
    called = []
    good = Plugin("good", lambda api: called.append(True))
    bad = Plugin("bad", requires=("future-feature",))
    with pytest.raises(ConfigurationError, match="bad.*future-feature"):
        async with load_plugins([good, bad], strict=strict):
            pass
    assert not called


async def test_directory_manifest_checked_before_import(tmp_path):
    (tmp_path / "pi-plugin.json").write_text(json.dumps({"requires": ["future-feature"]}))
    (tmp_path / "plugin.py").write_text("raise AssertionError('must not import')")
    with pytest.raises(ConfigurationError, match="future-feature"):
        async with load_plugins([tmp_path]):
            pass


@pytest.mark.parametrize(
    "manifest", ["[]", "{", '{"requires": "task-scope-v1"}', '{"require": []}']
)
async def test_invalid_directory_manifest(tmp_path, manifest):
    (tmp_path / "pi-plugin.json").write_text(manifest)
    with pytest.raises(ConfigurationError):
        async with load_plugins([tmp_path]):
            pass


async def test_directory_and_python_requirements_are_combined(tmp_path):
    (tmp_path / "pi-plugin.json").write_text(json.dumps({"requires": ["task-scope-v1"]}))
    (tmp_path / "plugin.py").write_text(
        "__requires__ = ('loop-portal-v1',)\ndef setup(api): pass\n"
    )
    async with load_plugins([tmp_path]) as plugins:
        assert set(plugins.plugins[0].requires) == {"task-scope-v1", "loop-portal-v1"}


async def test_single_file_requirement_checked_before_setup(tmp_path):
    code = tmp_path / "plugin.py"
    code.write_text("__requires__ = ('future-feature',)\ndef setup(api): raise AssertionError\n")
    with pytest.raises(ConfigurationError, match="future-feature"):
        async with load_plugins([code]):
            pass


async def test_plugin_scope_closes_active_and_queued_calls():
    started, gate = asyncio.Event(), asyncio.Event()
    cleaned = []
    handles = []

    def setup(api):
        scope = api.task_scope()
        handles.append(scope)

    async def work():
        started.set()
        try:
            await gate.wait()
        finally:
            await asyncio.sleep(0)
            cleaned.append(True)

    async with load_plugins([Plugin("example", setup, requires=("task-scope-v1",))]):
        caller = asyncio.create_task(handles[0].run(work()))
        await started.wait()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert cleaned == [True]
    assert handles[0].closed


async def test_code_root_manifest_is_enforced(tmp_path):
    (tmp_path / "pi-plugin.json").write_text(json.dumps({"requires": ["future-feature"]}))
    with pytest.raises(ConfigurationError, match="future-feature"):
        async with load_plugins([Plugin("example", root=tmp_path)]):
            pass


async def test_entry_point_requirements_are_enforced(monkeypatch):
    from types import SimpleNamespace
    from pi_python import plugins

    def setup(api):
        raise AssertionError("setup must not run")

    setup.__requires__ = ("future-feature",)
    ep = SimpleNamespace(
        name="example", value="sample:setup", dist=None, module="sample", load=lambda: setup
    )
    monkeypatch.setattr(plugins, "entry_points", lambda **kwargs: [ep])
    with pytest.raises(ConfigurationError, match="future-feature"):
        async with load_plugins(["example"]):
            pass
