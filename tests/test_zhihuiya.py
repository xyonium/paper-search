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
