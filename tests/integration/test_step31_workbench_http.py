from datetime import datetime, timezone

from bootstrap.api_main import create_app
from bootstrap.settings import load_settings
from fastapi.testclient import TestClient

from application.workbench.queries import WorkbenchPage, WorkbenchQueries


class FakeWorkbenchReads:
    def customers(self, tenant_id, customer_ids, query, offset, limit):
        return WorkbenchPage(
            [{"customer_id": value, "display_name_masked": "客户 **"} for value in customer_ids],
            None,
        )

    def tickets(self, tenant_id, customer_ids, filters, offset, limit):
        if "CUSTOMER-01" not in customer_ids:
            return WorkbenchPage([], None)
        return WorkbenchPage(
            [
                {
                    "ticket_id": "ticket-1",
                    "customer_id": "CUSTOMER-01",
                    "status": "open",
                    "priority": "normal",
                    "revision": 1,
                    "assigned_to": None,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            ],
            None,
        )

    def ticket_detail(self, tenant_id, ticket_id):
        return {
            "ticket_id": ticket_id,
            "customer_id": "CUSTOMER-01",
            "title": "问题",
            "messages": [{"visibility": "internal", "content_masked": "员工备注"}],
        }

    def handoffs(self, tenant_id, customer_ids, status, offset, limit):
        return WorkbenchPage([], None)

    def refunds(self, tenant_id, customer_ids, status, customer_id, offset, limit):
        return WorkbenchPage([], None)


def _headers(client, account_id):
    response = client.post("/auth/mock/session", json={"account_id": account_id})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['data']['session_token']}"}


def test_workbench_route_allows_staff_but_denies_customer_and_admin():
    app = create_app(
        settings=load_settings(),
        readiness=object(),
        chat_service=object(),
        workbench_queries=WorkbenchQueries(FakeWorkbenchReads()),
    )
    with TestClient(app) as client:
        staff = _headers(client, "sup-north-01")
        customers = client.get("/workbench/customers", headers=staff)
        assert customers.status_code == 200
        assert customers.json()["data"]["items"][0]["display_name_masked"] == "客户 **"
        detail = client.get("/workbench/tickets/ticket-1", headers=staff)
        assert detail.status_code == 200
        assert detail.json()["data"]["messages"][0]["visibility"] == "internal"

        for account_id in ("cust-north-01", "admin-north-01"):
            headers = _headers(client, account_id)
            response = client.get("/workbench/tickets/ticket-1", headers=headers)
            assert response.status_code == 404
            assert response.json()["error"]["error_code"] == "RESOURCE_UNAVAILABLE"
