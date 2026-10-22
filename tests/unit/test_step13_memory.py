"""步骤13：摘要覆盖、预算、事实类别、租户隔离与衰减。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from bootstrap.worker_main import run_memory_cleanup
from langchain_core.messages import AIMessage, SystemMessage

from application.agent.ports.llm import ChatMessage, LLMDependencyError, LLMResult, ToolSpec
from application.agent.prompts import PromptCatalog
from application.agent.service import ChatService
from application.common.authorization import AuthorizationDenied
from application.common.context import AuthContext, DataScope, TraceContext
from application.common.dto import ChatRequestDto
from application.memory.use_cases.long_term import LongTermMemoryUseCase
from application.memory.use_cases.maintenance import MemoryMaintenanceUseCase
from application.memory.use_cases.session_context import SessionContextUseCase
from domain.memory.aggregates.memory_profile import MemoryProfile
from domain.memory.entities.memory_fact import MemoryFact
from infrastructure.agent.budgeted_gateway import BudgetedLLMGateway, ContextBudgetError
from infrastructure.agent.deepseek_adapter import DeepSeekChatGateway
from infrastructure.agent.memory_adapter import InMemorySessionRepository


class FakeLLM:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = []

    def complete(self, *, system, messages, tools=None):
        self.calls.append((system, list(messages), tools))
        return LLMResult(content=self.content)


class FakeProfileRepository:
    def __init__(self) -> None:
        self.profiles: dict[tuple[str, str], MemoryProfile] = {}

    def load(self, tenant_id, user_id):
        return self.profiles.setdefault((tenant_id, user_id), MemoryProfile(tenant_id, user_id))

    def upsert(self, tenant_id, user_id, fact):
        self.load(tenant_id, user_id).remember(fact)


def auth(subject: str = "customer-a", tenant: str = "tenant-a") -> AuthContext:
    now = datetime.now(timezone.utc)
    return AuthContext(
        tenant,
        subject,
        ("customer",),
        (),
        DataScope(customer_ids=(subject,)),
        "session-a",
        now,
        now + timedelta(hours=1),
    )


def add_rounds(repository: InMemorySessionRepository, count: int) -> None:
    for index in range(count):
        repository.append("tenant-a", "thread-a", "customer-a", ChatMessage("user", f"问题{index}"))
        repository.append(
            "tenant-a", "thread-a", "customer-a", ChatMessage("assistant", f"回答{index}")
        )


def test_summary_starts_after_seven_rounds_and_only_adds_new_messages() -> None:
    repository = InMemorySessionRepository()
    llm = FakeLLM("用户偏好黑色产品")
    use_case = SessionContextUseCase(repository, llm)
    add_rounds(repository, 6)
    assert len(use_case.history("tenant-a", "thread-a", "customer-a")) == 12
    assert llm.calls == []
    add_rounds(repository, 1)
    history = use_case.history("tenant-a", "thread-a", "customer-a")
    assert len(history) == 9
    assert history[0].content.startswith("[会话摘要")
    assert (
        repository.load_snapshot("tenant-a", "thread-a", subject_id="customer-a").covered_until == 6
    )
    add_rounds(repository, 1)
    use_case.history("tenant-a", "thread-a", "customer-a")
    assert "问题3" in llm.calls[-1][1][0].content
    assert "问题0" not in llm.calls[-1][1][0].content
    assert (
        repository.load_snapshot("tenant-a", "thread-a", subject_id="customer-a").covered_until == 8
    )


def test_summary_failure_falls_back_and_sensitive_text_is_not_saved() -> None:
    repository = InMemorySessionRepository()
    add_rounds(repository, 7)
    llm = FakeLLM("住址北京市朝阳区幸福路99号")
    use_case = SessionContextUseCase(repository, llm)
    assert len(use_case.history("tenant-a", "thread-a", "customer-a")) == 14
    snapshot = repository.load_snapshot("tenant-a", "thread-a", subject_id="customer-a")
    assert snapshot.summary == ""
    assert snapshot.covered_until == 0


def test_model_gateway_trims_old_context_on_every_call() -> None:
    llm = FakeLLM("完成")
    gateway = BudgetedLLMGateway(llm, limit=180)
    messages = [
        ChatMessage("system", "系统规则"),
        ChatMessage("user", "旧问题" * 200),
        ChatMessage("assistant", "旧回答" * 200),
        ChatMessage("user", "当前问题"),
    ]
    gateway.complete(system="系统规则", messages=messages)
    sent = llm.calls[0][1]
    assert sent[0].role == "system"
    assert sent[-1].content == "当前问题"
    assert len(sent) < len(messages)
    with pytest.raises(ContextBudgetError):
        gateway.complete(system="系统规则" * 200, messages=[ChatMessage("user", "当前问题")])


def test_current_prompt_and_read_tool_catalog_fit_normal_chat_budget() -> None:
    root = Path(__file__).resolve().parents[2]
    prompt = PromptCatalog.load(root / "data" / "prompts").supervisor_system_prompt()
    manifest = json.loads(
        (root / "data" / "contracts" / "mcp-tools.json").read_text(encoding="utf-8")
    )
    tools = [
        ToolSpec(item["name"], item["description"], item["input_schema"])
        for item in manifest["tools"]
        if item["access"] == "read"
    ]
    llm = FakeLLM("完成")
    gateway = BudgetedLLMGateway(llm)
    messages = [ChatMessage("system", prompt), ChatMessage("user", "您好")]
    gateway.complete(system=prompt, messages=messages, tools=tools)
    assert llm.calls[0][1] == messages
    assert gateway._count(prompt, messages, tools) <= gateway._limit


def test_deepseek_adapter_sends_matching_system_prompt_once() -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.messages = []

        def invoke(self, messages):
            self.messages = messages
            return AIMessage(content="完成")

    client = RecordingClient()
    gateway = object.__new__(DeepSeekChatGateway)
    gateway._client = client
    gateway._model_name = "test-model"
    gateway.complete(
        system="系统规则",
        messages=[ChatMessage("system", "系统规则"), ChatMessage("user", "您好")],
    )
    assert len(client.messages) == 2
    assert isinstance(client.messages[0], SystemMessage)
    assert client.messages[0].content == "系统规则"


def test_summary_and_extraction_failures_do_not_break_chat() -> None:
    class AuxiliaryFailureLLM:
        def complete(self, *, system, messages, tools=None):
            if system.startswith(("压缩售后对话", "仅从用户明确说出")):
                raise LLMDependencyError("辅助模型不可用")
            return LLMResult(content="您好，请问有什么可以帮您？")

    class UnusedTools:
        def execute_tool(self, *args, **kwargs):
            raise AssertionError("寒暄不应调用工具")

    root = Path(__file__).resolve().parents[2]
    repository = InMemorySessionRepository()
    add_rounds(repository, 7)
    llm = BudgetedLLMGateway(AuxiliaryFailureLLM())
    service = ChatService(
        llm=llm,
        tools=UnusedTools(),
        memory=repository,
        prompts=PromptCatalog.load(root / "data" / "prompts"),
        registry_path=str(root / "data" / "contracts" / "mcp-tools.json"),
        session_context=SessionContextUseCase(repository, llm),
        long_term_memory=LongTermMemoryUseCase(FakeProfileRepository(), llm),
    )
    trace = TraceContext("1.0", "req-step13", "corr-step13", "trace-step13")
    response = service.execute(
        ChatRequestDto(trace=trace, message="您好", thread_id="thread-a"),
        auth(),
        session_token="test-token",
    )
    assert response.status == "success"
    assert response.answer == "您好，请问有什么可以帮您？"
    assert len(repository.load("tenant-a", "thread-a", subject_id="customer-a")) == 16


def test_long_term_memory_only_accepts_approved_stable_facts_and_isolates_customer(caplog) -> None:
    repository = FakeProfileRepository()
    llm = FakeLLM(
        '[{"fact_type":"product_preference","fact_key":"颜色偏好",'
        '"fact_value":"黑色","importance":0.7},'
        '{"fact_type":"budget","fact_key":"预算","fact_value":"5000元",'
        '"importance":0.9},'
        '{"fact_type":"after_sales_fact","fact_key":"联系电话",'
        '"fact_value":"13812345678","importance":0.5}]'
    )
    use_case = LongTermMemoryUseCase(repository, llm)
    use_case.remember_turn(auth(), "customer-a", "我喜欢黑色产品")
    facts = repository.load("tenant-a", "customer-a").facts
    assert len(facts) == 1
    assert facts[0].fact_value == "黑色"
    assert use_case.context(auth(), "customer-a")[0].content.endswith("颜色偏好: 黑色")
    assert repository.load("tenant-b", "customer-a").facts == []
    with pytest.raises(AuthorizationDenied):
        use_case.context(auth(), "customer-b")
    assert "长期记忆访问被拒绝" in caplog.text


def test_extracted_fact_must_be_grounded_in_customer_statement() -> None:
    repository = FakeProfileRepository()
    llm = FakeLLM(
        '[{"fact_type":"product_preference","fact_key":"颜色偏好",'
        '"fact_value":"蓝色","importance":0.8}]'
    )
    LongTermMemoryUseCase(repository, llm).remember_turn(auth(), "customer-a", "我喜欢黑色产品")
    assert repository.load("tenant-a", "customer-a").facts == []


def test_importance_decay_and_cleanup_threshold() -> None:
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=180)
    ordinary = MemoryFact("product_preference", "颜色", "黑色", 0.5, "conversation", old)
    important = MemoryFact("product_preference", "产品", "型号A", 0.8, "conversation", old)
    assert ordinary.cleanup_candidate(now)
    assert not important.cleanup_candidate(now)


def test_memory_maintenance_batches_until_no_candidates() -> None:
    class BatchRepository(FakeProfileRepository):
        def __init__(self) -> None:
            super().__init__()
            self.batches = [2, 1]
            self.seen = []

        def delete_cleanup_candidates(self, now, limit, tenant_id=None):
            self.seen.append((limit, tenant_id))
            return self.batches.pop(0)

    repository = BatchRepository()
    assert MemoryMaintenanceUseCase(repository, batch_size=2).run(tenant_id="tenant-a") == 3
    assert repository.seen == [(2, "tenant-a"), (2, "tenant-a")]


def test_worker_runs_memory_cleanup_before_waiting_for_next_cycle() -> None:
    class Stopper:
        stopped = False

        def is_stopped(self):
            return self.stopped

    class Repository:
        calls = 0

        def delete_cleanup_candidates(self, now, limit, tenant_id=None):
            self.calls += 1
            stopper.stopped = True
            return 0

    stopper = Stopper()
    repository = Repository()
    run_memory_cleanup(stopper, repository)
    assert repository.calls == 1
