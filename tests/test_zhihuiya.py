import importlib.util
import os
import sys
from types import SimpleNamespace

import pytest

# tool.py 含连字符路径无法直接包名导入，用 importlib 按路径加载
SPEC = importlib.util.spec_from_file_location(
    "tool", os.path.join(os.path.dirname(__file__), "..", "tool.py")
)
tool_mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool_mod)
Tools = tool_mod.Tools


def _user(apikey="", enabled=True):
    return {"valves": SimpleNamespace(zhihuiya_apikey=apikey, zhihuiya_enabled=enabled)}


def test_disabled_when_no_key():
    t = Tools()
    t.valves = Tools.Valves()  # zhihuiya_apikey 默认 ""
    enabled, key = t._zhihuiya_enabled_key(_user())
    assert enabled is False and key == ""


def test_enabled_with_admin_key():
    t = Tools()
    t.valves = Tools.Valves(zhihuiya_apikey="admin-key")
    enabled, key = t._zhihuiya_enabled_key(_user())
    assert enabled is True and key == "admin-key"


def test_user_key_overrides_admin():
    t = Tools()
    t.valves = Tools.Valves(zhihuiya_apikey="admin-key")
    enabled, key = t._zhihuiya_enabled_key(_user(apikey="user-key"))
    assert enabled is True and key == "user-key"


def test_user_switch_off_disables_even_with_key():
    t = Tools()
    t.valves = Tools.Valves(zhihuiya_apikey="admin-key")
    enabled, key = t._zhihuiya_enabled_key(_user(apikey="user-key", enabled=False))
    assert enabled is False


import json
from unittest.mock import AsyncMock, MagicMock, patch


def _fake_session(call_result):
    session = AsyncMock()
    session.initialize = AsyncMock()
    session.call_tool = AsyncMock(return_value=call_result)
    return session


def _text_content(payload):
    c = MagicMock()
    c.text = json.dumps(payload)
    return c


@pytest.mark.asyncio
async def test_zhihuiya_call_parses_json_text():
    t = Tools()
    payload = {"success": True, "data": {"results": [{"paper_id": "p1"}]},
               "error_code": 0, "error_msg": ""}
    result = MagicMock()
    result.isError = False
    result.content = [_text_content(payload)]
    session = _fake_session(result)

    with patch.object(tool_mod, "streamablehttp_client") as m_client, \
         patch.object(tool_mod, "ClientSession") as m_sess:
        m_client.return_value.__aenter__ = AsyncMock(return_value=("r", "w", None))
        m_client.return_value.__aexit__ = AsyncMock(return_value=False)
        m_sess.return_value.__aenter__ = AsyncMock(return_value=session)
        m_sess.return_value.__aexit__ = AsyncMock(return_value=False)

        out = await t._zhihuiya_call("search_literature", {"text": "x", "type": "all"}, "key123")

    assert out["success"] is True
    session.call_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_zhihuiya_call_raises_on_error_result():
    t = Tools()
    result = MagicMock()
    result.isError = True
    result.content = [_text_content({"error_msg": "apikey not Pass and call failed!"})]
    session = _fake_session(result)

    with patch.object(tool_mod, "streamablehttp_client") as m_client, \
         patch.object(tool_mod, "ClientSession") as m_sess:
        m_client.return_value.__aenter__ = AsyncMock(return_value=("r", "w", None))
        m_client.return_value.__aexit__ = AsyncMock(return_value=False)
        m_sess.return_value.__aenter__ = AsyncMock(return_value=session)
        m_sess.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(RuntimeError):
            await t._zhihuiya_call("search_literature", {"text": "x", "type": "all"}, "bad")


def test_map_paper_merges_search_and_bibliography():
    search_item = {
        "paper_id": "p1",
        "doi": "10.1/abc",
        "title": ["Some Title"],
        "author": ["Doe, John", "Roe, Jane"],
    }
    bib = {
        "publication_year": "2021",
        "publication": "Nature",
        "abstract": [{"lang": "EN", "text": "An abstract."}],
        "title": [{"lang": "EN", "text": "Some Title"}],
    }
    out = Tools._zhihuiya_map_paper(search_item, bib)
    assert out["title"] == "Some Title"
    assert out["authors"] == "Doe, John; Roe, Jane"
    assert out["published_date"] == "2021"
    assert out["abstract"] == "An abstract."
    assert out["paper_id"] == "p1"
    assert out["doi"] == "10.1/abc"
    assert out["source"] == "zhihuiya"
    assert out["pdf_url"] == ""
    assert out["citations"] == 0


def test_map_paper_handles_missing_bib_and_list_fields():
    search_item = {"paper_id": "p2", "title": ["T"], "author": []}
    out = Tools._zhihuiya_map_paper(search_item, None)
    assert out["title"] == "T"
    assert out["authors"] == ""
    assert out["abstract"] == ""
    assert out["published_date"] == ""
