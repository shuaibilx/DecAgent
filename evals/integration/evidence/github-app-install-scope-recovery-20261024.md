# GitHub App installation scope recovery

Date: 2026-10-24 (Asia/Shanghai)

## Result

The `DecAgent Private Deployment` GitHub App (App ID `5443230`, slug `decagent-private-deployment`) is configured with only `actions: write` and required `metadata: read` permissions. The App configuration is correct; the installation scope selected during the first install was not.

The installation endpoint reported `repository_selection: all`. An installation access token could see 16 repositories, including both `shuaibilx/DecAgent` and `shuaibilx/DecAgent-deploy`. This exceeded the approved single-repository scope. No workflow dispatch, Runner registration, CD enablement, or deployment was performed.

The overbroad installation was revoked through the GitHub App installation API (HTTP 204). Follow-up App-authenticated repository installation checks returned HTTP 404 for both the public `DecAgent` repository and the private `DecAgent-deploy` repository. At the time of those checks, the App was installed on neither target.

## Boundary checks

- Public `DecAgent`: `DECAGENT_PRIVATE_CD_ENABLED` is absent; the Client ID variable and private-key secret remain configured for the gated fixed-workflow dispatch path.
- Private `DecAgent-deploy`: no Actions variables or secrets are configured.
- Self-hosted Runners: 0 on the public repository and 0 on the private deployment repository.
- The local `.env` does not set `DECAGENT_PRIVATE_CD_ENABLED`.
- No new tag, workflow dispatch, persistent SOP container, or production rollout was created.

## Required next step

Reinstall the App from the GitHub App installation page using **Only select repositories**, selecting only `shuaibilx/DecAgent-deploy`. Before registering a Runner, verify the resulting installation lists exactly that repository and the public repository returns HTTP 404. Keep the public CD variable unset until the later deployment approval and end-to-end rollback rehearsal.

The GitHub REST API's per-repository removal endpoint requires an installation already in `selected` mode, so it cannot correct an installation created with `all` selection. See the [official installation API documentation](https://docs.github.com/en/rest/apps/installations).

## Browser follow-up (2026-10-24)

The Edge tab inventory showed the user-opened page at `https://github.com/settings/installations` with title `Installed GitHub Apps`. Binding the tab failed with `Debugger unattached`; no UI action or installation submission was performed. This page is the installed-app list, and the app-specific repository-selection form was not reached. A read-only `gh api user/installations` check was unavailable because the current CLI credential is not authorized for that endpoint (HTTP 403); this does not change the earlier App-authenticated 404 checks.

The public and private repository Runner APIs were rechecked: both returned `total_count=0`. Public Actions variables list only `DECAGENT_DEPLOY_APP_CLIENT_ID`; the private repository has no Actions variables, and querying public variable `DECAGENT_PRIVATE_CD_ENABLED` returned HTTP 404. On the Windows host, `DecAgentLocalDeployRunner` remains `Disabled`, `DecAgentPrivateDeployRunner` is absent, and no `Runner.Listener.exe` or `Runner.Worker.exe` process is active. No runner, workflow dispatch, or SOP service was started. Because the browser tab could not be read beyond its URL/title, the App's current installation state after the user opened the page still requires a fresh App-authenticated check.

## App-install page recheck (2026-10-24)

The user then opened `https://github.com/apps/decagent-private-deployment/installations/new`. Edge tab inventory reports the page title `Select Account`, so the browser is at the App-install account-selection flow rather than the installed-app list. The browser control connection did not expose page content, and no account was selected or installation submitted. The repository installation state therefore remains unverified; the previous App-authenticated 404 checks are historical evidence only. Public and private Runner counts remain zero from the latest successful repository API checks, and CD remains disabled.

## Browser recheck after user confirmed the page was open (2026-10-24)

The fresh browser inventory still shows the App-install URL with title `Select Account`. Rebinding by the visible URL and tab ID did not attach browser control; no selection, permission change, or installation submission occurred. The next step is for the user to select `shuaibilx` and restrict installation to only `DecAgent-deploy`; then verify the installation through the App-authenticated repository endpoint before registering any Runner. The user-authentication/account-selection dialog was left to the user.

## Recheck after the user reopened GitHub (2026-10-24 01:46 Asia/Shanghai)

The user said the GitHub page was open. A fresh `cua.getState()` inventory returned the Edge browser connection but an empty `tabs` list; the control session therefore cannot verify the current URL or page contents, and this does not establish that the page is closed. A second `gh api user/installations` attempt returned HTTP 403 because the active `gh` credential is a regular user token, not an App-authorized token. No installation, permission, Runner, workflow, or deployment action was made.

Read-only checks still show 0 self-hosted Runners on both `shuaibilx/DecAgent` and `shuaibilx/DecAgent-deploy`; the public repository exposes only `DECAGENT_DEPLOY_APP_CLIENT_ID` in its Actions variables, and the private repository has no Actions secrets. The local `DecAgentLocalDeployRunner` task is disabled; `DecAgentPrivateDeployRunner` and Runner processes are absent. `origin/main` remains tag-only for release and contains no `dispatch_private_deploy` job. The working branch's dispatch implementation remains gated by the unset public `DECAGENT_PRIVATE_CD_ENABLED` variable and targets only the fixed private workflow; it has no self-hosted runner job.

The remaining external step is still the GitHub App installation scope selection: choose `shuaibilx` and **Only select repositories**, select only `DecAgent-deploy`, then report completion so an App-authenticated installation check can verify that the public repository is excluded before Runner registration.

## Latest verified status (2026-10-24)

The user completed the GitHub App installation in the authenticated Edge session. The installation page showed **Only select repositories** with exactly one selected repository, `shuaibilx/DecAgent-deploy`; the GitHub success page confirmed installation. The public `shuaibilx/DecAgent` repository was not selected. The App retains only required `metadata: read` and `actions: write` permissions.

The private deployment repository now reports one online self-hosted Runner, `decagent-local-deploy`, with labels `self-hosted`, `Windows`, `X64`, and `decagent-deploy`. The public repository reports zero runners. The local scheduled task `DecAgentPrivateDeployRunner` is running and the old public task `DecAgentLocalDeployRunner` is disabled. The public repo contains the App client-ID variable and encrypted private-key secret; the public CD enablement variable remains absent. The private deployment workflow is present. No workflow dispatch, release tag, service rollout, or deployment was started; CD remains gated off.

The earlier sections above are historical snapshots and remain in place to document the overbroad installation incident and its recovery. They do not describe the latest installation state.
