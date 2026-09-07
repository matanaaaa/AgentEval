"""pytest 配置：确保项目根目录在 sys.path 中，并隔离外部依赖"""
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config  # noqa: E402  （需在 sys.path 注入之后导入）


@pytest.fixture
def layer_of():
    """
    按层前缀取 LayerResult，避免用 result.layers[i] 下标断言。

    用层前缀取结果，避免层级调整时数组下标整体错位。
    """
    def _get(result, prefix: str):
        for lr in result.layers:
            if lr.layer.split("_", 1)[0] == prefix:
                return lr
        raise AssertionError(
            f"未找到层 {prefix}，实际层: {[lr.layer for lr in result.layers]}"
        )
    return _get


@pytest.fixture(autouse=True)
def disable_db_check(monkeypatch):
    """
    单测一律关掉 Database Check。

    L4 相关用例若不关，会真的走 DBChecker → 自动登录 → 请求 CRM。
    单测不该依赖网络和凭据，也不该往真实环境发请求。
    """
    monkeypatch.setattr(config, "DB_CHECK_ENABLED", False)
