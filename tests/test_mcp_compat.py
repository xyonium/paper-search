"""Regression: the tool must import on both mcp 1.x and 2.x.

OpenWebUI loads this file with whatever `mcp` its image ships. mcp 2.x renamed
the StreamableHTTP helper (streamablehttp_client -> streamable_http_client) and
its context manager now yields a 2-tuple (read, write) instead of 1.x's
(read, write, get_session_id).

When that import broke, `exec()` raised ImportError inside OpenWebUI's plugin
loader: the module never got built, so the tool was silently unusable AND the
Valves editor rendered "no valve" (GET /tools/id/<id>/valves/spec -> null).
"""

from __future__ import annotations

import importlib.util
import os
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "tool_mcp_compat", os.path.join(os.path.dirname(__file__), "..", "tool.py")
)
tool_mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool_mod)


def test_module_imports_and_exposes_streamable_client():
    """The compat shim must bind `streamablehttp_client` whatever the mcp major."""
    assert hasattr(tool_mod, "streamablehttp_client")
    assert callable(tool_mod.streamablehttp_client)
    assert hasattr(tool_mod, "ClientSession")


def test_valves_schema_renders():
    """OpenWebUI renders the editor from Valves.schema(); it must not raise."""
    schema = tool_mod.Tools.Valves.schema()
    props = schema.get("properties", {})
    assert props, "valves schema must declare fields"
    for expected in ("papers_service_url", "openwebui_url", "firecrawl_base_url"):
        assert expected in props, f"missing valve {expected}"


@pytest.mark.asyncio
async def test_streamable_client_unpacks_two_or_three_tuple(monkeypatch):
    """The call site slices [0:2], so both mcp majors' yield shapes work."""

    class _FakeSession:
        def __init__(self, read, write):
            self.read, self.write = read, write

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def initialize(self):
            return None

        async def call_tool(self, name, args):
            return SimpleNamespace(
                isError=False,
                content=[SimpleNamespace(text='{"ok": 1}')],
            )

    def _make_client(n):
        class _CM:
            async def __aenter__(self_inner):
                return tuple(range(n))

            async def __aexit__(self_inner, *exc):
                return False

        return _CM()

    monkeypatch.setattr(tool_mod, "ClientSession", _FakeSession)
    t = tool_mod.Tools()
    t.valves = tool_mod.Tools.Valves()

    for arity in (2, 3):  # mcp 2.x, mcp 1.x
        monkeypatch.setattr(
            tool_mod, "streamablehttp_client", lambda url, n=arity: _make_client(n)
        )
        out = await t._zhihuiya_call("some_tool", {"q": "x"}, "k", timeout=5)
        assert isinstance(out, dict)
