"""Exercise the real guest workbench session lifecycle in Chromium.

The fixture uses a fresh PostgreSQL database, the production gateway, loopback
HTTP, and synthetic workspace rows. It does not start a task worker or contact
any model provider.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

import uvicorn
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect, sync_playwright
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from decagent.storage.database import Database
from evals.integration.business_guest_browser_fixture import (
    HOST,
    PORT,
    WORKBENCH_PATH,
    build_fixture,
)


def _wait_for_server(server: uvicorn.Server, thread: threading.Thread) -> None:
    deadline = time.monotonic() + 15
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("Synthetic guest gateway failed to start")


def _identity_row(database: Database, login: str):
    with database.connect() as connection:
        row = connection.execute(
            "SELECT a.workspace_id,a.role,w.kind FROM business.employee_accounts a "
            "JOIN business.workspaces w ON w.id=a.workspace_id WHERE a.login=%s",
            (login,),
        ).fetchone()
    if row is None:
        raise AssertionError(f"The guest identity {login!r} is not persisted")
    return row


def _exercise_browser(url: str, database: Database) -> dict[str, object]:
    page_errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context()
        page = context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=15_000)
            expect(page.locator("#guest-entry")).to_be_visible(timeout=10_000)
            expect(page.locator("#workbench")).to_be_hidden()

            page.get_by_role("button", name="开始游客体验", exact=True).click()
            expect(page.locator("#workbench")).to_be_visible(timeout=15_000)
            expect(page.locator("#guest-role-control")).to_be_visible()
            expect(page.locator("#current-identity")).to_contain_text("employee-it")

            first_login = page.locator("#current-identity").inner_text().split(" · ", 1)[0]
            first_identity = _identity_row(database, first_login)
            assert first_identity["kind"] == "guest"
            assert first_identity["role"] == "employee"

            page.locator("#guest-role").select_option("supervisor")
            expect(page.locator("#current-identity")).to_contain_text("supervisor")
            switched_login = page.locator("#current-identity").inner_text().split(" · ", 1)[0]
            switched_identity = _identity_row(database, switched_login)
            assert switched_identity["workspace_id"] == first_identity["workspace_id"]
            assert switched_identity["kind"] == "guest"
            assert switched_identity["role"] == "supervisor"

            page.reload(wait_until="domcontentloaded")
            expect(page.locator("#workbench")).to_be_visible(timeout=15_000)
            expect(page.locator("#guest-role")).to_have_value("supervisor")
            expect(page.locator("#current-identity")).to_contain_text("supervisor")

            page.get_by_role("button", name="清空游客空间", exact=True).click()
            expect(page.locator("#workbench")).to_be_hidden(timeout=10_000)
            expect(page.locator("#login-panel")).to_be_visible()
            expect(page.locator("#guest-entry")).to_be_visible()
            expect(page.locator("#notice")).to_contain_text("游客空间已停用")
            session_status = page.evaluate(
                "async () => (await fetch('/api/v3/guest/session', "
                "{credentials: 'same-origin'})).status"
            )
            assert session_status == 401, "cleared guest cookies must not restore a session"

            with database.connect() as connection:
                workspace = connection.execute(
                    "SELECT active FROM business.workspaces WHERE id=%s AND kind='guest'",
                    (first_identity["workspace_id"],),
                ).fetchone()
                session = connection.execute(
                    "SELECT count(*) AS active_sessions FROM business.guest_sessions "
                    "WHERE workspace_id=%s AND revoked_at IS NULL AND expires_at>now()",
                    (first_identity["workspace_id"],),
                ).fetchone()
            assert workspace is not None and workspace["active"] is False
            assert session["active_sessions"] == 0
            if page_errors:
                raise AssertionError(f"Browser raised JavaScript errors: {page_errors}")

            return {
                "status": "passed",
                "flow": "guest start -> role switch -> same-browser restore -> clear",
                "workspace_deactivated": True,
                "active_sessions_after_clear": session["active_sessions"],
                "session_restore_after_clear": session_status,
                "model_requests": 0,
                "browser": "chromium",
                "page_errors": page_errors,
            }
        except Exception:
            try:
                page_text = page.locator("body").inner_text(timeout=2_000)
                print("Guest browser page text on failure:\n" + page_text, file=sys.stderr)
            except PlaywrightError:
                pass
            raise
        finally:
            browser.close()


def main() -> int:
    source_url = os.environ.get("DECAGENT_TEST_DATABASE_URL")
    if not source_url:
        raise SystemExit("Set DECAGENT_TEST_DATABASE_URL to a disposable dedicated database")

    administrator, database_name, app = build_fixture()
    database_info = conninfo_to_dict(source_url)
    database_info["dbname"] = database_name
    database = Database(make_conninfo(**database_info))
    server = uvicorn.Server(
        uvicorn.Config(app, host=HOST, port=PORT, lifespan="on", log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True, name="guest-workbench-e2e")
    thread.start()
    try:
        _wait_for_server(server, thread)
        result = _exercise_browser(f"http://{HOST}:{PORT}{WORKBENCH_PATH}", database)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=5)
        gateway_still_running = thread.is_alive()
        with administrator.connect(autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(database_name)
                )
            )
        if gateway_still_running:
            raise RuntimeError("Synthetic guest gateway failed to stop")


if __name__ == "__main__":
    raise SystemExit(main())
