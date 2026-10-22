"""步骤26 收货地址修改集成：真实 MySQL 上的资格、密文、确认、幂等与 MCP 契约。

覆盖 TC-57~64：资格判定、受控表单密文落库、会话绑定意图、版本条件更新、
幂等重放/冲突、退款互斥与事件出站。全程不伪造：失败路径如实断言。
"""

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from bootstrap.api_main import create_app
from bootstrap.settings import load_settings
from fastapi.testclient import TestClient
from sqlalchemy import text

from application.common.audit import AuditUseCase
from application.common.authorization import AuthorizationDenied
from application.common.context import AuthContext, DataScope, TraceContext
from application.common.events import parse_event
from application.common.operation_confirmation import OperationConfirmationUseCase, content_digest
from application.common.ports.confirmation import ConfirmationConflictError
from application.common.ports.idempotency import IdempotencyConflictError
from application.logistics.use_cases.logistics_query import QueryLogisticsUseCase
from application.order.use_cases.address_change import (
    AddressChangeApplyUseCase,
    AddressChangeUseCase,
    address_confirmation_binding,
    payload_address,
)
from application.order.use_cases.order_query import QueryOrderUseCase, QueryPaymentUseCase
from domain.order.address import ShippingAddress
from infrastructure.auth.mock_auth_adapter import MockAuthAdapter
from infrastructure.messaging.event_contract import EventCatalog
from infrastructure.messaging.outbox_repository import OutboxRepository
from infrastructure.mock.logistics_adapter import MockLogisticsAdapter
from infrastructure.mock.order_adapter import MockOrderAdapter
from infrastructure.observability.audit_repository import MySQLAuditRepository
from infrastructure.order.mysql_address_change import (
    AddressChangeConflict,
    AddressCrypto,
    MySqlAddressChangeStore,
    MySqlAddressSnapshotAdapter,
    MySqlAddressToolIntent,
    MySqlMockAddressChangeAdapter,
)
from infrastructure.persistence.mysql.connection import create_mysql_engine
from infrastructure.security.confirmation_store import MySQLConfirmationStore
from interface.mcp.context_guard import ToolContextGuard
from interface.mcp.tools import MCPToolDeps, execute_tool_call

MOCK_IDENTITIES_PATH = "data/fixtures/mock-identities.json"
MCP_TOOLS_PATH = "data/contracts/mcp-tools.json"
EVENT_CATALOG_PATH = "data/contracts/event-catalog.json"
EVENT_SCHEMA_PATH = "data/contracts/events.schema.json"

GOOD_ADDRESS = ShippingAddress(
    receiver_name="李四",
    contact_phone="13987654321",
    province="浙江省",
    city="杭州市",
    district="西湖区",
    detail_address="文三路200号",
)


