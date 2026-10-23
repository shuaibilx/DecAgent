# Guest workbench browser E2E

- Date: 2026-10-24 (Asia/Shanghai)
- Browser: Playwright Chromium from the repository's locked E2E environment
- Database: disposable PostgreSQL 17 service using the same pinned image as CI
- Model/provider calls: 0
- Business tool/MCP calls: 0

## Covered flow

The test launches the production Gateway and real `business.html`/`business.js` in Chromium against a fresh database created by `business_guest_browser_fixture`:

1. With guest trial enabled, the browser shows the guest entry while the workbench remains hidden.
2. Starting the trial creates a guest workspace and authenticates its initial `employee-it` identity.
3. Switching to `supervisor` changes the employee identity but retains the same guest workspace.
4. Reloading the same browser context restores the supervisor session and selected role.
5. Clearing the guest workspace deactivates the workspace, removes all active guest sessions, hides the workbench, and makes session restore return HTTP 401.

The fixture does not start a task worker, call Jev, submit business writes, or use an external service. The test asserts there are no browser JavaScript errors. Its report was:

```json
{"active_sessions_after_clear":0,"browser":"chromium","flow":"guest start -> role switch -> same-browser restore -> clear","model_requests":0,"page_errors":[],"session_restore_after_clear":401,"status":"passed","workspace_deactivated":true}
```

The command is now part of the CI browser-E2E job:

```powershell
python -m evals.e2e.business_guest_workbench
```

This evidence closes only the browser-level guest session lifecycle. The Jev suggestion-to-semantic-reply path is still covered by the Node DOM regression rather than a real-browser flow, and this test makes no claim about live Jev quality or behavior. OpenSpec task 2.5 remains open.
