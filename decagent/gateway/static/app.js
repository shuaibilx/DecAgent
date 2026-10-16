"use strict";
const $ = id => document.getElementById(id);
const state = {csrf: sessionStorage.getItem("decagent.csrf") || "", projects: [], task: null,
  stream: null, streamTask: null, cursors: new Map(), eventHistories: new Map(), rendered: "", refreshTimer: null,
  selectionRevision: 0};
const terminal = new Set(["succeeded", "failed", "cancelled"]);
const labels = {queued: "排队中", running: "执行中", awaiting_input: "等待补充信息",
  awaiting_approval: "等待审批", paused_budget: "预算暂停", recovering: "等待核实执行结果",
  succeeded: "已完成", failed: "失败", cancelled: "已取消"};
function element(tag, text, className) { const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text; if (className) node.className = className; return node; }
function notice(text) { $("notice").textContent = text; $("notice").hidden = !text; }
function showLogin() { $("login-panel").hidden = false; $("workbench").hidden = true;
  $("logout").hidden = true; if (state.stream) state.stream.close(); }
async function api(path, {method = "GET", body, key} = {}) {
  const headers = {"Content-Type": "application/json"};
  if (method !== "GET") { headers["X-CSRF-Token"] = state.csrf;
    headers["Idempotency-Key"] = key || crypto.randomUUID(); }
  const response = await fetch("/api/v1" + path, {method, headers, credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) { if (response.status === 401) showLogin();
    throw new Error(`${result.code}: ${result.message}`); } return result;
}
function bind(id, event, action) { $(id).addEventListener(event, async e => {
  if (event === "submit") e.preventDefault();
  try { notice(""); await action(e); } catch (error) { notice(error.message); }
}); }
async function enter() { await loadProjects(); await loadTasks();
  $("config").textContent = JSON.stringify(await api("/config"), null, 2);
  $("login-panel").hidden = true; $("workbench").hidden = false; $("logout").hidden = false; }
async function loadProjects() { const selected = $("project-select").value;
  state.projects = await api("/projects"); $("project-select").replaceChildren();
  for (const project of state.projects) { const option = element("option", project.repository);
    option.value = project.id; $("project-select").append(option); }
  if (state.projects.some(p => p.id === selected)) $("project-select").value = selected;
}
async function loadTasks() { const tasks = (await api("/tasks"))
  .filter(task => task.kind !== "business"); $("task-list").replaceChildren();
  if (!tasks.length) $("task-list").append(element("p", "还没有任务。", "muted"));
  for (const task of tasks) { const button = element("button",
    `${labels[task.state] || task.state} · ${task.phase}\n${task.id.slice(0, 8)} · ${(task.requested_issue || "").slice(0, 90)}`,
    state.task?.id === task.id ? "active" : "");
    button.addEventListener("click", () => selectTask(task.id).catch(e => notice(e.message)));
    $("task-list").append(button); }
}
function budget(prefix = "") { if (prefix) return {max_calls: Number($("resume-calls").value),
  max_tokens: Number($("resume-tokens").value), max_cost: $("resume-cost").value.trim() || null,
  allow_unknown_price: state.task.budget.allow_unknown_price === true,
  max_active_seconds: state.task.budget.max_active_seconds || 1800};
  return {max_calls: Number($("max-calls").value), max_tokens: Number($("max-tokens").value),
    max_cost: $("max-cost").value.trim() || null, allow_unknown_price: $("unknown-price").checked}; }
async function selectTask(id) { const revision = ++state.selectionRevision;
  const task = await api("/tasks/" + id); if (revision !== state.selectionRevision) return; state.task = task;
  renderTask(); connectEvents(id); await loadTasks();
  const [diff, verification] = await Promise.all([api(`/tasks/${id}/diff`), api(`/tasks/${id}/verification`)]);
  if (revision !== state.selectionRevision) return;
  $("diff").textContent = JSON.stringify(diff, null, 2);
  $("verification").textContent = JSON.stringify(verification, null, 2);
}
function link(url, text) { try { const parsed = new URL(url);
  if (parsed.protocol !== "https:" || parsed.hostname !== "github.com") return element("span", text);
  const node = element("a", text); node.href = parsed.href; node.target = "_blank";
  node.rel = "noopener noreferrer"; return node;
} catch (_) { return element("span", text); } }
function renderTask() { const task = state.task; if (!task) return;
  const fingerprint = JSON.stringify(task); if (state.rendered === fingerprint) return;
  state.rendered = fingerprint; $("task-title").textContent = `任务 ${task.id.slice(0, 8)}`;
  $("task-state").textContent = `${labels[task.state] || task.state} · ${task.phase}`;
  $("task-issue").textContent = task.requested_issue || "";
  const waiting = ["awaiting_input", "awaiting_approval", "paused_budget", "recovering"].includes(task.state);
  $("waiting").hidden = !waiting;
  const reasons = {awaiting_input: "请回答下方具体问题，并在回复后恢复任务。",
    awaiting_approval: "请检查动作参数、当前版本和风险，处理下方审批后恢复任务。",
    paused_budget: "任务已停止调用。请调整预算上限后恢复。",
    recovering: "先核实执行器回执；结果未确认时不会重新执行。"};
  $("waiting").textContent = waiting ? reasons[task.state] +
    (task.waiting ? "\n" + JSON.stringify(task.waiting.payload, null, 2) : "") : "";
  $("cancel-task").hidden = terminal.has(task.state);
  $("resume-task").hidden = !waiting || task.state === "paused_budget";
  $("budget-form").hidden = task.state !== "paused_budget";
  $("resume-calls").value = task.budget.max_calls || 30;
  $("resume-tokens").value = task.budget.max_tokens || 100000;
  $("resume-cost").value = task.budget.max_cost || "";
  $("decisions").textContent = JSON.stringify(task.decisions, null, 2);
  renderInterventions(task.interventions || []);
  $("delivery").replaceChildren(); if (task.delivery) {
    $("delivery").append(element("p", `交付状态：${task.delivery.status}`));
    if (task.delivery.pr_url) $("delivery").append(link(task.delivery.pr_url, "查看草稿 PR / 合并状态")); }
  $("approvals").replaceChildren(); const pending = task.approvals.filter(a => a.status === "pending");
  if (!pending.length) $("approvals").append(element("p", "当前没有待处理审批。", "muted"));
  for (const approval of pending) renderApproval(approval);
  const replied = task.approvals.filter(a => a.status !== "pending");
  if (replied.length) { const history = element("details"); history.append(element("summary", "审批历史与编辑后的判断"));
    for (const approval of replied) { const saved = approval.reply_payload?.policy_record;
      history.append(element("h3", `${approval.kind} · ${saved?.status || approval.status}`),
        element("pre", JSON.stringify({approval_id: approval.id, digest: approval.digest,
          decision: saved?.edited_decision, edited_action: saved?.edited_action,
          confirmation: saved?.confirmation}, null, 2))); }
    $("approvals").append(history); }
}
function renderInterventions(events) {
  const target = $("interventions"); target.replaceChildren();
  if (!events.length) { target.append(element("p", "尚无进展记录；历史任务可能早于介入闭环。", "muted")); return; }
  const names = {continue: "继续探索", replan: "调整策略", clarify: "询问需求", escalate: "请求帮助"};
  const checks = events.filter(e => e.kind !== "progress.observed");
  const observations = events.filter(e => e.kind === "progress.observed");
  if (observations.length) target.append(element("p",
    `最近观察：${observations.at(-1).payload.number} 步 · ${observations.at(-1).payload.tool}`));
  for (const event of checks) {
    const data = event.payload; const section = element("section", undefined, "approval");
    const title = {"intervention.checked": names[data.route] || data.route,
      "intervention.replan": `策略调整 · 第 ${data.round} 次`,
      "intervention.requested": "需要你的帮助", "intervention.feedback": "已收到你的补充"}[event.kind];
    section.append(element("h3", `${event.seq} · ${title || event.kind}`));
    if (event.kind === "intervention.checked") section.append(element("p",
      `实际处理：${names[data.route] || data.route}；模型建议：${names[data.model_suggestion] || "未提供"}；`
      + (data.calibrated ? "匹配校准策略" : "使用有界规则；语义建议供观察")
      + `。依据：${data.basis}`));
    if (event.kind === "intervention.requested") section.append(element("p", data.question));
    if (event.kind === "intervention.feedback") section.append(element("p", data.answer));
    const details = element("details"); details.append(element("summary", "查看记录与证据"),
      element("pre", JSON.stringify(data, null, 2))); section.append(details); target.append(section);
  }
}
function renderApproval(approval) { const section = element("section", undefined, "approval");
  const help = approval.reply_payload?.subject?.type === "adaptive_intervention";
  section.append(element("h3", help ? "补充定位线索或预期行为" : `审批类型：${approval.kind}`));
  section.append(element("p", `摘要 ${approval.digest}`, "muted"));
  const evidence = element("pre", JSON.stringify({action: approval.action,
    subject: approval.reply_payload?.subject, code_trust_request: approval.reply_payload?.code_trust_request,
    version: approval.version, decisions: state.task.decisions}, null, 2));
  if (help) { section.append(element("p", approval.reply_payload?.question || "请根据尝试记录补充具体线索。"));
    const details = element("details"); details.append(element("summary", "查看尝试记录和确认材料"), evidence);
    section.append(details);
  } else section.append(evidence);
  const buttons = element("div", undefined, "row");
  const send = async (decision, extra = {}) => { try {
    const result = await api(`/approvals/${approval.id}/reply`, {method: "POST", body: {
      expected_digest: approval.digest, decision, ...extra}});
    notice(result.decision?.hard_denial ? `修改被硬规则拒绝：${result.decision.reason}` :
      "回复已保存。恢复任务时会重新校验当前授权和版本。"); await selectTask(state.task.id);
  } catch (e) { notice(e.message); } };
  if (["action", "code_trust"].includes(approval.kind)) {
    const approve = element("button", "批准当前提议"); approve.addEventListener("click", () => send("approve"));
    buttons.append(approve); }
  const reject = element("button", "拒绝", "danger"); reject.addEventListener("click", () => send("reject"));
  buttons.append(reject); section.append(buttons);
  if (approval.kind === "action") { const details = element("details"); details.append(element("summary", "编辑动作参数后重新治理"));
    const textarea = element("textarea"); textarea.rows = 5; textarea.setAttribute("aria-label", "替换动作参数 JSON");
    textarea.value = JSON.stringify(approval.action?.params || {}, null, 2);
    const edit = element("button", "提交修改"); edit.addEventListener("click", () => {
      try { send("edit", {edited_params: JSON.parse(textarea.value)}); } catch (e) { notice("参数必须为有效 JSON。"); }
    }); details.append(textarea, edit); section.append(details);
  } else if (!["action", "code_trust"].includes(approval.kind)) {
    const textarea = element("textarea"); textarea.rows = 3; textarea.setAttribute("aria-label", "确认或澄清内容");
    const confirm = element("button", approval.kind === "clarification" ? "提交补充信息" : "确认这些具体条件");
    confirm.addEventListener("click", () => { if (!textarea.value.trim()) return notice("请填写具体确认内容。");
      send("confirm", {confirmation: textarea.value}); }); section.append(textarea, confirm); }
  $("approvals").append(section);
}
function connectEvents(id) { if (state.streamTask === id && state.stream) return;
  if (state.stream) state.stream.close(); state.streamTask = id; $("events").replaceChildren();
  if (!state.eventHistories.has(id)) state.eventHistories.set(id, []);
  const history = state.eventHistories.get(id);
  const displayEvent = saved => { if (state.streamTask === id && state.task?.id === id)
    $("events").append(element("li", `${saved.seq} · ${saved.kind}\n${JSON.stringify(saved.payload)}`)); };
  for (const saved of history) displayEvent(saved);
  const after = state.cursors.get(id) || 0;
  state.stream = new EventSource(`/api/v1/tasks/${id}/events?after=${after}`);
  state.stream.onopen = () => { $("connection").textContent = "事件连接：已连接（任务状态独立保存）"; };
  state.stream.onerror = () => { $("connection").textContent = "事件连接：重连中；任务仍按持久化状态运行"; };
  state.stream.addEventListener("task_event", event => { const seq = Number(event.lastEventId);
    if (seq <= (state.cursors.get(id) || 0)) return; state.cursors.set(id, seq);
    const saved = JSON.parse(event.data); history.push(saved); displayEvent(saved);
    if (!state.refreshTimer) state.refreshTimer = setTimeout(async () => { state.refreshTimer = null;
      if (state.task?.id === id) { try { state.task = await api("/tasks/" + id); renderTask(); }
        catch (e) { notice(e.message); } } }, 200);
  });
}
async function loadMemories() { const project = $("project-select").value; if (!project) return;
  const rows = await api(`/projects/${project}/memories`); $("memories").replaceChildren();
  if (!rows.length) $("memories").append(element("p", "当前项目还没有记忆。", "muted"));
  for (const memory of rows) { const section = element("section", undefined, "memory");
    section.append(element("h3", `${memory.status} · ${memory.category} · 修订 ${memory.revision}`),
      element("p", memory.content), element("pre", JSON.stringify({applicability: memory.applicability,
        evidence_hash: memory.evidence_hash, source_task_id: memory.source_task_id, versions: memory.versions}, null, 2)));
    if (memory.source_pr) section.append(link(memory.source_pr, "来源 PR"));
    const text = element("textarea"); text.rows = 2; text.setAttribute("aria-label", "记忆审核确认内容");
    text.placeholder = "写明对这条记忆的适用性、来源与冲突的确认"; section.append(text);
    const buttons = element("div", undefined, "row");
    for (const [operation, label] of [["activate", "审核并申请激活"], ["reject", "审核并拒绝"], ["revoke", "撤销记忆"]]) {
      const button = element("button", label, operation === "revoke" ? "danger" : "quiet");
      button.addEventListener("click", async () => { try {
        if (operation !== "revoke" && !text.value.trim()) return notice("请填写这条记忆的具体审核确认。");
        const body = {expected_revision: memory.revision}; if (operation !== "revoke") {
          body.operation = operation; body.confirmation = text.value; }
        await api(`/memories/${memory.id}/${operation === "revoke" ? "revoke" : "review"}`, {method: "POST", body});
        await loadMemories(); notice("记忆状态已保存。");
      } catch (e) { notice(e.message); } }); buttons.append(button);
    } section.append(buttons); $("memories").append(section); }
}
bind("login-form", "submit", async () => { const token = $("owner-token").value;
  const result = await api("/auth/session", {method: "POST", body: {token}});
  $("owner-token").value = ""; state.csrf = result.csrf_token;
  sessionStorage.setItem("decagent.csrf", state.csrf); await enter(); });
bind("logout", "click", async () => { await api("/auth/session", {method: "DELETE"});
  sessionStorage.removeItem("decagent.csrf"); state.csrf = ""; showLogin(); });
bind("project-form", "submit", async () => { const created = await api("/projects", {method: "POST", body: {
  repository: $("repository").value.trim(), base_branch: $("base-branch").value.trim()}});
  await loadProjects(); $("project-select").value = created.id; notice("项目已登记；权限诊断状态见返回记录。"); });
bind("task-form", "submit", async () => { const project = $("project-select").value;
  if (!project) throw new Error("请先登记并选择项目。");
  const task = await api("/tasks", {method: "POST", body: {project_id: project,
    base_ref: $("base-ref").value.trim(), issue: $("issue").value,
    trust_scope: $("request-trust").checked ? {kind: "exact_commit"} : null,
    budget: budget(), publish_draft_pr: $("publish-pr").checked}});
  await selectTask(task.id); notice("任务已持久化并排队。"); });
bind("refresh", "click", async () => { await loadTasks(); if (state.task) await selectTask(state.task.id); });
bind("cancel-task", "click", async () => { await api(`/tasks/${state.task.id}/cancel`, {method: "POST", body: {}});
  await selectTask(state.task.id); });
bind("resume-task", "click", async () => { await api(`/tasks/${state.task.id}/resume`, {method: "POST", body: {}});
  await selectTask(state.task.id); });
bind("budget-form", "submit", async () => { await api(`/tasks/${state.task.id}/resume`, {method: "POST", body: {budget: budget("resume")}});
  await selectTask(state.task.id); });
bind("load-memories", "click", loadMemories);
bind("local-diagnostic", "click", async () => { $("diagnostic-result").textContent = JSON.stringify(
  await api("/diagnostics", {method: "POST", body: {mode: "local"}}), null, 2); });
bind("live-diagnostic", "click", async () => { if (!$("live-confirm").checked) throw new Error("请明确确认这一次外部请求。");
  $("diagnostic-result").textContent = JSON.stringify(await api("/diagnostics", {method: "POST", body: {
    mode: "live", target: $("live-target").value, confirm_external_request: true}}), null, 2);
  $("live-confirm").checked = false; });
for (const button of document.querySelectorAll("[data-view]")) button.addEventListener("click", async () => {
  for (const section of document.querySelectorAll(".view")) section.hidden = section.id !== button.dataset.view;
  for (const tab of document.querySelectorAll("[data-view]")) tab.classList.toggle("selected", tab === button);
  if (button.dataset.view === "memory-view") try { await loadMemories(); } catch (e) { notice(e.message); }
});
if (state.csrf) enter().catch(e => { showLogin(); notice(e.message); });
