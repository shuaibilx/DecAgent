const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

function element(tagName = 'div') {
  return {tagName, children: [], listeners: {}, textContent: '',
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; },
    addEventListener(name, callback) { this.listeners[name] = callback; },
    setAttribute() {}, removeAttribute() {}};
}

function allText(node) {
  return [node.textContent, ...node.children.map(allText)].filter(Boolean).join(' ');
}

const elements = new Map();
const context = vm.createContext({document: {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  },
  createElement: tagName => element(tagName),
}, sessionStorage: {getItem() { return ''; }, setItem() {}, removeItem() {}},
  setTimeout, clearTimeout, fetch() { throw Error('Unexpected network request'); }});

vm.runInContext(fs.readFileSync('decagent/gateway/static/business.js', 'utf8'), context);
const snapshot = {
  task_id: 'task-fixed', legacy: false,
  plan: {revision: 1, nodes: [], goal: {description: '完成业务目标'}},
  nodes: [], inactive_nodes: [], policies: [],
  threshold_explanation: {
    certainty: 'Jev 对本次答案的把握程度，不代表判断已被证明正确。',
    accuracy: '在独立验证数据集上观察到的实际正确比例。',
    confidence_interval: '验证集实测正确率的统计不确定范围，不代表单次答案把握度。',
  },
};

vm.runInContext('renderPlan(snapshot, null, null, null)', Object.assign(context, {snapshot}));
const rendered = allText(elements.get('plan-summary'));
for (const phrase of ['Jev 对本次答案的把握程度', '独立验证数据集上观察到的实际正确比例',
  '统计不确定范围']) {
  assert.ok(rendered.includes(phrase), `plan summary must show ${phrase} without a bound policy`);
}
const policySnapshot = {...snapshot, policies: [{policy_id: 'routing-policy', policy_version: '1',
  status: 'published', minimum_threshold: 0.9, effective_threshold: 0.9,
  override_revision: 0, allowed_thresholds: [0.9, 0.95]}]};
vm.runInContext('renderPlan(policySnapshot, null, null, null)',
  Object.assign(context, {policySnapshot}));
assert.ok(allText(elements.get('plan-policies')).includes('管理员下限 0.9'));
assert.ok(allText(elements.get('plan-policies')).includes('保存任务阈值'));
console.log('unbound-policy certainty and bound-policy threshold controls render');
