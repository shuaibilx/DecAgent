"use strict";

const $ = id => document.getElementById(id);
const state = {
  csrf: sessionStorage.getItem("decagent.employee.csrf") || "",
  sessionKind: sessionStorage.getItem("decagent.session.kind") || "",
  guestRoles: [], guestRoleKey: "",
  identity: null, plugins: [], task: null, operation: null,
  planSnapshot: null, trace: null, memories: [], memoryDependencies: null,
  selection: 0, operationSelection: 0, stream: null, cursor: new Map(),
  credentialRef: null, refreshTimer: null,
};
const terminal = new Set(["succeeded", "failed", "cancelled"]);
const taskLabels = {queued: "排队中", running: "执行中", awaiting_input: "等待补充",
  awaiting_approval: "等待确认", paused_budget: "预算暂停", recovering: "等待核实",
  succeeded: "目标已完成", failed: "未完成", cancelled: "已取消"};
const operationLabels = {ready: "待提交", awaiting_approval: "待本人确认", running: "执行中",
  unknown: "结果待核实", succeeded: "已核实成功", failed: "失败", rejected: "已拒绝",
  expired: "确认失效", cancelled: "已取消"};
const phaseLabels = {prepare: "查证事实", decide: "判断分支", plan: "规划步骤",
  validate: "校验动作", human: "人工决定", execute: "执行操作",
  verify: "核实回执", finalize: "核实目标"};

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function notice(message) { $("notice").textContent = message; $("notice").hidden = !message; }
function details(label, value) {
  const wrapper = el("details");
  wrapper.append(el("summary", label), el("pre", JSON.stringify(value, null, 2)));
  return wrapper;
}
function resetWorkspace(clearHash = true) {
  clearTimeout(state.refreshTimer);
  state.selection++; state.operationSelection++;
  state.stream?.close(); state.stream = null;
  state.cursor.clear(); state.task = null; state.operation = null;
  state.identity = null; state.plugins = []; state.credentialRef = null;
  $("task-list").replaceChildren(); $("ticket-list").replaceChildren();
  $("ticket-detail").replaceChildren(); $("operation-list").replaceChildren();
  $("operation-detail").hidden = true; $("credential-info").replaceChildren();
  $("plugin-select").replaceChildren(); $("capabilities").replaceChildren();
  $("plugin-description").textContent = ""; $("submit-task").disabled = true;
  for (const id of ["task-instruction", "phases", "waiting", "approvals", "result",
    "actions", "decisions", "events", "operation-body", "operation-actions",
    "plan-nodes", "plan-policies", "plan-usage", "trace-timeline", "memory-list",
    "memory-dependencies"]) $(id).replaceChildren();
  state.planSnapshot = null; state.trace = null; state.memories = []; state.memoryDependencies = null;
  $("plan-summary").replaceChildren(el("p", "选择任务后显示服务端保存的计划、分支路径和参数来源。", "muted"));
  $("trace-download").hidden = true; $("trace-download").removeAttribute("href");
  $("task-title").textContent = "选择一个任务"; $("task-state").textContent = "—";
  $("connection").textContent = "进度尚未连接";
  $("cancel-task").hidden = true; $("resume-task").hidden = true;
  $("budget-form").hidden = true; $("waiting").hidden = true;
  $("revoke-credential").hidden = true; $("current-identity").hidden = true;
  $("current-identity").textContent = "";
  $("guest-role-control").hidden = true; $("guest-role").replaceChildren();
  $("clear-guest").hidden = true;
  $("workbench").hidden = true; $("logout").hidden = true; $("login-panel").hidden = false;
  if (clearHash && location.hash.startsWith("#operation=")) {
    history.replaceState(null, "", location.pathname);
  }
}
async function api(path, {method = "GET", body, key} = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") {
    headers["X-CSRF-Token"] = state.csrf;
    headers["Idempotency-Key"] = key || crypto.randomUUID();
  }
  const response = await fetch("/api/v2/business" + path, {
    method, headers, credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401) {
      sessionStorage.removeItem("decagent.employee.csrf");
      sessionStorage.removeItem("decagent.session.kind");
      state.csrf = ""; resetWorkspace();
      state.sessionKind = ""; state.guestRoleKey = ""; state.guestRoles = [];
    }
    throw new Error(`${result.code || "request_failed"}: ${result.message || "请求失败"}`);
  }
  return result;
}
function bind(id, event, callback) {
  $(id).addEventListener(event, async e => {
    if (event === "submit") e.preventDefault();
    try { notice(""); await callback(e); } catch (error) { notice(error.message); }
  });
}
function describePlugin() {
  const plugin = state.plugins.find(item => item.scope_id === $("plugin-select").value);
  $("plugin-description").textContent = plugin
    ? `${plugin.plugin_id} · ${plugin.version} · ${plugin.commands.length} 项当前角色可用能力`
    : "尚无可用业务插件，请由实例维护者登记。";
  $("submit-task").disabled = !plugin;
}
async function loadPlugins() {
  state.plugins = await api("/plugins");
  $("plugin-select").replaceChildren(); $("capabilities").replaceChildren();
  for (const plugin of state.plugins) {
    const option = el("option", `${plugin.plugin_id} · ${plugin.version}`);
    option.value = plugin.scope_id; $("plugin-select").append(option);
    const section = el("section"); section.append(el("h3", plugin.plugin_id));
    const list = el("ul");
    for (const command of plugin.commands) {
      list.append(el("li", `${command.name} · ${command.effect === "read" ? "查询" : "写入"} · ${command.description}`));
    }
    section.append(list); $("capabilities").append(section);
  }
  describePlugin();
}
async function loadTasks() {
  const tasks = await api("/tasks");
  $("task-list").replaceChildren();
  if (!tasks.length) $("task-list").append(el("p", "当前员工没有任务。", "muted"));
  for (const task of tasks) {
    const button = el("button", `${taskLabels[task.state] || task.state} · ${task.id.slice(0, 8)}\n${(task.requested_issue || "").slice(0, 80)}`,
      state.task?.id === task.id ? "active" : "");
    button.type = "button";
    button.addEventListener("click", () => selectTask(task.id).catch(error => notice(error.message)));
    $("task-list").append(button);
  }
}
async function selectTask(id) {
  const sequence = ++state.selection;
  state.stream?.close(); state.stream = null;
  const task = await api(`/tasks/${id}`);
  if (sequence !== state.selection) return;
  state.task = task; renderTask(task); await loadTasks(); connectEvents(id, sequence);
  const [plan, trace, memories] = await Promise.allSettled([
    api(`/tasks/${id}/plan`), api(`/tasks/${id}/trace`),
    api(`/memories?task_id=${encodeURIComponent(id)}`),
  ]);
  if (sequence !== state.selection || state.task?.id !== id) return;
  state.planSnapshot = plan.status === "fulfilled" ? plan.value : null;
  state.trace = trace.status === "fulfilled" ? trace.value : null;
  state.memories = memories.status === "fulfilled" ? memories.value : [];
  const hasMemorySlots = (state.planSnapshot?.plan?.nodes || []).some(node =>
    Object.values(node.inputs || {}).some(binding => binding?.source === "verified_memory"));
  const dependencies = hasMemorySlots
    ? await Promise.allSettled([api("/memories/dependencies?task_id=" + encodeURIComponent(id))])
      .then(items => items[0])
    : {status: "fulfilled", value: {valid: true, not_declared: true}};
  if (sequence !== state.selection || state.task?.id !== id) return;
  state.memoryDependencies = dependencies.status === "fulfilled" ? dependencies.value : null;
  renderPlan(state.planSnapshot, state.trace, plan.status === "rejected" ? plan.reason : null,
    trace.status === "rejected" ? trace.reason : null);
  renderMemories(state.memories, state.memoryDependencies,
    memories.status === "rejected" ? memories.reason : null,
    dependencies.status === "rejected" ? dependencies.reason : null);
}
function renderTask(task) {
  $("task-title").textContent = `任务 ${task.id.slice(0, 8)}`;
  $("task-state").textContent = `${taskLabels[task.state] || task.state} · ${phaseLabels[task.phase] || task.phase}`;
  $("task-instruction").textContent = task.requested_issue || "";
  $("phases").replaceChildren();
  for (const [phase, text] of Object.entries(phaseLabels)) {
    $("phases").append(el("li", text, task.phase === phase ? "current" : ""));
  }
  const waiting = !terminal.has(task.state) && !["queued", "running"].includes(task.state);
  $("waiting").hidden = !waiting;
  $("waiting").textContent = waiting
    ? (task.waiting?.payload?.question || task.waiting?.payload?.reason || taskLabels[task.state] || task.state)
    : "";
  $("cancel-task").hidden = terminal.has(task.state);
  $("resume-task").hidden = task.state !== "recovering";
  $("budget-form").hidden = task.state !== "paused_budget";
  $("resume-calls").value = task.budget.max_calls || 30;
  $("resume-tokens").value = task.budget.max_tokens || 100000;
  $("resume-active").value = task.budget.max_active_seconds || 1800;
  $("resume-cost").value = task.budget.max_cost || "";
  $("approvals").replaceChildren();
  const pending = (task.approvals || []).filter(item => item.status === "pending");
  if (!pending.length) $("approvals").append(el("p", "当前没有待处理的问题。", "muted"));
  for (const approval of pending) renderTaskApproval(task, approval);
  $("result").replaceChildren();
  if (task.business_result) $("result").append(details("目标核实", task.business_result));
  $("actions").replaceChildren();
  for (const action of task.actions || []) {
    const card = el("section", undefined, "receipt");
    card.append(el("h3", `${action.snapshot?.command || "操作"} · ${action.status}`),
      details("参数与回执", {action_id: action.id, snapshot: action.snapshot, receipt: action.receipt}));
    $("actions").append(card);
  }
  $("decisions").replaceChildren();
  if (!task.decisions?.length) $("decisions").append(el("p", "尚无模型判断记录。", "muted"));
  for (const record of task.decisions || []) {
    const card = el("section", undefined, "route");
    card.append(el("h3", `${record.question_version === "primary-plan-1" ? "主模型规划" : "类型化判断"} · ${record.final_route}`),
      details("实际问题与答案", record.result));
    $("decisions").append(card);
  }
}
const nodeStatusLabels = {succeeded: "已完成", skipped: "已跳过", waiting: "等待本人",
  ready: "待执行", running: "执行中", failed: "失败", unknown: "结果待核实"};
