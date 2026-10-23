"""Exercise the real business workbench UI against isolated synthetic services.

The browser test starts the existing lifecycle fixture in-process. The fixture
uses a temporary PostgreSQL database, the production gateway and worker, and a
loopback MCP service. Its rule route avoids external model calls and all test
records are discarded during teardown.
"""

from __future__ import annotations

import json
import sys
import threading
import time

import uvicorn
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect, sync_playwright
from psycopg import sql

from evals.integration.business_workbench_lifecycle_fixture import (
    HOST,
    PORT,
    _stop_mcp,
    build_fixture,
)


def _wait_for_server(server: uvicorn.Server, thread: threading.Thread) -> None:
    deadline = time.monotonic() + 15
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("Synthetic business gateway failed to start")


def _exercise_browser(url: str, metadata: dict[str, object], database) -> dict[str, object]:
    page_errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=15_000)
            page.locator("#employee-login").fill(str(metadata["login"]))
            page.locator("#employee-password").fill(str(metadata["password"]))
            page.get_by_role("button", name="登录", exact=True).click()
            expect(page.locator("#workbench")).to_be_visible(timeout=15_000)

            plugin = page.locator("#plugin-select")
            expect(plugin).to_have_value(str(metadata["scope_id"]), timeout=15_000)
            plugin.select_option(str(metadata["scope_id"]))
            page.locator("#strategy").select_option("rule")
            page.locator("#instruction").fill(str(metadata["instruction"]))
            page.get_by_role("button", name="提交任务", exact=True).click()

            approval = page.get_by_role("button", name="批准当前冻结参数", exact=True)
            expect(approval).to_be_visible(timeout=45_000)
            approval.click()

            expect(page.locator("#task-state")).to_contain_text("目标已完成", timeout=45_000)
            expect(page.locator("#result")).to_contain_text(
                "Requested operation verified", timeout=10_000
            )
            expect(page.locator("#actions")).to_contain_text("set_priority", timeout=10_000)
            expect(page.locator("#actions")).to_contain_text('"priority": "high"', timeout=10_000)

            if page_errors:
                raise AssertionError(f"Browser raised JavaScript errors: {page_errors}")
            with database.connect() as connection:
                task = connection.execute(
                    "SELECT id, state FROM app.tasks WHERE workspace_id=%s AND kind='business' "
                    "ORDER BY created_at DESC LIMIT 1",
                    (metadata["workspace_id"],),
                ).fetchone()
                if task is None:
                    raise AssertionError("The submitted browser task was not persisted")
                if task["state"] != "succeeded":
                    raise AssertionError(f"Task did not reach succeeded state: {task['state']}")
                ticket = connection.execute(
                    "SELECT priority FROM business.tickets WHERE id=%s AND workspace_id=%s",
                    (metadata["ticket_id"], metadata["workspace_id"]),
                ).fetchone()
                if ticket is None or ticket["priority"] != "high":
                    raise AssertionError("The submitted operation did not update its target ticket")
                usage = connection.execute(
                    "SELECT count(*) AS total FROM app.usage_records WHERE task_id=%s",
                    (task["id"],),
                ).fetchone()
                if usage["total"]:
                    raise AssertionError(
                        "The deterministic browser task unexpectedly dispatched a model request"
                    )

            return {
                "status": "passed",
                "flow": "employee login -> synthetic task -> explicit approval -> verified receipt",
                "task_state": page.locator("#task-state").inner_text(),
                "browser": "chromium",
                "paid_model_requests": usage["total"],
                "page_errors": page_errors,
            }
        except Exception:
            try:
                page_text = page.locator("body").inner_text(timeout=2_000)
                print("Browser page text on failure:\n" + page_text, file=sys.stderr)
            except PlaywrightError:
                pass
            raise
        finally:
            browser.close()


def main() -> int:
    fixture = build_fixture()
    server = uvicorn.Server(
        uvicorn.Config(fixture.app, host=HOST, port=PORT, lifespan="on", log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True, name="browser-e2e-gateway")
    thread.start()
    try:
        _wait_for_server(server, thread)
        result = _exercise_browser(
            str(fixture.metadata["url"]), fixture.metadata, fixture.database
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=5)
        gateway_still_running = thread.is_alive()
        try:
            _stop_mcp(fixture.mcp_service)
        finally:
            fixture.temporary.cleanup()
            with fixture.administrator.connect(autocommit=True) as connection:
                connection.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(fixture.database_name)
                    )
                )
        if gateway_still_running:
            raise RuntimeError("Synthetic business gateway failed to stop")


if __name__ == "__main__":
    raise SystemExit(main())
