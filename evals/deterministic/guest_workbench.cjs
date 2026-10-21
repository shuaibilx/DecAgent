"use strict";

const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");

function makeElement(tag = "div") {
  return {
    tag, children: [], listeners: {}, value: "", hidden: false, disabled: false,
    textContent: "", className: "",
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; },
    addEventListener(name, callback) { this.listeners[name] = callback; },
    setAttribute() {}, removeAttribute() {},
    querySelectorAll(name) {
      const matches = [];
      const visit = item => {
        if (name === "button" && item.tag === "button") matches.push(item);
        for (const child of item.children || []) visit(child);
      };
      visit(this);
      return matches;
    },
    get options() { return this.children.filter(item => item.tag === "option"); },
  };
}

function response(body, status = 200) {
  return {ok: status >= 200 && status < 300, status, json: async () => body};
}

async function boot(enabled) {
  const elements = new Map();
  const storage = new Map();
  const calls = [];
  let roleKey = "employee-it";
  let csrf = "guest-csrf-1";
  const roles = [
    {key: "employee-it", name: "张伟", team: "IT Support", role: "employee"},
    {key: "supervisor", name: "Chen Li", team: "Network", role: "supervisor"},
  ];
  const identityFor = key => ({employee_id: `employee-${key}`, login: key,
    role: key === "supervisor" ? "supervisor" : "employee", source: "employee_session"});
  const context = vm.createContext({
    document: {
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, makeElement());
        return elements.get(id);
      },
      createElement(tag) { return makeElement(tag); },
      cookie: enabled ? "decagent_guest_csrf=guest-csrf-1" : "",
    },
    sessionStorage: {
      getItem(key) { return storage.get(key) || null; },
      setItem(key, value) { storage.set(key, String(value)); },
      removeItem(key) { storage.delete(key); },
    },
    crypto: {randomUUID() { return "guest-workbench-idempotency"; }},
    location: {hash: "", pathname: "/business"},
    history: {replaceState() {}}, URLSearchParams,
    setTimeout, clearTimeout,
    EventSource: function () { this.close = () => {}; this.addEventListener = () => {}; },
    async fetch(url, options = {}) {
      calls.push({url, options});
      if (url === "/api/v3/guest/status") return response({enabled});
      if (url === "/api/v3/guest/session" && (options.method || "GET") === "GET") {
        return enabled ? response({is_guest: true, identity: identityFor(roleKey), csrf_token: csrf,
          roles, current_role_key: roleKey}) : response({code: "not_found"}, 404);
      }
      if (url === "/api/v2/business/auth/session") return response(identityFor(roleKey));
      if (url === "/api/v2/business/plugins" || url === "/api/v2/business/tasks" ||
          url === "/api/v2/business/operations") return response([]);
      if (url === "/api/v2/business/tickets") return response({tickets: []});
      if (url === "/api/v3/guest/role") {
        assert.equal(options.headers["X-CSRF-Token"], csrf, "role switch uses the active CSRF token");
        const requested = JSON.parse(options.body).role_key;
        roleKey = requested;
        csrf = "guest-csrf-2";
        return response({is_guest: true, identity: identityFor(roleKey), csrf_token: csrf,
          roles, current_role_key: roleKey});
      }
      if (url === "/api/v3/guest/workspace" && options.method === "DELETE") {
        assert.equal(options.headers["X-CSRF-Token"], csrf,
          "workspace clear uses the active CSRF token");
        return response({cleared: true});
      }
      throw new Error(`Unexpected browser request: ${url}`);
    },
  });
  vm.runInContext(fs.readFileSync("decagent/gateway/static/business.js", "utf8"), context);
  for (let attempt = 0; attempt < 20; attempt++) await new Promise(resolve => setImmediate(resolve));
  return {context, elements, storage, calls};
}

(async () => {
  const disabled = await boot(false);
  assert.equal(disabled.elements.get("guest-entry").hidden, true,
    "disabled guest trial is not advertised in the login UI");
  assert.equal(disabled.calls.some(call => call.url === "/api/v2/business/auth/session"), false,
    "a disabled trial does not restore a guest identity");

  const app = await boot(true);
  assert.equal(app.elements.get("guest-entry").hidden, false,
    "enabled guest trial is discoverable");
  assert.equal(app.elements.get("guest-role-control").hidden, false,
    "restored guest identity exposes the fixed role selector");
  assert.equal(app.elements.get("guest-role").value, "employee-it");
  assert.match(app.elements.get("current-identity").textContent, /employee-it/);

  const state = vm.runInContext("state", app.context);
  let streamClosed = false;
  state.task = {id: "old-employee-task"};
  state.operation = {operation_id: "old-employee-operation"};
  state.trace = {events: ["private old-role trace"]};
  state.memories = [{content: "old-role memory"}];
  state.stream = {close() { streamClosed = true; }};
  app.elements.get("task-list").append(Object.assign(makeElement("p"),
    {textContent: "old employee private task"}));
  app.elements.get("ticket-detail").append(Object.assign(makeElement("p"),
    {textContent: "old employee ticket details"}));
  const selector = app.elements.get("guest-role");
  selector.value = "supervisor";
  await selector.listeners.change({currentTarget: selector});

  assert.equal(state.identity.role, "supervisor");
  assert.equal(state.task, null, "role switch clears selected task state");
  assert.equal(state.operation, null, "role switch clears selected operation state");
  assert.equal(state.trace, null, "role switch clears trace state");
  assert.deepEqual(Array.from(state.memories), [], "role switch clears memory view state");
  assert.equal(streamClosed, true, "role switch closes the previous event stream");
  assert.equal(app.storage.get("decagent.employee.csrf"), "guest-csrf-2",
    "the browser stores the newly rotated CSRF token");
  assert.equal(selector.value, "supervisor");
  assert.match(app.elements.get("current-identity").textContent, /supervisor/);
  assert.equal(app.elements.get("task-list").children.some(item =>
    item.textContent.includes("old employee private task")), false,
  "the previous employee task list is removed from the page");
  assert.equal(app.elements.get("ticket-detail").children.some(item =>
    item.textContent.includes("old employee ticket details")), false,
  "the previous employee ticket detail is removed from the page");
  assert.ok(app.calls.some(call => call.url === "/api/v3/guest/role"));

  const clearButton = app.elements.get("clear-guest");
  await clearButton.listeners.click({currentTarget: clearButton});
  assert.equal(state.sessionKind, "", "workspace clear forgets the browser identity");
  assert.equal(state.csrf, "", "workspace clear forgets the CSRF token");
  assert.equal(app.storage.has("decagent.employee.csrf"), false);
  assert.equal(app.elements.get("workbench").hidden, true);
  assert.equal(app.elements.get("login-panel").hidden, false);
  assert.equal(app.elements.get("clear-guest").hidden, true);
  assert.match(app.elements.get("notice").textContent, /已停用/);
  console.log("guest trial visibility, session restore, role switch and identity-cache reset passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
