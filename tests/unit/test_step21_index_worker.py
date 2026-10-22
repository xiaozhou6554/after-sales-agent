"""Worker 无清单或后端读回证明时不得提交成功。"""

import hashlib

from application.knowledge.indexing.models import KnowledgeIndexLease
from application.knowledge.indexing.worker import KnowledgeIndexWorker

MANIFEST = b'{"release":"r1"}'
DIGEST = hashlib.sha256(MANIFEST).hexdigest()


class Jobs:
    def __init__(self):
        self.lease = KnowledgeIndexLease("t1", "r1", "vector", DIGEST, "worker", 1)
        self.completions = []

    def claim(self, tenant_id, release_id, backend, worker_id, *, lease_seconds=60):
        return self.lease

    def complete(self, lease, *, verified_manifest_digest):
        self.completions.append(verified_manifest_digest)
        return "succeeded" if verified_manifest_digest else "failed"


class Manifest:
    def __init__(self, raw=MANIFEST):
        self.raw = raw

    def get_manifest(self, tenant_id, release_id):
        return self.raw


class Backend:
    def __init__(self, verified=DIGEST):
        self.verified = verified
        self.calls = []

    def build(self, lease, manifest):
        self.calls.append("build")

    def verify(self, lease, manifest):
        self.calls.append("verify")
        return self.verified


def test_worker_builds_then_verifies_before_ack() -> None:
    jobs, backend = Jobs(), Backend()
    assert (
        KnowledgeIndexWorker(jobs, Manifest(), {"vector": backend}).run_once(
            "t1", "r1", "vector", "worker"
        )
        == "succeeded"
    )
    assert backend.calls == ["build", "verify"]
    assert jobs.completions == [DIGEST]


def test_manifest_mismatch_fails_before_build() -> None:
    jobs, backend = Jobs(), Backend()
    assert (
        KnowledgeIndexWorker(jobs, Manifest(b"changed"), {"vector": backend}).run_once(
            "t1", "r1", "vector", "worker"
        )
        == "failed"
    )
    assert backend.calls == []
    assert jobs.completions == [None]


def test_bad_backend_readback_does_not_count_as_success() -> None:
    jobs, backend = Jobs(), Backend("wrong")
    assert (
        KnowledgeIndexWorker(jobs, Manifest(), {"vector": backend}).run_once(
            "t1", "r1", "vector", "worker"
        )
        == "failed"
    )
    assert jobs.completions == [None]