const nodeTypeLabels = {read_tool: "MCP 查询", governed_write: "受控写入", transform: "确定性转换",
  decide: "语义判断", branch: "条件分支", ask_user: "向员工澄清", complete: "完成核验"};
function bindingDescription(binding) {
  if (!binding || typeof binding !== "object") return "未声明";
  const source = binding.source || "未知来源";
  if (source === "verified_memory") return "员工记忆槽 · " + (binding.memory_kind || "?") + " / " + (binding.subject || "?");
  if (source === "node_output") return "节点输出 · " + (binding.node_id || "?") + (binding.path ? " · " + binding.path : "");
  if (source === "accepted_decision") return "已接受的员工判断 · " + (binding.node_id || "?");
  if (source === "task_input") return "任务输入" + (binding.path ? " · " + binding.path : "");
  if (source === "literal") return "固定值 · " + JSON.stringify(binding.value);
  return source;
}
function renderPlan(snapshot, trace, planError, traceError) {
  const summary = $("plan-summary"); summary.replaceChildren();
  $("plan-nodes").replaceChildren(); $("plan-policies").replaceChildren();
  $("plan-usage").replaceChildren(); $("trace-timeline").replaceChildren();
  if (!snapshot) {
    summary.append(el("p", "计划暂不可用：" + (planError?.message || "请求失败"), "failed"));
  } else if (snapshot.legacy || !snapshot.plan) {
    summary.append(el("p", snapshot.legacy ? "这是兼容任务，当前没有 business-3 计划投影。" : "计划尚未创建。", "muted"));
  } else {
    const plan = snapshot.plan;
    summary.append(el("p", "版本 " + plan.revision + " · " + plan.nodes.length + " 个节点 · 目标：" +
      (plan.goal?.description || plan.goal?.id || "已登记目标")));
    summary.append(el("p", "计划摘要：" + (plan.content_digest || "不可用"), "muted"));
    renderThresholdExplanation(snapshot, summary);
    for (const [key, label] of [["completed_nodes", "已完成"], ["active_nodes", "已激活"],
      ["pending_nodes", "待执行"], ["inactive_nodes", "未走分支"], ["blocked_nodes", "被阻塞"]]) {
      const values = snapshot[key] || [];
      summary.append(el("p", label + "：" + (values.length ? values.join("、") : "无"),
        values.length ? "route-state" : "muted"));
    }
    const facts = new Map((snapshot.nodes || []).map(item => [item.node_id, item]));
    for (const definition of plan.nodes || []) {
      const fact = facts.get(definition.id);
      const status = fact?.status ? (nodeStatusLabels[fact.status] || fact.status) :
        (snapshot.inactive_nodes || []).includes(definition.id) ? "未走分支" : "尚无执行记录";
      const card = el("article", undefined, "plan-node" + (fact?.activated ? " active" : "") +
        (status === "未走分支" ? " inactive" : ""));
      card.append(el("h3", definition.id + " · " + (nodeTypeLabels[definition.type] || definition.type) + " · " + status));
      const command = definition.command || definition.tool?.command;
      if (command) card.append(el("p", "MCP 工具：" + command));
      if (fact?.branch_case) card.append(el("p", "实际命中分支：" + fact.branch_case, "approved"));
      if (fact?.control?.targets?.length) card.append(el("p", "实际进入：" + fact.control.targets.join("、"), "approved"));
      for (const [name, binding] of Object.entries(definition.inputs || {})) {
        card.append(el("p", "参数 " + name + " ← " + bindingDescription(binding), "binding-source"));
      }
      if (definition.type === "branch" && Array.isArray(definition.cases)) {
        const cases = definition.cases.map(item => (item.id || item.name || "分支") +
          " → " + ((item.targets || []).join("、") || "结束"));
        if (cases.length) card.append(details("声明的分支", cases));
      }
      if (!fact && definition.type === "branch" && Array.isArray(definition.cases)) {
        card.append(el("p", "尚未收到运行时结果，分支没有被预先假定。", "muted"));
      }
      $("plan-nodes").append(card);
    }
    if (snapshot.policies?.length) renderPolicyControls(snapshot);
    else $("plan-policies").append(el("p", "当前任务没有已绑定的校准策略；语义判断需要按系统人工接管规则处理。", "muted"));
  }
  const usage = snapshot?.usage || trace?.usage;
  if (usage) {
    for (const family of ["primary", "jev"]) {
      const item = usage.families?.[family] || {};
      const label = family === "primary" ? "主模型" : "Jev";
      const tokens = item.actual_tokens ?? (item.unknown_tokens ? "未知" : 0);
      $("plan-usage").append(el("p", label + "：请求 " + (item.actual_requests ?? 0) +
        " 次 / token " + tokens + " / 已知费用 $" + (item.known_cost ?? "0"), "muted"));
    }
    $("plan-usage").prepend(el("p", "总请求 " + (usage.actual_requests ?? 0) + " 次；预留 " +
      (usage.reservations ?? 0) + " 次；未知请求 " + (usage.unknown_requests ?? 0) + " 次。" +
      (usage.unknown_cost ? "总费用仍有未知项，按预留额度保留。" :
        "已知总费用 $" + (usage.known_cost ?? usage.actual_cost ?? "0") + "。")));
    if ((usage.requests || []).length) $("plan-usage").append(details("模型请求明细", usage.requests));
  } else $("plan-usage").append(el("p", "暂无模型用量记录。", "muted"));
  if (trace) {
    const link = $("trace-download");
    link.href = "/api/v2/business/tasks/" + encodeURIComponent(trace.task_id) + "/export";
    link.download = "business-trace-" + trace.task_id + ".json"; link.hidden = false;
    for (const item of trace.timeline || []) {
      const row = el("li", (item.seq ?? "·") + " · " + new Date(item.created_at).toLocaleString() + " · " + item.kind);
      row.append(details("关联证据", {links: item.links, details: item.details}));
      $("trace-timeline").append(row);
    }
    if (!(trace.timeline || []).length) $("trace-timeline").append(el("li", "暂无事件。", "muted"));
  } else $("trace-timeline").append(el("li", "时间线暂不可用：" + (traceError?.message || "请求失败"), "failed"));
}
function renderThresholdExplanation(snapshot, target) {
  const explanation = snapshot.threshold_explanation || {};
  const section = el("section", undefined, "certainty-explanation");
  section.append(el("h3", "理解语义决策的三种指标"));
  for (const [key, label] of [["certainty", "Jev 单次把握度"],
    ["accuracy", "验证集实测正确率"], ["confidence_interval", "统计置信区间"]]) {
    if (explanation[key]) section.append(el("p", label + "：" + explanation[key]));
  }
  if (section.children.length > 1) target.append(section);
}
function renderPolicyControls(snapshot) {
  const panel = el("section", undefined, "policy-panel");
  panel.append(el("h3", "语义判断阈值策略"));
  panel.append(el("p", "阈值仅能在管理员允许且经过验证的候选范围内调整；提高阈值通常会增加人工接管。", "muted"));
  for (const policy of snapshot.policies) {
    const card = el("article", undefined, "policy-record");
    card.append(el("h4", policy.policy_id + " · v" + policy.policy_version + " · " + policy.status));
    card.append(el("p", "管理员下限 " + policy.minimum_threshold + " · 当前生效 " +
      (policy.effective_threshold ?? "无") + " · 修订 " + (policy.override_revision ?? 0)));
    const allowed = policy.allowed_thresholds || [];
    if (!allowed.length) {
      card.append(el("p", "当前策略没有达到既定验证质量目标的阈值；只能请求人工判断。" +
        (policy.reason ? " 原因：" + policy.reason : ""), "failed"));
    } else {
      const form = el("form", undefined, "threshold-form");
      const label = el("label", "本任务自动决策阈值");
      const select = el("select"); select.setAttribute("aria-label", "本任务 " + policy.policy_id + " 阈值");
      for (const value of allowed) {
        const option = el("option", Math.round(value * 100) + "% certainty"); option.value = String(value);
        option.selected = Number(policy.effective_threshold) === value; select.append(option);
      }
      label.append(select);
      const button = el("button", "保存任务阈值"); button.type = "submit";
      form.append(label, button);
      form.addEventListener("submit", async event => {
        event.preventDefault(); if (!state.task || state.task.id !== snapshot.task_id) return;
        button.disabled = true;
        try {
          await api("/tasks/" + state.task.id + "/decision-thresholds", {method: "POST", body: {
            policy_id: policy.policy_id, policy_version: policy.policy_version,
            threshold: Number(select.value),
          }});
          notice("本任务的阈值已保存；服务端已保留策略修订与审计事件。");
          await selectTask(state.task.id);
        } catch (error) { notice(error.message); button.disabled = false; }
      });
      card.append(form);
    }
    panel.append(card);
  }
  $("plan-policies").replaceChildren(panel);
}
function renderMemories(memories, dependencies, memoryError, dependencyError) {
  const list = $("memory-list"); list.replaceChildren();
  const dependencyView = $("memory-dependencies"); dependencyView.replaceChildren();
  if (dependencies) {
    if (dependencies.not_declared) {
      dependencyView.append(el("p", "当前计划没有声明员工记忆槽，不会读取记忆来填充任务上下文；你仍可在下方查看和管理自己的记忆。", "muted"));
    } else {
      const invalid = (dependencies.invalid_dependencies || []).map(item =>
        item.memory_kind + "/" + item.subject + " (" + item.reason + ")");
      dependencyView.append(el("p", dependencies.valid ? "当前任务声明的记忆槽均已通过有效性检查。" :
        "当前任务记忆槽需要处理：" + (invalid.join("、") || "工作流或上下文不可用"),
        dependencies.valid ? "approved" : "failed"));
    }
  } else if (dependencyError) dependencyView.append(el("p", "无法校验当前任务的记忆依赖：" + dependencyError.message, "muted"));
  if (memoryError) {
    list.append(el("p", "无法读取当前任务的员工记忆：" + memoryError.message, "failed"));
    return;
  }
  if (!memories.length) {
    list.append(el("p", "当前员工在此业务范围没有记忆记录。新记忆必须先由可信流程提出，再由本人确认。", "muted"));
    return;
  }
  for (const memory of memories) {
    const card = el("article", undefined, "memory-record");
    const status = memory.expired ? "已过期" : memory.status === "pending_confirmation" ? "待本人确认" :
      memory.active && memory.verified ? "当前可用" : (memory.status || "不可用");
    card.append(el("h3", memory.kind + " · " + memory.subject + " · " + status));
    card.append(el("p", JSON.stringify(memory.data || {})),
      el("p", "创建：" + new Date(memory.created_at).toLocaleString() + " · 到期：" +
        new Date(memory.expires_at).toLocaleString(), "muted"));
    const controls = el("div", undefined, "memory-controls");
    const run = async (button, path, body, message) => {
      button.disabled = true;
      try { await api(path, {method: "POST", body}); notice(message); await selectTask(state.task.id); }
      catch (error) { notice(error.message); button.disabled = false; }
    };
    const base = {task_id: state.task.id, expected_digest: memory.content_digest};
    if (memory.status === "pending_confirmation" && !memory.expired) {
      const confirm = el("button", "确认启用这条记忆"); confirm.type = "button";
      confirm.addEventListener("click", () => run(confirm, "/memories/" + memory.id + "/confirm", base, "记忆已由本人确认。"));
      controls.append(confirm);
    }
    if (memory.active && memory.verified && !memory.expired) {
      const correction = el("input"); correction.maxLength = 2048; correction.required = true;
      correction.setAttribute("aria-label", "更正后的记忆内容"); correction.placeholder = "更正内容；提交后需要重新确认";
      const correct = el("button", "提交更正提议"); correct.type = "button";
      correct.addEventListener("click", () => {
        if (!correction.value.trim()) { notice("请填写更正内容。"); return; }
        run(correct, "/memories/" + memory.id + "/correct", {...base, value: correction.value.trim()},
          "更正提议已保存，需本人再次确认后才生效。");
      });
      controls.append(correction, correct);
      const revoke = el("button", "撤销此记忆", "danger"); revoke.type = "button";
      revoke.addEventListener("click", () => run(revoke, "/memories/" + memory.id + "/revoke",
        {...base, reason: "员工在业务工作台主动撤销"}, "记忆已撤销，后续执行将重新校验依赖。"));
      controls.append(revoke);
    }
    if (controls.childElementCount) card.append(controls);
    list.append(card);
  }
}

