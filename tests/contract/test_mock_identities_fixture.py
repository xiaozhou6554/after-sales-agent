"""Mock 身份 fixture 契约测试（技术详细规格 27.3：受控 fixture 为一期授权来源）。

fixture 是受控契约：字段、角色、scope、范围标记任何漂移都在此失败；
非法变体（客户端自行扩权、混用系统范围、未知角色/scope）必须被装载器拒绝。
"""

from __future__ import annotations

import copy
import json

import pytest
from bootstrap.container import MOCK_IDENTITIES_PATH

from application.common.authorization import KNOWN_ROLES, KNOWN_SCOPES
from infrastructure.auth.mock_auth_adapter import (
    MockIdentityRecord,
    load_mock_identities,
    parse_mock_identities,
)


def test_real_fixture_loads_and_covers_roles_and_tenants() -> None:
    records = load_mock_identities(MOCK_IDENTITIES_PATH)
    assert len(records) >= 9
    roles = {record.role for record in records.values()}
    assert roles == KNOWN_ROLES
    tenants = {record.tenant_id for record in records.values()}
    assert tenants == {"tenant-north", "tenant-south"}


def test_real_fixture_admin_is_minimal_and_customers_are_self_scoped() -> None:
    records = load_mock_identities(MOCK_IDENTITIES_PATH)
    # admin 显式 audit:read，不自动继承业务写权限（27.2）。
    assert records["admin-north-01"].scopes == ("audit:read",)
    # customer 账户范围只能是自己：唯一 allowed_customer_ids、无团队。
    for account_id, record in records.items():
        if record.role == "customer":
            assert len(record.allowed_customer_ids) == 1, account_id
            assert record.team_ids == (), account_id
    assert records["cust-north-01"].allowed_customer_ids == ("CUSTOMER-01",)
    assert records["cust-south-91"].allowed_customer_ids == ("CUSTOMER-91",)


def test_real_fixture_system_scope_marker_only_for_ops_and_admin() -> None:
    records = load_mock_identities(MOCK_IDENTITIES_PATH)
    for account_id, record in records.items():
        if "*" in record.allowed_customer_ids:
            assert record.role in ("operations", "admin"), account_id
            assert record.allowed_customer_ids == ("*",), account_id
    assert records["ops-north-01"].allowed_customer_ids == ("*",)


def test_real_fixture_scopes_subset_of_known_registry() -> None:
    records = load_mock_identities(MOCK_IDENTITIES_PATH)
    for record in records.values():
        assert set(record.scopes) <= KNOWN_SCOPES


def _base_account() -> dict:
    return {
        "tenant_id": "tenant-north",
        "account_id": "cust-x",
        "subject_id": "subject-x",
        "role": "customer",
        "scopes": ["order:read"],
        "team_ids": [],
        "allowed_customer_ids": ["CUSTOMER-01"],
    }


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda a: a.update({"roles": ["admin"]}), "未知字段"),
        (lambda a: a.update({"role": "root"}), "未知角色"),
        (lambda a: a.update({"scopes": ["order:write"]}), "未注册 scope"),
        (lambda a: a.update({"scopes": []}), "scopes 不能为空"),
        (
            lambda a: a.update({"allowed_customer_ids": ["CUSTOMER-01", "CUSTOMER-02"]}),
            "本人客户范围不唯一",
        ),
        (lambda a: a.update({"team_ids": ["team-x"]}), "customer 角色但携带团队范围"),
        (lambda a: a.update({"account_id": ""}), "必须为非空字符串"),
        (lambda a: a.update({"scopes": ["order:read", "order:read"]}), "重复项"),
    ],
)
def test_account_variants_rejected(mutate, match) -> None:
    account = _base_account()
    mutate(account)
    with pytest.raises(ValueError, match=match):
        parse_mock_identities({"schema_version": "1.0", "accounts": [account]})


def test_system_scope_marker_rejected_for_support() -> None:
    account = _base_account()
    account.update(
        {
            "role": "support",
            "team_ids": ["team-north-support"],
            "allowed_customer_ids": ["*"],
        }
    )
    with pytest.raises(ValueError, match="系统范围标记"):
        parse_mock_identities({"schema_version": "1.0", "accounts": [account]})


def test_duplicate_account_or_subject_rejected() -> None:
    raw = {"schema_version": "1.0", "accounts": [_base_account(), _base_account()]}
    with pytest.raises(ValueError, match="account_id 重复"):
        parse_mock_identities(raw)

    other = _base_account()
    other["account_id"] = "cust-y"
    raw = {"schema_version": "1.0", "accounts": [_base_account(), other]}
    with pytest.raises(ValueError, match="subject_id 重复"):
        parse_mock_identities(raw)


def test_unknown_top_level_field_and_schema_version_rejected() -> None:
    raw = {"schema_version": "1.0", "accounts": [_base_account()], "extra": True}
    with pytest.raises(ValueError, match="未知字段"):
        parse_mock_identities(raw)
    raw = {"schema_version": "2.0", "accounts": [_base_account()]}
    with pytest.raises(ValueError, match="schema_version"):
        parse_mock_identities(raw)


def test_loader_reads_utf8_fixture_file(tmp_path) -> None:
    payload = {"schema_version": "1.0", "accounts": [_base_account()]}
    path = tmp_path / "identities.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    records = load_mock_identities(path)
    assert isinstance(records["cust-x"], MockIdentityRecord)
    assert copy.deepcopy(records["cust-x"].scopes) == ("order:read",)
