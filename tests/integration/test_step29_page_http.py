"""步骤29骨架：API 同源提供聊天页与模块脚本。"""

from bootstrap.api_main import create_app
from bootstrap.settings import load_settings
from fastapi.testclient import TestClient


def test_customer_page_and_scripts_are_served_on_api_origin() -> None:
    app = create_app(
        settings=load_settings(),
        readiness=object(),
        chat_service=object(),
        task_service=object(),
    )
    with TestClient(app) as client:
        page = client.get("/")
        script = client.get("/src/pages/chat/app.js")
    assert page.status_code == 200
    assert "售后服务" in page.text
    assert script.status_code == 200
    assert "sendChat" in script.text
