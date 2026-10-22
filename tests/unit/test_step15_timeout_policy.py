"""绝对请求时限会传递到依赖调用并在嵌套退出后恢复。"""

import time

from application.common.execution.timeout_policy import bind_deadline_at, remaining_seconds


def test_deadline_scope_and_restore() -> None:
    assert remaining_seconds() is None
    with bind_deadline_at(time.monotonic() + 1):
        assert 0 < remaining_seconds() <= 1
        with bind_deadline_at(time.monotonic() - 1):
            assert remaining_seconds() == 0
        assert 0 < remaining_seconds() <= 1
    assert remaining_seconds() is None
