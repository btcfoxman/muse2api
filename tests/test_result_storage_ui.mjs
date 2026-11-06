/** Real admin-page helpers with a fake DOM/API; no browser, accounts or network. */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const html = readFileSync(new URL('../admin.html', import.meta.url), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)?.[1];
assert.ok(script, 'admin page must have its executable script');
new vm.Script(script);
const first = script.indexOf('/* ================= S3 结果存储 ================= */');
const last = script.indexOf('/* ================= 媒体库 ================= */', first);
assert.ok(first !== -1 && last > first, 'storage helper section must be available');
const helpers = script.slice(first, last);

function settings(overrides = {}) {
  return {
    result_s3_enabled: true,
    result_s3_backend: 's3_center_direct',
    result_s3_center_api_base: 'https://s3.invalid',
    result_s3_center_token_configured: true,
    result_s3_bucket: 'assets',
    result_s3_key_prefix: 'aicontent/ai-muse',
    ...overrides,
  };
}

function harness(initialSettings = settings()) {
  const nodes = new Map();
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, {
      value: '', checked: false, disabled: false, textContent: '', className: '',
      listeners: {}, addEventListener(name, handler) { this.listeners[name] = handler; },
    });
    return nodes.get(id);
  };
  const mediaButtons = [{disabled: false}, {disabled: false}];
  const state = {calls: [], toasts: [], tabs: [], mediaRefreshes: 0, taskRefreshes: 0};
  let response = initialSettings;
  let responder;
  const ctx = vm.createContext({
    $, document: {querySelectorAll: () => mediaButtons},
    api: async (path, opts) => {
      state.calls.push({path, opts});
      return responder ? responder(path, opts) : response;
    },
    toast: (message, kind) => state.toasts.push({message, kind}),
    goTab: tab => state.tabs.push(tab),
    loadMedia: () => state.mediaRefreshes++,
    loadTasks: () => state.taskRefreshes++,
    confirm: () => false,
  });
  vm.runInContext(helpers, ctx);
  return {
    $, nodes, mediaButtons, state,
    run: code => vm.runInContext(code, ctx),
    setResponse(value) { response = value; responder = undefined; },
    setResponder(value) { responder = value; },
  };
}

test('loading and saving never echo a token; blank token preserves the saved secret', async () => {
  const h = harness(settings({result_s3_center_token: 'server-secret-must-not-render'}));
  await h.run('loadResultStorage()');
  assert.equal(h.$('storageToken').value, '');
  assert.equal(h.$('storageTransferButton').disabled, false);
  const visibleText = [...h.nodes.values()].map(node => node.value + node.textContent).join('\n');
  assert.ok(!visibleText.includes('server-secret-must-not-render'));

  await h.run('saveResultStorage({preventDefault(){}})');
  const save = h.state.calls.at(-1);
  assert.equal(save.path, '/admin/result-storage');
  assert.equal(save.opts.method, 'POST');
  const payload = JSON.parse(save.opts.body);
  assert.equal(Object.hasOwn(payload, 'result_s3_center_token'), false,
    'saving unrelated settings must not clear or resend a saved secret');
  assert.equal(payload.result_s3_center_api_base, 'https://s3.invalid');
  assert.equal(payload.result_s3_enabled, true);

  h.$('storageToken').value = '  new-fixture-token  ';
  await h.run('saveResultStorage({preventDefault(){}})');
  assert.equal(JSON.parse(h.state.calls.at(-1).opts.body).result_s3_center_token, 'new-fixture-token');
  assert.equal(h.$('storageToken').value, '', 'a successful save must clear the password field');
  assert.ok(h.state.toasts.every(toast => !toast.message.includes('new-fixture-token')));
});