function renderSemanticApproval(task, approval) {
  const stored = approval.reply_payload?.semantic || approval.reply_payload?.request ||
    approval.reply_payload || {};
  const request = task.waiting?.payload?.kind === "decision" ? task.waiting.payload : stored;
  const question = request.question || {};
  const criteria = question.criteria || {};
  const card = el("section", undefined, "approval-card semantic-card");
  card.append(el("h3", "需要你判断 · " + (question.id || request.question_id || request.node_id || "业务分支")),
    el("p", question.instructions || request.evidence?.summary || "请核对当前业务信息并选择合适的分支。"),
    el("p", "Jev 建议：" + JSON.stringify(request.suggestion ?? request.raw_model_answer ?? "无") +
      " · 策略门控后等待本人确认", "muted"));
  if (request.raw_model_answer) card.append(details("Jev 原始答案与证据", request.raw_model_answer));
  const valueSelect = el("select"); valueSelect.setAttribute("aria-label", "选择业务判断结果");
  if (question.type === "score" && Array.isArray(criteria)) {
    criteria.forEach((label, index) => {
      const option = el("option", index + " · " + label); option.value = String(index); valueSelect.append(option);
    });
  } else if (question.type === "noul") {
    for (const [value, label] of [["true", criteria.true || "是"], ["false", criteria.false || "否"]]) {
      const option = el("option", label); option.value = value; valueSelect.append(option);
    }
  } else {
    for (const [value, label] of Object.entries(criteria)) {
      if (value === "no_match") continue;
      let title = label;
      try {
        const candidate = JSON.parse(label);
        if (candidate && typeof candidate === "object") {
          title = [candidate.name, candidate.team, candidate.id].filter(Boolean).join(" · ") || label;
        }
      } catch {}
      const option = el("option", title); option.value = value; valueSelect.append(option);
    }
  }
  const controls = el("div", undefined, "approval-controls");
  const send = async (decision, value, text) => {
    if (state.task?.id !== task.id) return;
    for (const button of controls.querySelectorAll("button")) button.disabled = true;
    const approvalId = request.approval_id || approval.id;
    try {
      const result = await api("/tasks/" + task.id + "/decisions/" + approvalId + "/reply", {method: "POST", body: {
        expected_digest: request.digest || approval.digest,
        expected_plan_revision: request.plan_revision || question.plan_revision || 1,
        decision, ...(value !== undefined ? {value} : {}), ...(text !== undefined ? {text} : {}),
      }});
      notice(result.status === "stale"
        ? "候选已变化或过期，本次选择未采用；任务已刷新，请检查最新业务信息后重新判断。"
        : "员工判断已保存；该判断不会批准任何业务写操作。");
      await selectTask(task.id);
    } catch (error) {
      notice(error.message);
      for (const button of controls.querySelectorAll("button")) button.disabled = false;
    }
  };
  if (valueSelect.options.length) {
    const select = el("button", "采用所选判断"); select.type = "button";
    select.addEventListener("click", () => {
      const value = question.type === "noul" ? valueSelect.value === "true" :
        question.type === "score" ? Number(valueSelect.value) : valueSelect.value;
      send("select", value);
    });
    controls.append(valueSelect, select);
  } else controls.append(el("p", "当前候选已失效或无法显示，请刷新任务并重新检查。", "failed"));
  if (question.type === "choice") {
    const noMatch = el("button", "都不符合"); noMatch.type = "button";
    noMatch.addEventListener("click", () => send("no_match")); controls.append(noMatch);
  }
  const supplement = el("textarea"); supplement.rows = 3; supplement.maxLength = 65536;
  supplement.setAttribute("aria-label", "补充业务事实"); supplement.placeholder = "补充会改变分支判断的事实";
  const addFacts = el("button", "补充事实后重判"); addFacts.type = "button";
  addFacts.addEventListener("click", () => supplement.value.trim()
    ? send("supplement", undefined, supplement.value.trim()) : notice("请填写具体业务事实。"));
  const reject = el("button", "拒绝当前判断", "danger"); reject.type = "button";
  reject.addEventListener("click", () => send("reject"));
  controls.append(supplement, addFacts, reject); card.append(controls); $("approvals").append(card);
}

