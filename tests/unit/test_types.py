"""app/shared/types.py 通用类型单元测试

验证 NewType 标识运行时等价性（零开销、兼容裸 str 调用）与别名可用。
"""

from app.shared.types import Messages, SessionId, UserId


def test_session_id_runtime_identity():
    """NewType 恒等函数：运行时返回原 str 对象"""
    x = "s1"
    assert SessionId(x) is x
    assert SessionId("s1") == "s1"
    assert type(SessionId("s1")) is str


def test_user_id_runtime_identity():
    x = "u1"
    assert UserId(x) is x
    assert UserId("u1") == "u1"
    assert type(UserId("u1")) is str


def test_newtype_distinct():
    """类型层面区分：SessionId/UserId 是不同 NewType（供类型检查器区分用途）"""
    assert SessionId.__supertype__ is str
    assert UserId.__supertype__ is str
    assert SessionId is not UserId


def test_messages_alias():
    """Messages 别名运行时等价 list[dict]"""
    assert Messages == list[dict]
