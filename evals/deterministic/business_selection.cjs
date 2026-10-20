// Reordered responses must not steal the employee's newer task selection.
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
function element() {
  return {children: [], listeners: {}, value: '', hidden: false, disabled: false,
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; },
    addEventListener(name, callback) { this.listeners[name] = callback; }, setAttribute() {}};
}
async function check(handler, event) {
  const elements = new Map(), pending = [];
  const context = vm.createContext({document: {
    getElementById(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); },
    createElement: element}, sessionStorage: {getItem() {return '';}, setItem() {}, removeItem() {}},
    crypto: {randomUUID() {return 'fixture-key';}}, setTimeout, clearTimeout,
    EventSource: function () {
      this.close = () => {};
      this.addEventListener = () => {};
    },
    fetch(url, options) {return new Promise(resolve => pending.push({url, options, resolve}));}});
  vm.runInContext(fs.readFileSync('decagent/gateway/static/business.js', 'utf8'), context);
  const task = id => ({id, state: 'succeeded', phase: 'finalize', event_seq: 0,
    budget: {}, approvals: [], decisions: [], actions: []});
  context.taskA = task('A'); context.taskB = task('B');
  vm.runInContext("state.task = taskA", context);
  const oldAction = elements.get(handler).listeners[event]({preventDefault() {}});
  const newerSelection = vm.runInContext("selectTask('B')", context);
  pending[0].resolve({ok: true, json: async () => handler === 'refresh' ? [] : context.taskA});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(pending.length, 2, `${handler} must not refresh the old task`);
  pending[1].resolve({ok: true, json: async () => context.taskB});
  await new Promise(resolve => setImmediate(resolve));
  for (let index = 2; index < pending.length; index++) {
    const request = pending[index];
    let result = [];
    if (request.url.endsWith('/tasks/B')) result = context.taskB;
    else if (request.url.endsWith('/tasks/A')) result = context.taskA;
    else if (request.url.endsWith('/plan')) result = {legacy: true, plan: null, nodes: []};
    else if (request.url.endsWith('/trace')) result = {task_id: 'B', timeline: []};
    request.resolve({ok: true, json: async () => result});
    await new Promise(resolve => setImmediate(resolve));
  }
  await Promise.all([oldAction, newerSelection]);
  assert.equal(vm.runInContext('state.task.id', context), 'B', handler);
  if (handler !== 'refresh') assert.equal(pending[0].url.includes('/tasks/A/'), true);
}
const deadline = setTimeout(() => {throw Error('Unresolved selection test');}, 5000);
(async () => {
  for (const [handler, event] of [['refresh', 'click'], ['cancel-task', 'click'],
    ['resume-task', 'click'], ['budget-form', 'submit']]) await check(handler, event);
  console.log('4 reordered-response selection checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => clearTimeout(deadline));