function renderTaskApproval(task, approval) {
  if (approval.kind === "decision") {
    renderSemanticApproval(task, approval);
    return;
  }
  const card = el("section", undefined, "approval-card");
  const action = approval.action || {};
  const isAction = approval.kind === "action";
  const operationId = (task.actions || []).find(item => item.id === approval.action_id)
    ?.server_operation_id;
  card.append(
    el("h3", isAction ? `确认具体业务操作 · ${action.command || "待执行"}` : "补充业务信息"),
    el("p", `确认摘要：${approval.digest} · 版本：${approval.version}`, "muted"),
  );
  if (isAction) {
    card.append(details("冻结动作参数与服务端操作", {
      action, operation_id: operationId, approval_id: approval.id,
    }));
    if (operationId) {
      const linked = el("button", "查看关联操作", "quiet"); linked.type = "button";
      linked.addEventListener("click", () => selectOperation(operationId)
        .catch(error => notice(error.message)));
      card.append(linked);
    }
  } else {
    card.append(el("p", approval.reply_payload?.question || task.waiting?.payload?.question
      || "请提供完成当前业务步骤所需的信息。"));
  }
  const controls = el("div", undefined, "approval-controls");
  const send = async (decision, extra = {}) => {
    if (state.task?.id !== task.id) return;
    for (const button of controls.querySelectorAll("button")) button.disabled = true;
    try {
      await api(`/tasks/${task.id}/approvals/${approval.id}/reply`, {
        method: "POST", body: {expected_digest: approval.digest, decision, ...extra},
      });
      notice("本人决定已保存；Agent 将按当前回复继续处理。");
      await selectTask(task.id);
      await loadOperations();
    } catch (error) {
      notice(error.message);
      for (const button of controls.querySelectorAll("button")) button.disabled = false;
    }
  };
  const reject = el("button", "拒绝", "danger"); reject.type = "button";
  reject.addEventListener("click", () => send("reject"));
  if (isAction) {
    const approve = el("button", "批准当前冻结参数"); approve.type = "button";
    approve.addEventListener("click", () => send("approve"));
    const editor = el("textarea"); editor.rows = 5;
    editor.setAttribute("aria-label", "编辑完整业务操作参数 JSON");
    editor.value = JSON.stringify(action.params || {}, null, 2);
    const edit = el("button", "修改并重新规划"); edit.type = "button";
    edit.addEventListener("click", () => {
      try { send("edit", {edited_params: JSON.parse(editor.value)}); }
      catch { notice("请填写有效的完整 JSON 参数。"); }
    });
    controls.append(approve, reject, editor, edit);
  } else {
    const answer = el("textarea"); answer.rows = 3;
    answer.setAttribute("aria-label", "补充业务信息");
    const confirm = el("button", "提交补充信息"); confirm.type = "button";
    confirm.addEventListener("click", () => {
      if (!answer.value.trim()) { notice("请填写具体补充信息。"); return; }
      send("confirm", {confirmation: answer.value.trim()});
    });
    controls.append(answer, confirm, reject);
  }
  card.append(controls); $("approvals").append(card);
}
function connectEvents(id, sequence) {
  const cursor = state.cursor.get(id) || 0;
  $("events").replaceChildren();
  state.stream = new EventSource(`/api/v2/business/tasks/${id}/events?after=${cursor}`);
  state.stream.onopen = () => { $("connection").textContent = "实时进度已连接"; };
  state.stream.onerror = () => { $("connection").textContent = "进度连接中断，正在重新连接"; };
  state.stream.addEventListener("task_event", event => {
    if (sequence !== state.selection || state.task?.id !== id) return;
    const item = JSON.parse(event.data);
    if (item.seq <= (state.cursor.get(id) || 0)) return;
    state.cursor.set(id, item.seq);
    $("events").append(el("li", `${item.seq} · ${item.kind} · ${JSON.stringify(item.payload)}`));
    clearTimeout(state.refreshTimer);
    state.refreshTimer = setTimeout(() => {
      if (sequence === state.selection) selectTask(id).catch(error => notice(error.message));
    }, 150);
  });
}
async function loadTickets() {
  const result = await api("/tickets");
  $("ticket-list").replaceChildren();
  if (!result.tickets.length) $("ticket-list").append(el("p", "没有当前身份可访问的工单。", "muted"));
  for (const ticket of result.tickets) {
    const button = el("button", `${ticket.status} · ${ticket.title}\n${ticket.id.slice(0, 8)} · revision ${ticket.revision}`);
    button.type = "button";
    button.addEventListener("click", () => selectTicket(ticket.id).catch(error => notice(error.message)));
    $("ticket-list").append(button);
  }
}
async function selectTicket(id) {
  const result = await api(`/tickets/${id}`);
  $("ticket-detail").replaceChildren(
    el("h3", `${result.ticket.title} · ${result.ticket.status}`),
    details("工单及授权历史", result),
  );
}
async function loadOperations() {
  const operations = await api("/operations");
  $("operation-list").replaceChildren();
  if (!operations.length) $("operation-list").append(el("p", "没有可查看的操作。", "muted"));
  for (const operation of operations) {
    const source = operation.source === "external_mcp" ? "独立 MCP" : "DecAgent";
    const button = el("button", `${operationLabels[operation.state] || operation.state} · ${operation.command} · ${source}\n${operation.operation_id}`,
      state.operation?.operation_id === operation.operation_id ? "active" : "");
    button.type = "button";
    button.addEventListener("click", () => selectOperation(operation.operation_id).catch(error => notice(error.message)));
    $("operation-list").append(button);
  }
}
async function selectOperation(id) {
  const sequence = ++state.operationSelection;
  const operation = await api(`/operations/${encodeURIComponent(id)}`);
  if (sequence !== state.operationSelection) return;
  state.operation = operation; renderOperation(operation); await loadOperations();
}
function renderOperation(operation) {
  $("operation-detail").hidden = false;
  $("operation-title").textContent = `${operation.command} · ${operationLabels[operation.state] || operation.state}`;
  $("operation-body").replaceChildren(); $("operation-actions").replaceChildren();
  const source = operation.source === "external_mcp" ? "独立 MCP 客户端" : "DecAgent";
  $("operation-body").append(
    el("p", `来源：${source} · 原操作 ID：${operation.operation_id}`),
    el("p", `员工：${operation.employee_id} · 准备时 revision：${operation.expected_revision ?? "新建"}`),
    details("冻结参数、输入摘要与资源快照", {
      params: operation.params, input_hash: operation.input_hash,
      resource_snapshot: operation.resource_snapshot, supersedes: operation.supersedes,
      approval_expires_at: operation.approval_expires_at,
    }),
  );
  if (operation.state === "succeeded" && operation.result) {
    $("operation-body").append(details("已核实业务结果与回执", operation.result));
  } else if (operation.state === "unknown" || operation.state === "running") {
    $("operation-body").append(el("p", "执行结果尚不确定。只查询这个原操作 ID，不创建新操作重试。", "muted"));
  } else if (operation.state === "ready" && operation.source === "external_mcp") {
    $("operation-body").append(el("p", "确认已记录。请回到原 MCP 客户端，用同一 operation_id 提交。", "muted"));
  }
  if (operation.error_code) $("operation-body").append(el("p", `原因：${operation.error_code}`, "failed"));
  const verify = el("button", "核实原操作 ID", "quiet"); verify.type = "button";
  verify.addEventListener("click", () => selectOperation(operation.operation_id).catch(error => notice(error.message)));
  $("operation-actions").append(verify);
  if (operation.state !== "awaiting_approval" || operation.employee_id !== state.identity.employee_id) return;
  const text = el("textarea"); text.rows = 5; text.value = JSON.stringify(operation.params, null, 2);
  text.setAttribute("aria-label", "编辑操作完整参数");
  $("operation-actions").append(el("p", "请核对参数和 revision。编辑将产生新操作，并要求重新确认。"), text);
  for (const [decision, label] of [["approve", "批准"], ["reject", "拒绝"], ["edit", "编辑并重新准备"]]) {
    const button = el("button", label, decision === "reject" ? "danger" : ""); button.type = "button";
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const body = {expected_hash: operation.input_hash, decision};
        if (decision === "edit") body.edited_params = JSON.parse(text.value);
        const updated = await api(`/operations/${encodeURIComponent(operation.operation_id)}/reply`, {method: "POST", body});
        notice(decision === "edit" ? "已创建新操作，请重新核对并确认。" : "决定已记录；实际业务提交仍需原操作核实。" );
        await selectOperation(updated.operation_id);
      } catch (error) { notice(error.message); } finally { button.disabled = false; }
    });
    $("operation-actions").append(button);
  }
}
async function enter() {
  const identity = await api("/auth/session");
  resetWorkspace(false); state.identity = identity;
  $("current-identity").textContent = `${identity.login} · ${identity.role}`;
  $("current-identity").hidden = false;
  $("login-panel").hidden = true; $("workbench").hidden = false; $("logout").hidden = false;
  $("clear-guest").hidden = state.sessionKind !== "guest";
  renderGuestRoles();
  const outcomes = await Promise.allSettled([loadPlugins(), loadTasks(), loadTickets(), loadOperations()]);
  const failed = outcomes.filter(item => item.status === "rejected");
  if (failed.length) notice(`${failed.length} 个业务视图暂不可用：${failed[0].reason.message}`);
  const selected = new URLSearchParams(location.hash.slice(1)).get("operation");
  if (selected) await selectOperation(selected);
}