test('opening the tab again preserves unsaved edits; cancelled reload makes no request', async () => {
  const h = harness();
  await h.run('loadResultStorage()');
  h.$('storageBucket').value = 'unsaved-bucket';
  h.$('storageForm').listeners.input();
  const count = h.state.calls.length;
  await h.run('loadResultStorage()');
  h.run('reloadResultStorage()');
  assert.equal(h.state.calls.length, count);
  assert.equal(h.$('storageBucket').value, 'unsaved-bucket');
});

for (const status of ['failed', 'disabled', 'unexpected']) {
  test(`HTTP success with storage_status=${status} is not shown as completed storage`, async () => {
    const h = harness();
    await h.run('loadResultStorage()');
    h.setResponse({storage_status: status, storage_error: 'safe-fixture-error'});
    await h.run("transferResultToStorage({filename:'original.webp'})");
    assert.equal(h.state.calls.at(-1).path, '/admin/result-storage/transfer');
    assert.deepEqual(JSON.parse(h.state.calls.at(-1).opts.body), {filename: 'original.webp'});
    assert.equal(h.state.mediaRefreshes, 0);
    assert.equal(h.state.taskRefreshes, 0);
    assert.equal(h.state.toasts.at(-1).kind, 'warn');
    assert.equal(h.$('storageTransferFeedback').textContent, 'safe-fixture-error');
    assert.equal(h.$('storageTransferButton').disabled, false, 'the same original can be retried');
    assert.ok(h.state.calls.every(call => call.path.startsWith('/admin/result-storage')),
      'storage retry must never call a generation endpoint');
  });
}

test('a stored result refreshes links and duplicate clicks cannot upload twice', async () => {
  const h = harness();
  await h.run('loadResultStorage()');
  let complete;
  h.setResponder(() => new Promise(resolve => { complete = resolve; }));
  const pending = h.run("transferResultToStorage({task_id:'task_fixture'})");
  assert.equal(h.$('storageTransferButton').disabled, true);
  assert.ok(h.mediaButtons.every(button => button.disabled));
  const callCount = h.state.calls.length;
  await h.run("transferResultToStorage({task_id:'task_fixture'})");
  assert.equal(h.state.calls.length, callCount);
  complete({storage_status: 'stored', url: 'https://public.invalid/original.webp'});
  await pending;
  assert.equal(h.state.mediaRefreshes, 1);
  assert.equal(h.state.taskRefreshes, 1);
  assert.equal(h.state.toasts.at(-1).kind, 'ok');
  assert.equal(h.$('storageTransferButton').disabled, false);
  assert.ok(h.mediaButtons.every(button => !button.disabled));
  assert.ok(!h.$('storageTransferFeedback').textContent.includes('https://'),
    'feedback must not print signed result URLs');
});

test('disabled storage blocks transfers and directs the user to settings', async () => {
  const h = harness(settings({result_s3_enabled: false}));
  await h.run('loadResultStorage()');
  const count = h.state.calls.length;
  await h.run("transferResultToStorage({filename:'original.webp'})");
  assert.equal(h.state.calls.length, count);
  assert.equal(h.$('storageTransferButton').disabled, true);
  assert.ok(h.mediaButtons.every(button => button.disabled));
  assert.deepEqual(h.state.tabs, ['storage']);
  assert.equal(h.state.toasts.at(-1).kind, 'warn');
});

test('transfer form sends exactly one identity and empty input is not submitted', async () => {
  const h = harness();
  await h.run('loadResultStorage()');
  h.setResponse({storage_status: 'stored'});
  for (const [type, value] of [['task_id', 'task_fixture'], ['filename', 'original.webp']]) {
    h.$('storageTransferType').value = type;
    h.$('storageTransferIdentity').value = '  ' + value + '  ';
    await h.run('transferStoredResult({preventDefault(){}})');
    assert.deepEqual(JSON.parse(h.state.calls.at(-1).opts.body), {[type]: value});
  }
  h.$('storageTransferIdentity').value = ' ';
  const count = h.state.calls.length;
  await h.run('transferStoredResult({preventDefault(){}})');
  assert.equal(h.state.calls.length, count);
});