class _Env:
    def __init__(self):
        self.settings = load_settings()
        self.engine = create_mysql_engine(
            self.settings.mysql_url,
            self.settings.mysql_connect_timeout_s,
            self.settings.mysql_read_timeout_s,
        )
        self.crypto = AddressCrypto(self.settings.address_encrypt_key)
        self.store = MySqlAddressChangeStore(self.engine, self.crypto)
        self.snapshots = MySqlAddressSnapshotAdapter(self.engine)
        self.intents = MySqlAddressToolIntent(self.engine)
        self.confirmations = MySQLConfirmationStore(self.engine)
        self.outbox = OutboxRepository(
            self.engine, EventCatalog(EVENT_CATALOG_PATH, EVENT_SCHEMA_PATH)
        )
        self.usecase = AddressChangeUseCase(
            self.snapshots,
            self.store,
            OperationConfirmationUseCase(
                self.confirmations, self.settings.address_confirm_ttl_seconds
            ),
            self.intents,
            ttl_seconds=self.settings.address_confirm_ttl_seconds,
        )
        self.apply = AddressChangeApplyUseCase(
            self.store, MySqlMockAddressChangeAdapter(self.engine, self.store, self.outbox)
        )
        self.auth = MockAuthAdapter.from_fixture(MOCK_IDENTITIES_PATH, enabled=True)
        self._wipe()
        self._seed()

    def _wipe(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE i FROM address_tool_intent i"
                    " JOIN mock_oms_order o ON o.tenant_id=i.tenant_id"
                    " AND o.order_id=i.order_id WHERE o.order_id LIKE 'ORDER-AD-%'"
                )
            )
            conn.execute(
                text(
                    "DELETE a FROM mock_oms_address_change a"
                    " JOIN mock_oms_order o ON o.tenant_id=a.tenant_id"
                    " AND o.order_id=a.order_id WHERE o.order_id LIKE 'ORDER-AD-%'"
                )
            )
            for statement in (
                "DELETE FROM operation_confirmation WHERE target_id LIKE 'ORDER-AD-%'",
                "DELETE FROM refund_order_guard WHERE order_id LIKE 'ORDER-AD-%'",
                "DELETE FROM operation_idempotency WHERE idempotency_key"
                " LIKE '%:shipping_address_change:%'",
                "DELETE FROM outbox_event WHERE aggregate_id LIKE 'ORDER-AD-%'",
                "DELETE FROM agent_audit_log WHERE action IN"
                " ('tool.check_shipping_address_change',"
                "'tool.query_shipping_address_change','tool.update_shipping_address')",
                "DELETE FROM tool_invocation WHERE tool_name IN"
                " ('check_shipping_address_change','query_shipping_address_change',"
                "'update_shipping_address')",
                "DELETE FROM mock_pay_payment WHERE order_id LIKE 'ORDER-AD-%'",
                "DELETE FROM mock_oms_order WHERE order_id LIKE 'ORDER-AD-%'",
            ):
                conn.execute(text(statement))

    def _seed(self) -> None:
        orders = [
            ("tenant-north", "ORDER-AD-01", "CUSTOMER-01", "pending_shipment", 5000, 5),
            ("tenant-north", "ORDER-AD-02", "CUSTOMER-01", "delivered", 3000, 2),
            ("tenant-north", "ORDER-AD-03", "CUSTOMER-01", "pending_shipment", 2000, 1),
            ("tenant-north", "ORDER-AD-04", "CUSTOMER-01", "pending_shipment", 1500, 1),
            ("tenant-north", "ORDER-AD-05", "CUSTOMER-01", "pending_shipment", 1200, 3),
            ("tenant-north", "ORDER-AD-06", "CUSTOMER-01", "pending_shipment", 1100, 1),
            ("tenant-north", "ORDER-AD-07", "CUSTOMER-01", "pending_shipment", 1000, 1),
            ("tenant-north", "ORDER-AD-08", "CUSTOMER-01", "pending_shipment", 800, 1),
            ("tenant-south", "ORDER-AD-91", "CUSTOMER-91", "pending_shipment", 900, 1),
        ]
        payments = {
            "ORDER-AD-01": "succeeded",
            "ORDER-AD-02": "succeeded",
            "ORDER-AD-03": "failed",
            "ORDER-AD-04": "succeeded",
            "ORDER-AD-05": "succeeded",
            "ORDER-AD-06": "succeeded",
            "ORDER-AD-07": "succeeded",
            "ORDER-AD-08": "succeeded",
            "ORDER-AD-91": "succeeded",
        }
        with self.engine.begin() as conn:
            for tenant, order_id, customer, status, paid, version in orders:
                conn.execute(
                    text(
                        "INSERT INTO mock_oms_order (tenant_id,order_id,customer_id,status,"
                        "currency,paid_minor,version) VALUES (:t,:o,:c,:s,'CNY',:p,:v)"
                    ),
                    {
                        "t": tenant,
                        "o": order_id,
                        "c": customer,
                        "s": status,
                        "p": paid,
                        "v": version,
                    },
                )
                conn.execute(
                    text(
                        "INSERT INTO mock_pay_payment (tenant_id,payment_id,order_id,"
                        "paid_minor,currency,status) VALUES (:t,:p,:o,:paid,'CNY',:s)"
                    ),
                    {
                        "t": tenant,
                        "p": f"PAY-AD-{order_id.split('-')[-1]}",
                        "o": order_id,
                        "paid": paid,
                        "s": payments[order_id],
                    },
                )
            conn.execute(
                text(
                    "INSERT INTO refund_order_guard (tenant_id,order_id,active_request_id,"
                    "reserved_goods_minor,reserved_shipping_minor,currency,revision,"
                    "created_at,updated_at) VALUES ('tenant-north','ORDER-AD-04',"
                    "'refund-guard-x',10,0,'CNY',1,NOW(6),NOW(6))"
                )
            )

    def customer(self, *, session: str = "addr-session-1", subject="customer-north-01"):
        now = datetime.now(timezone.utc)
        return AuthContext(
            tenant_id="tenant-north" if subject == "customer-north-01" else "tenant-south",
            subject_id=subject,
            roles=("customer",),
            scopes=("order:read", "order:address:update"),
            data_scope=DataScope(customer_ids=("CUSTOMER-01",)),
            session_id=session,
            issued_at=now,
            expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )

    def south_customer(self):
        now = datetime.now(timezone.utc)
        return AuthContext(
            tenant_id="tenant-south",
            subject_id="customer-south-91",
            roles=("customer",),
            scopes=("order:read", "order:address:update"),
            data_scope=DataScope(customer_ids=("CUSTOMER-91",)),
            session_id="addr-session-south",
            issued_at=now,
            expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )

    def trace(self, tag: str) -> TraceContext:
        return TraceContext(
            "1.0", f"req-{tag}", f"corr-{tag}", f"trace-{tag}", f"thread-{tag}"
        )

    def mcp_deps(self) -> MCPToolDeps:
        order_query = QueryOrderUseCase(MockOrderAdapter(self.engine))
        return MCPToolDeps(
            auth_gateway=self.auth,
            guard=ToolContextGuard(MCP_TOOLS_PATH),
            query_order=order_query,
            query_payment=QueryPaymentUseCase(MockOrderAdapter(self.engine)),
            audit=AuditUseCase(MySQLAuditRepository(self.engine)),
            query_logistics=QueryLogisticsUseCase(
                order_query,
                MockLogisticsAdapter(Path("data/mock/v2/logistics.json")),
            ),
            address_change=self.usecase,
            address_apply=self.apply,
            address_tool_intent=self.intents,
        )

    def mcp_headers(self, account_id: str) -> dict:
        grant = self.auth.issue_mock_session(account_id)
        return {"authorization": f"Bearer {grant.session_token}"}

    def order_version(self, order_id: str) -> int:
        with self.engine.connect() as conn:
            return int(
                conn.execute(
                    text(
                        "SELECT version FROM mock_oms_order WHERE tenant_id='tenant-north'"
                        " AND order_id=:o"
                    ),
                    {"o": order_id},
                ).scalar_one()
            )

    def close(self) -> None:
        self.engine.dispose()


