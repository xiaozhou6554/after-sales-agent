"""步骤28 集成测试：通知发送状态、事件链扇出、消费幂等与故障边界（TC-78～86）。

真实 MySQL；消费经 InboxRepository 租约/业务守卫驱动，无需 RabbitMQ。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from bootstrap.api_main import create_app
from bootstrap.settings import load_settings
from fastapi.testclient import TestClient
from sqlalchemy import text

from application.common.context import TraceContext
from application.common.events import (
    EventEnvelope,
    NotificationCreatedPayload,
    OrderShippingAddressUpdatedPayload,
    parse_event,
)
from infrastructure.auth.mock_auth_adapter import MockAuthAdapter
from infrastructure.messaging.event_contract import EventCatalog
from infrastructure.messaging.inbox_repository import (
    InboxConflictError,
    InboxRepository,
)
from infrastructure.messaging.notification_handlers import (
    MockNotificationSender,
    NotificationFanoutHandler,
    NotificationSendHandler,
)
from infrastructure.messaging.outbox_repository import OutboxRepository
from infrastructure.persistence.mysql.connection import create_mysql_engine

CATALOG = EventCatalog("data/contracts/event-catalog.json", "data/contracts/events.schema.json")


def _envelope(
    event_type: str,
    producer: str,
    payload,
    *,
    event_id: str,
    idempotency_key: str,
    tenant_id: str = "tenant-north",
) -> EventEnvelope:
    tag = event_id[:8]
    trace = TraceContext("1.0", f"req-{tag}", f"corr-{tag}", f"tr-{tag}", f"th-{tag}")
    return EventEnvelope(
        schema_version="1.0",
        event_id=event_id,
        event_type=event_type,
        event_version=1,
        tenant_id=tenant_id,
        request_id=trace.request_id,
        correlation_id=trace.correlation_id,
        trace_id=trace.trace_id,
        operation_id=None,
        causation_id=None,
        parent_event_id=None,
        producer=producer,
        occurred_at=datetime.now(timezone.utc).isoformat(),
        idempotency_key=idempotency_key,
        subject_id=payload.actor_subject_id,
        retry_count=0,
        payload=payload,
    )


def _notification_payload(
    notification_id: str, *, customer_id: str | None = "CUSTOMER-01"
) -> NotificationCreatedPayload:
    return NotificationCreatedPayload(
        notification_id=notification_id,
        channel="mock_sms",
        template_id="refund_approved",
        resource_refs=["ORDER-NT-01"],
        actor_subject_id="customer-north-01",
        customer_id=customer_id,
    )


class _Env:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.engine = create_mysql_engine(
            self.settings.mysql_url,
            self.settings.mysql_connect_timeout_s,
            self.settings.mysql_read_timeout_s,
        )
        self.outbox = OutboxRepository(self.engine, CATALOG)
        self.inbox = InboxRepository(self.engine, max_retries=CATALOG.max_retries)
        self.auth = MockAuthAdapter.from_fixture("data/fixtures/mock-identities.json", enabled=True)
        self._wipe()
        self._seed()

    def _wipe(self) -> None:
        with self.engine.begin() as conn:
            for statement in (
                "DELETE FROM notification WHERE tenant_id IN ('tenant-north','tenant-south')"
                " AND (notification_id LIKE 'nt-%' OR notification_id LIKE"
                " CONCAT('ntfan%', '%') OR LENGTH(notification_id)=32)",
                "DELETE FROM consumer_inbox WHERE consumer_name IN"
                " ('notification-consumer','address-followup-consumer','ticket-consumer',"
                "'handoff-consumer','refund-coordinator')",
                "DELETE FROM consumer_business_guard WHERE consumer_name IN"
                " ('notification-consumer','address-followup-consumer','ticket-consumer',"
                "'handoff-consumer','refund-coordinator')",
                "DELETE FROM outbox_event WHERE owner_module='notification-service'"
                " AND aggregate_type='notification'",
                "DELETE FROM refund_request WHERE refund_request_id LIKE 'RR-NT-%'",
                "DELETE FROM mock_oms_address_change WHERE order_id LIKE 'ORDER-NT-%'",
                "DELETE FROM operation_confirmation WHERE target_id='ORDER-NT-01'",
                "DELETE FROM mock_oms_order WHERE order_id LIKE 'ORDER-NT-%'",
            ):
                conn.execute(text(statement))

    def _seed(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO mock_oms_order (tenant_id,order_id,customer_id,status,"
                    "currency,paid_minor,version) VALUES ('tenant-north','ORDER-NT-01',"
                    "'CUSTOMER-01','pending_shipment','CNY',1000,7)"
                )
            )

    def seed_address_change(self, address_change_id: str, *, status: str = "succeeded") -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO operation_confirmation (tenant_id,confirmation_id,"
                    "actor_subject_id,customer_id,confirmer_subject_id,operation_type,"
                    "target_id,payload_digest,expected_revision,challenge_digest,status,"
                    "expires_at,created_at) VALUES ('tenant-north',:cf,"
                    "'customer-north-01','CUSTOMER-01','customer-north-01',"
                    "'shipping_address_change','ORDER-NT-01','seeded',7,'seeded','consumed',"
                    "DATE_ADD(NOW(6), INTERVAL 1 HOUR),NOW(6))"
                ),
                {"cf": f"cf-{address_change_id}"},
            )
            conn.execute(
                text(
                    "INSERT INTO mock_oms_address_change (tenant_id,address_change_id,order_id,"
                    "customer_id,actor_subject_id,expected_order_version,status,receiver_name,"
                    "contact_phone,province,city,district,detail_address,address_digest,"
                    "confirmation_id,idempotency_key,result_order_version,observed_at,"
                    "created_at,updated_at) VALUES ('tenant-north',:a,'ORDER-NT-01',"
                    "'CUSTOMER-01','customer-north-01',7,:status,X'00',X'00','浙江省','杭州市',"
                    "'西湖区',X'00','seeded',:cf,:idem,8,NOW(6),NOW(6),NOW(6))"
                ),
                {
                    "a": address_change_id,
                    "status": status,
                    "cf": f"cf-{address_change_id}",
                    "idem": f"idem-{address_change_id}",
                },
            )

    def seed_refund(
        self,
        refund_request_id: str,
        *,
        review_status: str = "approved",
        order_id: str = "ORDER-NT-01",
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO refund_request (tenant_id,refund_request_id,quote_id,order_id,"
                    "actor_subject_id,customer_id,confirmation_id,idempotency_key,reason_code,"
                    "eligibility,review_status,amount_minor,goods_minor,shipping_minor,currency,"
                    "policy_id,policy_version,order_version,payment_version,created_at,updated_at)"
                    " VALUES ('tenant-north',:r,CONCAT('q-',:r),:o,"
                    "'customer-north-01','CUSTOMER-01',CONCAT('cf-',:r),CONCAT('idem-',:r),"
                    "'七日无理由','manual_review',:status,1000,1000,0,'CNY','POL-7D','v1',"
                    "'7','7',NOW(6),NOW(6))"
                ),
                {"r": refund_request_id, "o": order_id, "status": review_status},
            )

    def notification_row(self, tenant_id: str, notification_id: str) -> dict | None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT notification_id,status,error,sent_at FROM notification"
                        " WHERE tenant_id=:t AND notification_id=:n"
                    ),
                    {"t": tenant_id, "n": notification_id},
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    def outbox_notification_events(self) -> list[dict]:
        with self.engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT payload FROM outbox_event WHERE owner_module="
                        "'notification-service' AND event_type='notification.created'"
                        " ORDER BY created_at"
                    )
                )
                .mappings()
                .all()
            )
            return [json.loads(row["payload"]) for row in rows]

    def consume(self, event: EventEnvelope, handler) -> str:
        outcome, lease = self.inbox.claim(
            event, CATALOG.entries[event.event_type]["consumer"], "worker-step28"
        )
        assert outcome == "claimed", outcome
        return self.inbox.complete(lease, handler)

    def close(self) -> None:
        self.engine.dispose()


@pytest.fixture(scope="module")
def env():
    environment = _Env()
    yield environment
    environment.close()


def test_01_send_success_then_redelivery_is_deduped(env):
    payload = _notification_payload("nt-ok-01")
    event = _envelope(
        "notification.created",
        "notification-service",
        payload,
        event_id="evt-nt-01",
        idempotency_key="idem-nt-01",
    )
    handler = NotificationSendHandler(MockNotificationSender())
    result_ref = env.consume(event, handler)
    assert result_ref == "notification:nt-ok-01:sent"
    row = env.notification_row("tenant-north", "nt-ok-01")
    assert row["status"] == "sent" and row["error"] is None and row["sent_at"] is not None
    # ACK 丢失重投：同一 event_id 再次 claim → duplicate；业务结果只提交一次。
    outcome, _ = env.inbox.claim(event, "notification-consumer", "worker-step28")
    assert outcome == "duplicate"
    assert env.notification_row("tenant-north", "nt-ok-01")["status"] == "sent"


def test_02_send_failure_records_status_without_retry_or_business_rollback(env):
    payload = _notification_payload("nt-fail-01")
    event = _envelope(
        "notification.created",
        "notification-service",
        payload,
        event_id="evt-nt-fail",
        idempotency_key="idem-nt-fail",
    )
    result_ref = env.consume(
        event, NotificationSendHandler(MockNotificationSender(behavior="failed"))
    )
    assert result_ref == "notification:nt-fail-01:failed"
    row = env.notification_row("tenant-north", "nt-fail-01")
    assert row["status"] == "failed" and row["error"] == "MOCK_CHANNEL_REJECTED"
    # 消费成功完成（无重试/死信）；已成功的业务（订单行）不受影响。
    outcome, _ = env.inbox.claim(event, "notification-consumer", "worker-step28")
    assert outcome == "duplicate"
    with env.engine.connect() as conn:
        order = conn.execute(
            text(
                "SELECT version,status FROM mock_oms_order WHERE tenant_id='tenant-north'"
                " AND order_id='ORDER-NT-01'"
            )
        ).one()
    assert order.status == "pending_shipment" and int(order.version) == 7


def test_03_unknown_result_is_pending_verification_not_fake_success(env):
    payload = _notification_payload("nt-unk-01")
    event = _envelope(
        "notification.created",
        "notification-service",
        payload,
        event_id="evt-nt-unk",
        idempotency_key="idem-nt-unk",
    )
    result_ref = env.consume(
        event, NotificationSendHandler(MockNotificationSender(behavior="unknown"))
    )
    assert result_ref == "notification:nt-unk-01:unknown"
    row = env.notification_row("tenant-north", "nt-unk-01")
    assert row["status"] == "unknown" and row["error"] == "MOCK_CHANNEL_TIMEOUT"


def test_04_fanout_event_chain_respects_module_boundary(env):
    env.seed_address_change("AC-NT-01", status="succeeded")
    payload = OrderShippingAddressUpdatedPayload(
        order_id="ORDER-NT-01",
        address_change_id="AC-NT-01",
        result_order_version=8,
        actor_subject_id="customer-north-01",
        customer_id="CUSTOMER-01",
    )
    event = _envelope(
        "order.shipping_address.updated",
        "order-service",
        payload,
        event_id="evt-addr-01",
        idempotency_key="idem-addr-01",
    )
    fanout = NotificationFanoutHandler(env.outbox)
    result_ref = env.consume(event, fanout)
    assert result_ref.startswith("notification_fanout:")
    notification_id = result_ref.split(":", 1)[1]
    # 模块边界：扇出只写共享 Outbox，不直接写通知表。
    assert env.notification_row("tenant-north", notification_id) is None
    events = env.outbox_notification_events()
    assert len(events) == 1
    created = parse_event(events[0])
    assert created.producer == "notification-service"
    assert created.payload.template_id == "address_change_confirmed"
    assert created.payload.resource_refs == ["ORDER-NT-01", "AC-NT-01"]
    assert created.payload.customer_id == "CUSTOMER-01"
    assert created.causation_id == "evt-addr-01"
    # 事件链下游：通知消费者在自己的消费事务内落库并发送。
    send_ref = env.consume(created, NotificationSendHandler(MockNotificationSender()))
    assert send_ref == f"notification:{notification_id}:sent"
    row = env.notification_row("tenant-north", notification_id)
    assert row["status"] == "sent"
    # 同一业务事件重放（同 event_id）→ inbox duplicate，不产生第二条通知事件。
    outcome, _ = env.inbox.claim(event, "address-followup-consumer", "worker-step28")
    assert outcome == "duplicate"
    assert len(env.outbox_notification_events()) == 1


def test_05_fanout_rejects_source_missing_or_wrong_status(env):
    env.seed_address_change("AC-NT-02", status="pending")
    payload = OrderShippingAddressUpdatedPayload(
        order_id="ORDER-NT-01",
        address_change_id="AC-NT-02",
        result_order_version=8,
        actor_subject_id="customer-north-01",
        customer_id="CUSTOMER-01",
    )
    event = _envelope(
        "order.shipping_address.updated",
        "order-service",
        payload,
        event_id="evt-addr-02",
        idempotency_key="idem-addr-02",
    )
    outcome, lease = env.inbox.claim(
        event, CATALOG.entries[event.event_type]["consumer"], "worker-step28"
    )
    assert outcome == "claimed" and lease is not None
    with pytest.raises(InboxConflictError):
        env.inbox.complete(lease, NotificationFanoutHandler(env.outbox))
    # 消费者失败路径：fail 落库进入重试/死信通道，供审计恢复核查。
    env.inbox.fail(lease, "InboxConflictError")
    with env.engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT status,attempts FROM consumer_inbox WHERE tenant_id='tenant-north'"
                " AND consumer_name='address-followup-consumer' AND event_id='evt-addr-02'"
            )
        ).one()
    assert row.status in ("failed", "dead_letter") and int(row.attempts) >= 1
    assert all(
        event["causation_id"] != "evt-addr-02"
        for event in env.outbox_notification_events()
    )


def test_06_same_business_key_different_event_id_commits_once(env):
    env.seed_refund("RR-NT-01", review_status="approved")

    from application.common.events import RefundApprovedPayload

    def refund_event(event_id: str) -> EventEnvelope:
        return _envelope(
            "refund.approved",
            "refund-review-use-case",
            RefundApprovedPayload(
                refund_request_id="RR-NT-01",
                revision=1,
                approved_amount_minor=1000,
                currency="CNY",
                return_status="unknown",
                review_subject_id="mock-refund-review",
                actor_subject_id="customer-north-01",
                customer_id="CUSTOMER-01",
            ),
            event_id=event_id,
            idempotency_key="refund-approved:RR-NT-01:1",
        )

    fanout = NotificationFanoutHandler(env.outbox)
    first = env.consume(refund_event("evt-rf-1"), fanout)
    # 不同 event_id 携带同一业务幂等键：business guard 去重，返回既有结果。
    second = env.consume(refund_event("evt-rf-2"), fanout)
    assert first == second
    refund_fanouts = [
        e for e in env.outbox_notification_events() if e["causation_id"].startswith("evt-rf-")
    ]
    assert len(refund_fanouts) == 1


def test_07_http_query_scope_labels_and_non_customer_denied(env):
    env.seed_refund("RR-NT-02", review_status="approved", order_id="ORDER-NT-02")

    from application.common.events import RefundApprovedPayload

    refund_event = _envelope(
        "refund.approved",
        "refund-review-use-case",
        RefundApprovedPayload(
            refund_request_id="RR-NT-02",
            revision=1,
            approved_amount_minor=1000,
            currency="CNY",
            return_status="unknown",
            review_subject_id="mock-refund-review",
            actor_subject_id="customer-north-01",
            customer_id="CUSTOMER-01",
        ),
        event_id="evt-rf-3",
        idempotency_key="refund-approved:RR-NT-02:1",
    )
    fanout_ref = env.consume(refund_event, NotificationFanoutHandler(env.outbox))
    created = parse_event(env.outbox_notification_events()[-1])
    env.consume(created, NotificationSendHandler(MockNotificationSender(behavior="unknown")))
    notification_id = fanout_ref.split(":", 1)[1]
    assert env.notification_row("tenant-north", notification_id)["status"] == "unknown"

    app = create_app(
        settings=env.settings.model_copy(update={"app_env": "test"}),
        chat_service=object(),
        readiness=object(),
        notification_engine=env.engine,
    )
    with TestClient(app) as client:
        login = client.post("/auth/mock/session", json={"account_id": "cust-north-01"})
        assert login.status_code == 200
        headers = {"Authorization": f"Bearer {login.json()['data']['session_token']}"}
        listing = client.get("/notifications", headers=headers)
        assert listing.status_code == 200
        body = listing.json()["data"]
        assert body["unknown_count"] >= 1
        mine = [n for n in body["notifications"] if n["notification_id"] == notification_id]
        assert mine and mine[0]["status"] == "unknown"
        assert mine[0]["status_label"] == "待核实"
        assert "MOCK_CHANNEL" not in json.dumps(mine[0], ensure_ascii=False)
        detail = client.get(f"/notifications/{notification_id}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["data"]["status_label"] == "待核实"
        # 不存在/他人通知统一不可见。
        stranger = client.get("/notifications/nt-does-not-exist", headers=headers)
        assert stranger.status_code == 404
        assert "CUSTOMER-91" not in json.dumps(listing.json(), ensure_ascii=False)
        # 非客户角色拒绝查询。
        staff = client.post("/auth/mock/session", json={"account_id": "support-north-01"})
        assert staff.status_code == 200
        staff_headers = {"Authorization": f"Bearer {staff.json()['data']['session_token']}"}
        denied = client.get("/notifications", headers=staff_headers)
        assert denied.status_code in (403, 404)
        # 未知状态过滤参数被拒绝（TC-78 未知字段/取值结构化校验）。
        bad_filter = client.get("/notifications?status=bogus", headers=headers)
        assert bad_filter.status_code in (400, 403, 404)
