"use strict";

// A stale server decision is not a successful employee reply; the UI must explain
// the refreshed candidates and prevent duplicate clicks while the request settles.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

function element(tag = 'div') {
  return {
    tag, children: [], listeners: {}, value: '', hidden: false, disabled: false,
    textContent: '',
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; },
    addEventListener(name, callback) { this.listeners[name] = callback; },
    setAttribute() {},
    querySelectorAll(name) {
      const matches = [];
      const visit = item => {
        if (name === 'button' && item.tag === 'button') matches.push(item);
        for (const child of item.children || []) visit(child);
      };
      visit(this);
      return matches;
    },
    click() {
      if (!this.disabled) return this.listeners.click?.({preventDefault() {}});
    },
    get options() { return this.children.filter(item => item.tag === 'option'); },
    get childElementCount() { return this.children.length; },
  };
}

const elements = new Map();
const pending = [];
const context = vm.createContext({
  document: {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, element());
      return elements.get(id);
    },
    createElement(tag) { return element(tag); },
  },
  sessionStorage: {getItem() {return '';}, setItem() {}, removeItem() {}},
  crypto: {randomUUID() {return 'semantic-reply-idempotency';}},
  setTimeout, clearTimeout,
  EventSource: function () { this.close = () => {}; this.addEventListener = () => {}; },
  fetch(url, options) {
    if (url === "/api/v3/guest/status") {
      return Promise.resolve({ok: true, json: async () => ({enabled: false})});
    }
    if (url === "/api/v3/guest/session") {
      return Promise.resolve({ok: false, status: 404, json: async () => ({})});
    }
    return new Promise(resolve => pending.push({url, options, resolve}));
  },
});

vm.runInContext(fs.readFileSync('decagent/gateway/static/business.js', 'utf8'), context);
const task = {
  id: 'task-with-stale-choice', state: 'awaiting_input',
  waiting: {payload: {
    kind: 'decision', approval_id: 'approval-1', digest: 'a'.repeat(64), plan_revision: 3,
    question: {id: 'person', type: 'choice', instructions: '选择当前在职员工', criteria: {
      'person-1': JSON.stringify({id: 'person-1', name: '林晓'}), no_match: '都不符合',
    }},
  }},
  approvals: [{id: 'approval-1', kind: 'decision', status: 'pending', digest: 'a'.repeat(64)}],
  actions: [], decisions: [], budget: {},
};
const approval = task.approvals[0];
const memory = {
  id: 'pending-memory', kind: 'confirmed_preference', subject: 'response_language',
  status: 'pending_confirmation', active: false, verified: false, expired: false,
  data: {value: '中文'}, created_at: '2026-10-17T00:00:00Z',
  expires_at: '2026-11-16T00:00:00Z', content_digest: 'b'.repeat(64),
};
vm.runInContext('state.task = task; renderTaskApproval(task, approval);',
  Object.assign(context, {task, approval}));
vm.runInContext('selectTask = async id => { globalThis.refreshedTask = id; }', context);
vm.runInContext('renderMemories([memory], {not_declared: true}, null, null)',
  Object.assign(context, {memory}));
assert.equal(elements.get('memory-list').children.length, 1,
  'an employee can manage their own memory when no workflow slot reads it');
assert.equal(elements.get('memory-list').querySelectorAll('button')[0].textContent,
  '确认启用这条记忆');

const buttons = elements.get('approvals').querySelectorAll('button');
const choose = buttons.find(button => button.textContent === '采用所选判断');
assert.ok(choose, 'choice reply control is rendered');
choose.click();
choose.click();
assert.equal(pending.length, 1, 'a fast repeated click sends only one reply');
assert.ok(buttons.every(button => button.disabled), 'all reply choices are locked in flight');

pending[0].resolve({ok: true, json: async () => ({status: 'stale'})});
setImmediate(async () => {
  assert.equal(context.refreshedTask, task.id, 'stale evidence refreshes the durable task');
  const message = elements.get('notice').textContent;
  assert.match(message, /候选|目录|已变化|过期/, 'the employee sees why the suggestion was not accepted');
  assert.doesNotMatch(message, /判断已保存/, 'stale evidence is never reported as accepted');
  console.log('stale-choice prompt and duplicate-click checks passed');
});
