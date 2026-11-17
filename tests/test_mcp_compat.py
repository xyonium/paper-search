"""Regression: the tool must import and work on both mcp 1.x and 2.x.

OpenWebUI loads this file with whatever `mcp` its image ships. The two majors
differ in ways that all used to break the tool:

1. mcp 2.x renamed the StreamableHTTP helper
   (``streamablehttp_client`` -> ``streamable_http_client``).
2. The helper's context manager yields a different arity:
   1.x yields ``(read, write, get_session_id)``, 2.x yields ``(read, write)``.
3. ``CallToolResult.isError`` became ``is_error``; on 2.x the camelCase name
   survives only as a constructor/wire alias and is NOT readable as an
   attribute, so reading the wrong one silently treats a failed tool call as
   success.

When (1) broke, ``exec()`` raised ImportError inside OpenWebUI's plugin
loader: the module never got built, so the tool was silently unusable AND the
Valves editor rendered "no valve" (GET /tools/id/<id>/valves/spec -> null).

A single test run only ever exercises the branch that matches the installed
mcp, so the dual-name test below stubs ``sys.modules`` to force each branch.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from types import SimpleNamespace

import pytest

TOOL_PATH = os.path.join(os.path.dirname(__file__), "..", "tool.py")


def _load_tool_module(name: str = "tool_mcp_compat"):
    """Load tool.py fresh, the way OpenWebUI's plugin loader execs it."""
    spec = importlib.util.spec_from_file_location(name, TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tool_mod = _load_tool_module()


def test_module_imports_and_exposes_streamable_client():
    """The compat shim must bind `streamablehttp_client` whatever the mcp major."""
    assert hasattr(tool_mod, "streamablehttp_client")
    assert callable(tool_mod.streamablehttp_client)
    assert hasattr(tool_mod, "ClientSession")


@pytest.mark.parametrize(
    "exposed,absent",
    [
        ("streamable_http_client", "streamablehttp_client"),  # mcp >= 2.0
        ("streamablehttp_client", "streamable_http_client"),  # mcp 1.x
    ],
)
def test_import_shim_covers_both_mcp_majors(monkeypatch, exposed, absent):
    """Force each selection branch, regardless of which mcp the runner has.

    Without this, a CI box on mcp 1.x only ever proves the fallback branch: if
    someone dropped the compat shim, the crash on mcp 2.x would go undetected.
    """
    sentinel = object()
    fake = types.ModuleType("mcp.client.streamable_http")
    setattr(fake, exposed, sentinel)
    assert not hasattr(fake, absent), "stub must expose exactly one name"

    monkeypatch.setitem(sys.modules, "mcp.client.streamable_http", fake)
    import mcp.client

    monkeypatch.setattr(mcp.client, "streamable_http", fake, raising=False)

    mod = _load_tool_module(name=f"tool_branch_{exposed}")
    assert (
        mod.streamablehttp_client is sentinel
    ), f"tool.py did not pick up the {exposed!r}-only mcp install"


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_valves_schema_renders():
    """OpenWebUI renders the editor from Valves.schema(); it must not raise.

    ``.schema()`` (not ``model_json_schema()``) is deliberate: OpenWebUI's
    ``/tools/id/<id>/valves/spec`` handler calls ``Valves.schema()`` itself, so
    this mirrors the real call site rather than the modern pydantic spelling.
    """
    schema = tool_mod.Tools.Valves.schema()
    props = schema.get("properties", {})
    assert props, "valves schema must declare fields"
    for expected in ("papers_service_url", "openwebui_url", "firecrawl_base_url"):
        assert expected in props, f"missing valve {expected}"


def _patch_transport(monkeypatch, session_cls, arity):
    """Point the module's transport + session at fakes."""

    class _CM:
        async def __aenter__(self_inner):
            return tuple(range(arity))

        async def __aexit__(self_inner, *exc):
            return False

    monkeypatch.setattr(tool_mod, "ClientSession", session_cls)
    monkeypatch.setattr(tool_mod, "streamablehttp_client", lambda url: _CM())


@pytest.mark.asyncio
@pytest.mark.parametrize("arity", [2, 3])
async def test_zhihuiya_call_unpacks_two_or_three_tuple(monkeypatch, arity):
    """mcp 2.x yields (read, write); 1.x yields (read, write, get_session_id)."""
    seen = {"events": []}

    class _FakeSession:
        def __init__(self, read, write):
            seen["streams"] = (read, write)

        async def __aenter__(self):
            seen["events"].append("session_enter")
            return self

        async def __aexit__(self, *exc):
            seen["events"].append("session_exit")
            return False

        async def initialize(self):
            seen["events"].append("initialize")
            return None

        async def call_tool(self, name, args):
            seen["call"] = (name, args)
            return SimpleNamespace(
                is_error=False,
                content=[SimpleNamespace(text='{"ok": 1}')],
            )

    _patch_transport(monkeypatch, _FakeSession, arity)

    t = tool_mod.Tools()
    t.valves = tool_mod.Tools.Valves()

    out = await t._zhihuiya_call("some_tool", {"q": "x"}, "k", timeout=5)

    # Anchors the behaviour: a real dict came back, not an error fallback.
    assert out == {"ok": 1}
    # ...and read/write were taken from positions 0/1 in both shapes.
    assert seen["streams"] == (0, 1)
    # The full happy path ran: initialize -> call_tool with the caller's args.
    assert seen["call"] == ("some_tool", {"q": "x"})
    assert "initialize" in seen["events"]
    assert seen["events"].count("session_exit") == 1


@pytest.mark.asyncio
async def test_zhihuiya_call_raises_on_error_result(monkeypatch):
    """An error tool result must raise, on both mcp majors.

    Uses the SDK's real ``CallToolResult`` built with the wire alias
    ``isError=`` (accepted by 1.x and 2.x) so the test exercises whichever
    attribute name the installed major actually exposes.
    """
    from mcp.types import CallToolResult, TextContent

    class _FakeSession:
        def __init__(self, read, write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def initialize(self):
            return None

        async def call_tool(self, name, args):
            return CallToolResult(
                isError=True,
                content=[TextContent(type="text", text="quota exceeded")],
            )

    _patch_transport(monkeypatch, _FakeSession, 2)

    t = tool_mod.Tools()
    t.valves = tool_mod.Tools.Valves()

    with pytest.raises(RuntimeError) as ei:
        await t._zhihuiya_call("some_tool", {}, "k", timeout=5)

    assert "返回错误" in str(ei.value)
    assert "quota exceeded" in str(ei.value)