bind("login-form", "submit", async () => {
  const response = await fetch("/api/v2/business/auth/session", {
    method: "POST", headers: {"Content-Type": "application/json"}, credentials: "same-origin",
    body: JSON.stringify({login: $("employee-login").value.trim(), password: $("employee-password").value}),
  });
  $("employee-password").value = "";
  const result = await response.json();
  if (!response.ok) throw new Error(`${result.code}: ${result.message}`);
  state.csrf = result.csrf_token;
  state.sessionKind = "employee"; state.guestRoles = []; state.guestRoleKey = "";
  sessionStorage.setItem("decagent.employee.csrf", state.csrf);
  sessionStorage.setItem("decagent.session.kind", state.sessionKind);
  await enter();
});
bind("logout", "click", async () => {
  if (state.sessionKind === "guest") {
    const response = await fetch("/api/v3/guest/session", {
      method: "DELETE", credentials: "same-origin",
      headers: {"X-CSRF-Token": state.csrf, "Idempotency-Key": crypto.randomUUID()},
    });
    const result = await response.json();
    if (!response.ok) throw new Error(`${result.code || "guest_logout_failed"}: ${result.message || "退出游客体验失败"}`);
  } else {
    await api("/auth/session", {method: "DELETE"});
  }
  clearSessionCache();
  resetWorkspace();
});
bind("clear-guest", "click", async buttonEvent => {
  const button = buttonEvent.currentTarget;
  button.disabled = true;
  try {
    const response = await fetch("/api/v3/guest/workspace", {
      method: "DELETE", credentials: "same-origin",
      headers: {"X-CSRF-Token": state.csrf, "Idempotency-Key": crypto.randomUUID()},
    });
    const result = await response.json();
    if (!response.ok) throw new Error(`${result.code || "guest_clear_failed"}: ${result.message || "清空游客空间失败"}`);
    clearSessionCache(); resetWorkspace();
    notice("游客空间已停用；后台清理会遵循任务租约和未知操作核实边界。此浏览器无法恢复该空间。" );
  } finally { button.disabled = false; }
});
bind("start-guest", "click", async buttonEvent => {
  const button = buttonEvent.currentTarget;
  button.disabled = true;
  try {
    const response = await fetch("/api/v3/guest/session", {
      method: "POST", credentials: "same-origin",
    });
    const result = await response.json();
    if (!response.ok) throw new Error(`${result.code || "guest_start_failed"}: ${result.message || "游客体验暂不可用"}`);
    state.csrf = result.csrf_token;
    state.sessionKind = "guest"; state.guestRoles = result.roles || [];
    state.guestRoleKey = result.current_role_key || "";
    sessionStorage.setItem("decagent.employee.csrf", state.csrf);
    sessionStorage.setItem("decagent.session.kind", state.sessionKind);
    await enter();
    notice("已进入独立合成工作区；切换体验身份会清空当前页面的数据视图。" );
  } finally { button.disabled = false; }
});
bind("guest-role", "change", async event => {
  const roleKey = event.currentTarget.value;
  if (!roleKey || roleKey === state.guestRoleKey) return;
  const selector = event.currentTarget;
  selector.disabled = true;
  try {
    const response = await fetch("/api/v3/guest/role", {
      method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": state.csrf,
        "Idempotency-Key": crypto.randomUUID()},
      body: JSON.stringify({role_key: roleKey}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(`${result.code || "guest_role_switch_failed"}: ${result.message || "切换身份失败"}`);
    state.csrf = result.csrf_token; state.guestRoles = result.roles || [];
    state.guestRoleKey = result.current_role_key || roleKey;
    sessionStorage.setItem("decagent.employee.csrf", state.csrf);
    sessionStorage.setItem("decagent.session.kind", "guest");
    resetWorkspace(false);
    await enter();
    notice("体验身份已切换，旧身份的工单、任务、审批和追踪视图已清空。" );
  } catch (error) {
    selector.value = state.guestRoleKey;
    throw error;
  } finally { selector.disabled = false; }
});
bind("plugin-select", "change", describePlugin);
bind("refresh", "click", async () => {
  const selectedTaskId = state.task?.id;
  const selection = state.selection;
  await loadTasks();
  if (selectedTaskId && selection === state.selection && state.task?.id === selectedTaskId) {
    await selectTask(selectedTaskId);
  }
});
bind("refresh-tickets", "click", loadTickets);
bind("refresh-operations", "click", async () => {
  await loadOperations(); if (state.operation) await selectOperation(state.operation.operation_id);
});
bind("refresh-memories", "click", async () => {
  if (!state.task) { notice("先选择一个业务任务，再查看其记忆依赖。"); return; }
  await selectTask(state.task.id);
});
bind("business-form", "submit", async () => {
  const button = $("submit-task"); button.disabled = true;
  try {
    const body = {scope_id: $("plugin-select").value, instruction: $("instruction").value,
      strategy: $("strategy").value, budget: {max_calls: Number($("max-calls").value),
        max_tokens: Number($("max-tokens").value), max_active_seconds: Number($("max-active").value),
        max_cost: $("max-cost").value.trim() || null}};
    const task = await api("/tasks", {method: "POST", body});
    await selectTask(task.id);
  } finally { button.disabled = false; }
});
bind("cancel-task", "click", async () => {
  const id = state.task.id, selection = state.selection;
  await api(`/tasks/${id}/cancel`, {method: "POST", body: {}});
  if (selection === state.selection && state.task?.id === id) await selectTask(id);
});
bind("resume-task", "click", async () => {
  const id = state.task.id, selection = state.selection;
  await api(`/tasks/${id}/resume`, {method: "POST", body: {}});
  if (selection === state.selection && state.task?.id === id) await selectTask(id);
});
bind("budget-form", "submit", async () => {
  const id = state.task.id, selection = state.selection;
  const budget = {...state.task.budget, max_calls: Number($("resume-calls").value),
    max_tokens: Number($("resume-tokens").value), max_active_seconds: Number($("resume-active").value),
    max_cost: $("resume-cost").value.trim() || null};
  await api(`/tasks/${id}/resume`, {method: "POST", body: {budget}});
  if (selection === state.selection && state.task?.id === id) await selectTask(id);
});
bind("issue-credential", "click", async () => {
  const credential = await api("/auth/mcp-credentials", {method: "POST"});
  state.credentialRef = credential.credential_ref;
  $("credential-info").replaceChildren(el("p", `凭据引用：${credential.credential_ref}`),
    el("p", `受保护文件：${credential.credential_path}`), el("p", `到期：${credential.expires_at}`));
  $("revoke-credential").hidden = false;
});
bind("revoke-credential", "click", async () => {
  await api(`/auth/mcp-credentials/${state.credentialRef}`, {method: "DELETE"});
  state.credentialRef = null; $("credential-info").replaceChildren();
  $("revoke-credential").hidden = true;
});
function clearSessionCache() {
  sessionStorage.removeItem("decagent.employee.csrf");
  sessionStorage.removeItem("decagent.session.kind");
  state.csrf = ""; state.sessionKind = ""; state.guestRoles = []; state.guestRoleKey = "";
}
function renderGuestRoles() {
  const control = $("guest-role-control"), selector = $("guest-role");
  control.hidden = state.sessionKind !== "guest";
  selector.replaceChildren();
  for (const role of state.guestRoles) {
    const option = el("option", `${role.name} · ${role.role} · ${role.team}`);
    option.value = role.key;
    selector.append(option);
  }
  selector.value = state.guestRoleKey;
}
async function restoreGuestSession() {
  let response = await fetch("/api/v3/guest/session", {credentials: "same-origin"});
  if (!response.ok) return false;
  let result = await response.json();
  if (!result.csrf_token) {
    response = await fetch("/api/v3/guest/session", {
      method: "POST", credentials: "same-origin",
    });
    if (!response.ok) return false;
    result = await response.json();
  }
  state.csrf = result.csrf_token; state.sessionKind = "guest";
  state.guestRoles = result.roles || []; state.guestRoleKey = result.current_role_key || "";
  sessionStorage.setItem("decagent.employee.csrf", state.csrf);
  sessionStorage.setItem("decagent.session.kind", state.sessionKind);
  await enter();
  return true;
}
function hasGuestSessionHint() {
  return document.cookie.split(";").some(cookie =>
    cookie.trim().startsWith("decagent_guest_csrf="));
}
async function initializeWorkbench() {
  let guestEnabled = false;
  try {
    const status = await fetch("/api/v3/guest/status", {credentials: "same-origin"});
    if (status.ok) guestEnabled = Boolean((await status.json()).enabled);
  } catch (_) { /* Employee sign-in remains available if trial status cannot be read. */ }
  $("guest-entry").hidden = !guestEnabled;

  if (state.sessionKind === "guest") {
    if (guestEnabled && hasGuestSessionHint() && await restoreGuestSession()) return;
    clearSessionCache(); resetWorkspace();
  } else if (state.csrf) {
    state.sessionKind = "employee";
    await enter();
    return;
  }
  if (guestEnabled && hasGuestSessionHint() && await restoreGuestSession()) return;
}
initializeWorkbench().catch(error => { clearSessionCache(); resetWorkspace(); notice(error.message); });
