"""步骤05运行时测试：健康三态、失败关闭、内存降级生命周期与停止信号等待。"""

from bootstrap.lifecycle import StopAndWait

from application.common.health import (
    DEGRADED,
    READY,
    UNAVAILABLE,
    ReadinessCheckUseCase,
)
from application.common.ports.dependency_probe import ProbeResult


class FakeProbe:
    def __init__(self, name: str, ok: bool = True, error: Exception | None = None) -> None:
        self.name = name
        self._ok = ok
        self._error = error

    def probe(self) -> ProbeResult:
        if self._error is not None:
            raise self._error
        return ProbeResult(name=self.name, ok=self._ok, latency_ms=1)


def make_use_case(probes, fallback: bool) -> ReadinessCheckUseCase:
    return ReadinessCheckUseCase(probes=tuple(probes), memory_fallback_allowed=fallback)


def full_set(redis_ok: bool = True, fallback: bool = False) -> ReadinessCheckUseCase:
    return make_use_case(
        [
            FakeProbe("mysql"),
            FakeProbe("redis", ok=redis_ok),
            FakeProbe("neo4j"),
            FakeProbe("rabbitmq"),
        ],
        fallback=fallback,
    )


def test_all_dependencies_up_is_ready() -> None:
    report = full_set().execute()
    assert report.status == READY
    assert report.dependencies == {"mysql": "up", "redis": "up", "neo4j": "up", "rabbitmq": "up"}


def test_redis_down_with_explicit_fallback_is_degraded() -> None:
    report = full_set(redis_ok=False, fallback=True).execute()
    assert report.status == DEGRADED
    assert report.dependencies["redis"] == "down"
    assert report.dependencies["mysql"] == "up"


def test_redis_down_without_fallback_fails_closed() -> None:
    report = full_set(redis_ok=False, fallback=False).execute()
    assert report.status == UNAVAILABLE


def test_any_non_redis_dependency_down_is_unavailable() -> None:
    for bad_name in ("mysql", "neo4j", "rabbitmq"):
        probes = [
            FakeProbe("mysql", ok="mysql" != bad_name),
            FakeProbe("redis"),
            FakeProbe("neo4j", ok="neo4j" != bad_name),
            FakeProbe("rabbitmq", ok="rabbitmq" != bad_name),
        ]
        report = make_use_case(probes, fallback=True).execute()
        assert report.status == UNAVAILABLE, bad_name


def test_probe_exception_is_treated_as_failure_not_crash() -> None:
    use_case = make_use_case(
        [FakeProbe("mysql", error=OSError("connect refused")), FakeProbe("redis")],
        fallback=True,
    )
    report = use_case.execute()
    assert report.status == UNAVAILABLE
    assert report.dependencies["mysql"] == "down"


def test_probe_all_returns_error_type_without_credentials() -> None:
    use_case = make_use_case([FakeProbe("mysql", error=OSError("connect refused"))], fallback=False)
    results = use_case.probe_all()
    assert results[0].ok is False
    assert results[0].error == "OSError"


def test_stop_and_wait_lifecycle() -> None:
    stopper = StopAndWait()
    assert not stopper.is_stopped()
    stopper.request_stop()
    stopper.wait(check_interval_s=0.01)
    assert stopper.is_stopped()
