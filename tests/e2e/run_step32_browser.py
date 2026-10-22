"""Run step 32's customer and staff browser entry checks against a live API."""

from __future__ import annotations

import argparse
import os

from playwright.sync_api import sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.getenv("STEP32_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--real-model", action="store_true")
    args = parser.parse_args()

    executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    with sync_playwright() as playwright:
        launch_options = {"headless": True}
        if executable:
            launch_options["executable_path"] = executable
        browser = playwright.chromium.launch(**launch_options)
        page_errors: list[str] = []

        customer = browser.new_page()
        customer.on("pageerror", lambda error: page_errors.append(str(error)))
        customer.goto(args.base_url, wait_until="domcontentloaded")
        customer.locator("#account-id").fill("cust-north-01")
        customer.locator('#login-form button[type="submit"]').click()
        customer.locator("#chat-panel").wait_for(state="visible", timeout=15_000)
        if args.real_model:
            customer.locator("#message").fill("你好，请简单介绍你可以帮我处理什么售后问题。")
            customer.locator("#send-button").click()
            customer.locator("#messages .message.assistant").wait_for(
                state="visible", timeout=90_000
            )
            if customer.locator("#chat-error").is_visible():
                raise AssertionError("real-model chat returned an API error")
            answer = customer.locator("#messages .message.assistant").last.inner_text()
            print(f"customer_real_model_chat=pass answer_chars={len(answer)}")
        else:
            print("customer_real_model_chat=not_requested")
        for route in ("refund", "order-address"):
            customer.locator(f'a[data-route="{route}"]').click()
            customer.locator("#page-root").wait_for(state="visible", timeout=10_000)
            print(f"customer_{route}=pass")

        staff = browser.new_page()
        staff.on("pageerror", lambda error: page_errors.append(str(error)))
        api_failures: list[str] = []
        staff.on(
            "response",
            lambda response: api_failures.append(f"{response.status} {response.url}")
            if "/workbench/" in response.url and response.status >= 400
            else None,
        )
        staff.goto(f"{args.base_url}/#/chat", wait_until="domcontentloaded")
        staff.locator("#account-id").fill("sup-north-01")
        staff.locator('#login-form button[type="submit"]').click()
        workbench = staff.locator('a[data-route="workbench"]')
        workbench.wait_for(state="visible", timeout=15_000)
        workbench.click()
        staff.locator(".workbench-page").wait_for(state="visible", timeout=15_000)
        staff.wait_for_timeout(750)
        print("staff_workbench=pass")

        browser.close()
        if page_errors:
            raise AssertionError(f"browser JavaScript errors: {page_errors!r}")
        if api_failures:
            raise AssertionError(f"workbench API errors: {api_failures!r}")
        print("browser_page_errors=0")


if __name__ == "__main__":
    main()
