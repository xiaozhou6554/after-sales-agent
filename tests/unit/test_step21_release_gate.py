"""步骤21：三路任务全部同源且成功才准发布。"""

from application.knowledge.indexing import IndexJobState, decide_publication


def _jobs(*, status: str = "succeeded") -> tuple[IndexJobState, ...]:
    return tuple(
        IndexJobState(name, "r1", "digest", status, 1) for name in ("vector", "bm25", "graph")
    )


def test_all_three_matching_successes_are_ready() -> None:
    assert decide_publication("r1", "digest", _jobs()).status == "ready"


def test_missing_or_duplicate_backend_is_not_publishable() -> None:
    assert decide_publication("r1", "digest", _jobs()[:2]).status == "pending"
    duplicate = _jobs()[:2] + (IndexJobState("vector", "r1", "digest", "succeeded", 1),)
    assert decide_publication("r1", "digest", duplicate).status == "indexing_failed"


def test_mismatch_failure_or_pending_prevents_activation() -> None:
    jobs = _jobs()
    mismatch = jobs[:2] + (IndexJobState("graph", "r2", "digest", "succeeded", 1),)
    assert decide_publication("r1", "digest", mismatch).status == "indexing_failed"
    failure = jobs[:2] + (IndexJobState("graph", "r1", "digest", "failed", 1),)
    assert decide_publication("r1", "digest", failure).status == "indexing_failed"
    pending = jobs[:2] + (IndexJobState("graph", "r1", "digest", "pending", 0),)
    assert decide_publication("r1", "digest", pending).status == "pending"


def test_success_without_persisted_revision_is_rejected() -> None:
    jobs = _jobs()[:2] + (IndexJobState("graph", "r1", "digest", "succeeded", 0),)
    assert decide_publication("r1", "digest", jobs).status == "indexing_failed"