@pytest.fixture(scope="module")
def env():
    environment = _Env()
    yield environment
    environment.close()


def _prepare(env, order_id="ORDER-AD-01", address=GOOD_ADDRESS, session="addr-session-1"):
    return env.usecase.prepare(env.customer(session=session), order_id, address)


def _verify(env, prepared, session: str) -> None:
    env.usecase.verify(
        env.customer(session=session),
        prepared.record.confirmation_id,
        prepared.confirmation.mock_challenge,
    )


class TestStep26AddressChange:
    def test_01_check_eligibility_and_denials(self, env):
        auth = env.customer()
        ok = env.usecase.check(auth, "ORDER-AD-01")
        assert ok["eligible"] is True
        assert ok["order_version"] == "5"
        assert ok["denial_reasons"] == []
        assert ok["confirmation_required"] is True
        assert ok["evidence_refs"]
        delivered = env.usecase.check(auth, "ORDER-AD-02")
        assert delivered["eligible"] is False
        assert delivered["denial_reasons"] == ["ORDER_NOT_PAID_PENDING_SHIPMENT"]
        unpaid = env.usecase.check(auth, "ORDER-AD-03")
        assert unpaid["denial_reasons"] == ["PAYMENT_NOT_SUCCEEDED"]
        refunding = env.usecase.check(auth, "ORDER-AD-04")
        assert refunding["denial_reasons"] == ["ORDER_HAS_ACTIVE_REFUND"]
        with pytest.raises(AuthorizationDenied):
            env.usecase.check(env.south_customer(), "ORDER-AD-01")
        with pytest.raises(AuthorizationDenied):
            env.usecase.check(auth, "ORDER-AD-NONE")

    def test_02_prepare_stores_ciphertext_only_and_registers_intent(self, env):
        prepared = _prepare(env)
        record = prepared.record
        assert record.status == "pending"
        assert record.expected_order_version == 5
        masked = record.address.masked_summary()
        assert "13987654321" not in masked and "文三路200号" not in masked
        with env.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT receiver_name,contact_phone,detail_address,address_digest"
                    " FROM mock_oms_address_change"
                    " WHERE tenant_id='tenant-north' AND address_change_id=:a"
                ),
                {"a": record.address_change_id},
            ).first()
            intent_rows = conn.execute(
                text(
                    "SELECT COUNT(*) FROM address_tool_intent"
                    " WHERE tenant_id='tenant-north' AND address_change_id=:a"
                ),
                {"a": record.address_change_id},
            ).scalar_one()
        assert GOOD_ADDRESS.contact_phone.encode() not in bytes(row.contact_phone)
        assert GOOD_ADDRESS.detail_address.encode() not in bytes(row.detail_address)
        assert row.address_digest == content_digest(payload_address(GOOD_ADDRESS))
        assert intent_rows == 1
        # 停留在 prepared 的确认不允许解析意图，也不允许提交。
        with pytest.raises(ConfirmationConflictError):
            env.intents.resolve(env.customer(session="addr-session-1"), record.order_id)
        with pytest.raises(ConfirmationConflictError):
            env.apply.apply(
                env.customer(session="addr-session-1"),
                record.order_id,
                record.address_change_id,
                record.confirmation_id,
                record.idempotency_key,
                env.trace("addr-no-verify"),
            )

    def test_03_apply_success_event_and_replay(self, env):
        prepared = _prepare(env, session="addr-session-3")
        record = prepared.record
        _verify(env, prepared, "addr-session-3")
        result = env.apply.apply(
            env.customer(session="addr-session-3"),
            record.order_id,
            record.address_change_id,
            record.confirmation_id,
            record.idempotency_key,
            env.trace("addr-apply-1"),
        )
        assert result.status == "succeeded" and not result.replayed
        assert result.result_order_version == 6
        assert env.order_version("ORDER-AD-01") == 6
        with env.engine.connect() as conn:
            events = conn.execute(
                text(
                    "SELECT event_type,status,payload FROM outbox_event"
                    " WHERE tenant_id='tenant-north' AND aggregate_type='order'"
                    " AND aggregate_id='ORDER-AD-01'"
                )
            ).all()
            idem = conn.execute(
                text(
                    "SELECT status,result_ref FROM operation_idempotency"
                    " WHERE tenant_id='tenant-north' AND idempotency_key=:k"
                ),
                {"k": record.idempotency_key},
            ).first()
        confirmation_status = env.confirmations.get(
            "tenant-north", record.confirmation_id
        ).status
        assert len(events) == 1
        assert events[0].event_type == "order.shipping_address.updated"
        assert events[0].status == "pending"
        payload = parse_event(json.loads(events[0].payload)).payload
        assert payload.address_change_id == record.address_change_id
        assert payload.result_order_version == 6
        assert payload.customer_id == "CUSTOMER-01"
        assert confirmation_status == "consumed"
        assert idem.status == "succeeded" and idem.result_ref == record.address_change_id
        replay = env.apply.apply(
            env.customer(session="addr-session-3"),
            record.order_id,
            record.address_change_id,
            record.confirmation_id,
            record.idempotency_key,
            env.trace("addr-apply-1-retry"),
        )
        assert replay.replayed is True
        assert replay.address_change_id == record.address_change_id
        assert env.order_version("ORDER-AD-01") == 6
        query = env.usecase.query(env.customer(), record.address_change_id)
        assert query["status"] == "succeeded"
        assert query["result_order_version"] == 6

    def test_04_version_conflict_blocks_then_recovers(self, env):
        prepared = _prepare(env, order_id="ORDER-AD-05", session="addr-session-4")
        record = prepared.record
        auth = env.customer(session="addr-session-4")
        _verify(env, prepared, "addr-session-4")
        with env.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE mock_oms_order SET version=version+1"
                    " WHERE tenant_id='tenant-north' AND order_id='ORDER-AD-05'"
                )
            )
        assert env.order_version("ORDER-AD-05") == 4
        with pytest.raises(AddressChangeConflict, match="版本"):
            env.apply.apply(
                auth, record.order_id, record.address_change_id,
                record.confirmation_id, record.idempotency_key, env.trace("addr-version"),
            )
        with env.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE mock_oms_order SET version=3"
                    " WHERE tenant_id='tenant-north' AND order_id='ORDER-AD-05'"
                )
            )
        result = env.apply.apply(
            auth, record.order_id, record.address_change_id,
            record.confirmation_id, record.idempotency_key, env.trace("addr-version"),
        )
        assert result.status == "succeeded" and result.result_order_version == 4
        assert env.order_version("ORDER-AD-05") == 4

    def test_05_intent_is_session_bound_and_confirmation_required(self, env):
        prepared = _prepare(env, session="addr-session-5")
        record = prepared.record
        auth = env.customer(session="addr-session-5")
        _verify(env, prepared, "addr-session-5")
        with pytest.raises(ConfirmationConflictError):
            env.intents.resolve(env.customer(session="other-session"), record.order_id)
        intent = env.intents.resolve(auth, record.order_id)
        assert intent.address_change_id == record.address_change_id
        assert intent.confirmation_id == record.confirmation_id
        assert intent.idempotency_key == record.idempotency_key
        with pytest.raises(AddressChangeConflict):
            env.apply.apply(
                auth, record.order_id, record.address_change_id,
                "fabricated-confirmation", intent.idempotency_key, env.trace("addr-forged"),
            )
        with pytest.raises(AddressChangeConflict):
            env.apply.apply(
                auth, record.order_id, record.address_change_id,
                record.confirmation_id, "fabricated-key", env.trace("addr-forged-key"),
            )
        with pytest.raises(AuthorizationDenied):
            env.apply.apply(
                env.south_customer(), record.order_id, record.address_change_id,
                record.confirmation_id, record.idempotency_key, env.trace("addr-cross"),
            )

    def test_06_same_key_different_content_conflicts(self, env):
        prepared = _prepare(env, order_id="ORDER-AD-05", session="addr-session-6")
        record = prepared.record
        auth = env.customer(session="addr-session-6")
        _verify(env, prepared, "addr-session-6")
        first = env.apply.apply(
            auth, record.order_id, record.address_change_id,
            record.confirmation_id, record.idempotency_key, env.trace("addr-idem"),
        )
        assert first.status == "succeeded"
        tampered = replace(
            record,
            address=replace(record.address, detail_address="另一条路9号"),
        )
        tampered = replace(tampered, address_digest=content_digest(payload_address(
            tampered.address
        )))
        binding = address_confirmation_binding(auth, tampered)
        adapter = MySqlMockAddressChangeAdapter(env.engine, env.store, env.outbox)
        with pytest.raises(IdempotencyConflictError):
            adapter.commit(
                auth, tampered, binding, record.confirmation_id,
                record.idempotency_key, env.trace("addr-idem"),
            )

    def test_07_verified_exclusivity_and_refund_mutex(self, env):
        first = _prepare(env, order_id="ORDER-AD-06", session="addr-session-7")
        second = _prepare(
            env,
            order_id="ORDER-AD-06",
            address=replace(GOOD_ADDRESS, detail_address="文二路8号"),
            session="addr-session-8",
        )
        auth7 = env.customer(session="addr-session-7")
        auth8 = env.customer(session="addr-session-8")
        # 未验证的待办单不阻塞他人；但提交前必须完成一次性验证。
        _verify(env, second, "addr-session-8")
        result = env.apply.apply(
            auth8, second.record.order_id, second.record.address_change_id,
            second.record.confirmation_id, second.record.idempotency_key,
            env.trace("addr-exclusive"),
        )
        assert result.status == "succeeded"
        _verify(env, first, "addr-session-7")
        with pytest.raises(AddressChangeConflict, match="版本"):
            env.apply.apply(
                auth7, first.record.order_id, first.record.address_change_id,
                first.record.confirmation_id, first.record.idempotency_key,
                env.trace("addr-exclusive-first"),
            )
        # 已验证待办单会阻塞同订单的其他已验证提交（互斥以 verified 为界）。
        third = _prepare(
            env,
            order_id="ORDER-AD-06",
            address=replace(GOOD_ADDRESS, detail_address="文一路1号"),
            session="addr-session-7b",
        )
        _verify(env, third, "addr-session-7b")
        with pytest.raises(AddressChangeConflict, match="待处理"):
            env.apply.apply(
                auth8, third.record.order_id, third.record.address_change_id,
                third.record.confirmation_id, third.record.idempotency_key,
                env.trace("addr-exclusive-third"),
            )
        late = _prepare(env, order_id="ORDER-AD-07", session="addr-session-9")
        with env.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO refund_order_guard (tenant_id,order_id,active_request_id,"
                    "reserved_goods_minor,reserved_shipping_minor,currency,revision,"
                    "created_at,updated_at) VALUES ('tenant-north','ORDER-AD-07',"
                    "'refund-late',5,0,'CNY',0,NOW(6),NOW(6))"
                )
            )
        _verify(env, late, "addr-session-9")
        with pytest.raises(AddressChangeConflict, match="退款"):
            env.apply.apply(
                env.customer(session="addr-session-9"),
                late.record.order_id,
                late.record.address_change_id,
                late.record.confirmation_id,
                late.record.idempotency_key,
                env.trace("addr-refund-mutex"),
            )

    def test_08_http_controlled_form_and_mcp_write(self, env):
        app = create_app(
            settings=env.settings.model_copy(update={"app_env": "test"}),
            chat_service=object(),
            readiness=object(),
            address_change=env.usecase,
            address_apply=env.apply,
        )
        with TestClient(app) as client:
            login = client.post("/auth/mock/session", json={"account_id": "cust-north-01"})
            assert login.status_code == 200
            token = login.json()["data"]["session_token"]
            headers = {"Authorization": f"Bearer {token}"}
            created = client.post(
                "/orders/ORDER-AD-05/shipping-address-changes",
                headers=headers,
                json={
                    "schema_version": "1.0",
                    "receiver_name": "王五",
                    "contact_phone": "13611112222",
                    "province": "江苏省",
                    "city": "南京市",
                    "district": "鼓楼区",
                    "detail_address": "中山北路1号",
                },
            )
            assert created.status_code == 201
            body = created.json()["data"]
            version_before = env.order_version("ORDER-AD-05")
            assert body["status"] == "pending"
            assert body["expected_order_version"] == version_before
            assert "13611112222" not in json.dumps(created.json(), ensure_ascii=False)
            assert "中山北路1号" not in json.dumps(created.json(), ensure_ascii=False)
            assert body["masked_summary"].startswith("王*")
            verified = client.post(
                f"/shipping-address-confirmations/{body['confirmation_id']}/verify",
                headers=headers,
                json={"schema_version": "1.0", "challenge": body["mock_challenge"]},
            )
            assert verified.status_code == 200
            status = client.get(
                f"/shipping-address-changes/{body['address_change_id']}", headers=headers
            )
            assert status.status_code == 200
            assert status.json()["data"]["status"] == "pending"
            outsider = client.post("/auth/mock/session", json={"account_id": "cust-north-02"})
            other_token = outsider.json()["data"]["session_token"]
            denied = client.get(
                f"/shipping-address-changes/{body['address_change_id']}",
                headers={"Authorization": f"Bearer {other_token}"},
            )
            assert denied.status_code == 404
            mcp_result = execute_tool_call(
                "update_shipping_address",
                {
                    "order_id": "ORDER-AD-05",
                    "address_change_id": body["address_change_id"],
                },
                deps=env.mcp_deps(),
                headers={"authorization": f"Bearer {token}"},
            )
            assert mcp_result["status"] == "success", mcp_result["error"]
            data = mcp_result["data"]
            assert data["address_change_id"] == body["address_change_id"]
            assert data["status"] == "succeeded"
            assert data["result_order_version"] == version_before + 1
            assert data["error_code"] is None
            final = client.get(
                f"/shipping-address-changes/{body['address_change_id']}", headers=headers
            )
            assert final.json()["data"]["status"] == "succeeded"
            assert final.json()["data"]["result_order_version"] == version_before + 1
            assert env.order_version("ORDER-AD-05") == version_before + 1

    def test_09_mcp_contract_shapes_and_denials(self, env):
        deps = env.mcp_deps()
        check = execute_tool_call(
            "check_shipping_address_change",
            {"order_id": "ORDER-AD-05"},
            deps=deps,
            headers=env.mcp_headers("cust-north-01"),
        )
        assert check["status"] == "success"
        assert set(check["data"]) == {
            "eligible",
            "order_id",
            "order_version",
            "denial_reasons",
            "confirmation_required",
            "evidence_refs",
        }
        grant = env.auth.issue_mock_session("cust-north-01")
        mcp_auth = env.auth.resolve(grant.session_token)
        prepared = env.usecase.prepare(mcp_auth, "ORDER-AD-08", GOOD_ADDRESS)
        record = prepared.record
        query = execute_tool_call(
            "query_shipping_address_change",
            {"address_change_id": record.address_change_id},
            deps=deps,
            headers=env.mcp_headers("cust-north-01"),
        )
        assert set(query["data"]) == {
            "address_change_id",
            "order_id",
            "status",
            "expected_order_version",
            "result_order_version",
            "observed_at",
        }
        assert query["data"]["status"] == "pending"
        naked = execute_tool_call(
            "update_shipping_address",
            {"order_id": "ORDER-AD-05", "address_change_id": "addr-fabricated"},
            deps=deps,
            headers=env.mcp_headers("cust-north-01"),
        )
        assert naked["status"] == "error"
        assert naked["error"]["error_code"] == "CONFIRMATION_INVALID"
        forged = execute_tool_call(
            "update_shipping_address",
            {"order_id": record.order_id, "address_change_id": record.address_change_id},
            deps=deps,
            headers=env.mcp_headers("cust-north-02"),
        )
        assert forged["status"] == "error"
        assert forged["error"]["error_code"] == "CONFIRMATION_INVALID"
        env.usecase.verify(
            mcp_auth, record.confirmation_id, prepared.confirmation.mock_challenge
        )
        applied = execute_tool_call(
            "update_shipping_address",
            {"order_id": record.order_id, "address_change_id": record.address_change_id},
            deps=deps,
            headers={"authorization": f"Bearer {grant.session_token}"},
        )
        assert applied["status"] == "success", applied["error"]
        assert applied["data"]["status"] == "succeeded"
        with env.engine.connect() as conn:
            audits = conn.execute(
                text(
                    "SELECT status FROM agent_audit_log WHERE tenant_id='tenant-north'"
                    " AND action='tool.update_shipping_address'"
                    " ORDER BY created_at DESC LIMIT 3"
                )
            ).all()
        assert any(row.status == "success" for row in audits)
