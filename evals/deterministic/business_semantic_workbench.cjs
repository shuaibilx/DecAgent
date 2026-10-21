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
(async () => {
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(context.refreshedTask, task.id, 'stale evidence refreshes the durable task');
  const message = elements.get('notice').textContent;
  assert.match(message, /候选|目录|已变化|过期/, 'the employee sees why the suggestion was not accepted');
  assert.doesNotMatch(message, /判断已保存/, 'stale evidence is never reported as accepted');

  const combinedText = node => node.textContent + node.children.map(combinedText).join('');
  const submitDecision = async ({id, type, criteria, action, value, text}) => {
    const approvalId = `approval-${id}`;
    const decisionTask = {
      id, state: 'awaiting_input',
      waiting: {payload: {
        kind: 'decision', approval_id: approvalId, digest: 'c'.repeat(64), plan_revision: 7,
        question: {id: `question-${type}`, type, instructions: '依据当前合成业务信息判断', criteria},
        suggestion: {selected: 'suggested-candidate', confidence: 0.88},
        raw_model_answer: {primitive: type, selected: 'suggested-candidate'},
      }},
    };
    const decisionApproval = {id: approvalId, kind: 'decision', status: 'pending', digest: 'c'.repeat(64)};
    elements.get('approvals').replaceChildren();
    vm.runInContext('state.task = task; renderTaskApproval(task, approval);',
      Object.assign(context, {task: decisionTask, approval: decisionApproval}));
    const card = elements.get('approvals').children[0];
    const controls = card.children[card.children.length - 1];
    assert.match(combinedText(card), /Jev 建议：/, 'the received Jev suggestion is visible to the employee');
    assert.doesNotMatch(combinedText(card), /批准当前冻结参数|确认具体业务操作/,
      'semantic decision card contains no business write approval control');

    const selector = controls.children.find(item => item.tag === 'select');
    const textarea = controls.children.find(item => item.tag === 'textarea');
    if (value !== undefined) selector.value = String(value);
    if (text !== undefined) textarea.value = text;
    const button = controls.querySelectorAll('button').find(item => item.textContent === action);
    assert.ok(button, `${type} exposes ${action}`);
    const oldPendingCount = pending.length;
    button.click();
    assert.equal(pending.length, oldPendingCount + 1, `${action} sends one semantic reply`);
    const request = pending.pop();
    assert.match(request.url, /\/decisions\//, 'semantic input uses the decision endpoint');
    assert.doesNotMatch(request.url, /\/operations\//, 'semantic input cannot submit a business write');
    const body = JSON.parse(request.options.body);
    assert.equal(body.expected_digest, 'c'.repeat(64));
    assert.equal(body.expected_plan_revision, 7);
    request.resolve({ok: true, json: async () => ({status: 'accepted'})});
    await new Promise(resolve => setImmediate(resolve));
    assert.match(elements.get('notice').textContent, /该判断不会批准任何业务写操作/,
      'the reply explains that semantic selection does not approve a write');
    return body;
  };

  const choice = await submitDecision({
    id: 'guest-choice', type: 'choice',
    criteria: {candidate: '当前候选', no_match: '都不符合'},
    action: '采用所选判断', value: 'candidate',
  });
  assert.equal(choice.decision, 'select');
  assert.equal(choice.value, 'candidate');

  const noul = await submitDecision({
    id: 'guest-noul', type: 'noul', criteria: {true: '需要人工介入', false: '可自动处理'},
    action: '采用所选判断', value: 'false',
  });
  assert.equal(noul.value, false, 'Noul sends a boolean, not a string');

  const score = await submitDecision({
    id: 'guest-score', type: 'score', criteria: ['低优先级', '普通优先级', '紧急'],
    action: '采用所选判断', value: '2',
  });
  assert.equal(score.value, 2, 'Score sends a numeric scale value');

  const noMatch = await submitDecision({
    id: 'guest-no-match', type: 'choice', criteria: {candidate: '当前候选', no_match: '都不符合'},
    action: '都不符合',
  });
  assert.equal(noMatch.decision, 'no_match');
  assert.equal('value' in noMatch, false);

  const supplement = await submitDecision({
    id: 'guest-supplement', type: 'noul', criteria: {true: '是', false: '否'},
    action: '补充事实后重判', text: '员工已确认无法在当前地点办公',
  });
  assert.equal(supplement.decision, 'supplement');
  assert.equal(supplement.text, '员工已确认无法在当前地点办公');

  const reject = await submitDecision({
    id: 'guest-reject', type: 'score', criteria: ['低', '中', '高'], action: '拒绝当前判断',
  });
  assert.equal(reject.decision, 'reject');
  console.log('guest Choice/Noul/Score, no-match, supplement, reject and write-separation checks passed');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
