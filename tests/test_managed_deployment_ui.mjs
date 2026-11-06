/** Run: node tests/test_managed_deployment_ui.mjs; no browser or network. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const html = readFileSync(new URL('../admin.html', import.meta.url), 'utf8');
new vm.Script(html.match(/<script>([\s\S]*?)<\/script>/)[1]);
const flags = html.slice(html.indexOf('const MANAGED_DEPLOYMENT ='), html.indexOf('let _notifiedSha ='));
const upgrade = html.slice(html.indexOf('async function upgradeNow(){'), html.indexOf('/* ================= 总览刷新'));
assert.match(html, /body\[data-managed-deployment="true"\] \[data-repo-write\] \{ display:none !important; \}/);
for (const id of ['btnRepoPull', 'btnBannerUpgrade']) {
  assert.match(html, new RegExp(`id="${id}" data-repo-write`));
}

for (const managed of [true, false]) {
  const calls = [];
  const nodes = new Map();
  const ctx = vm.createContext({
    LANG: 'en',
    document: {body: {dataset: {managedDeployment: String(managed)}}},
    $: (id) => {
      if (!nodes.has(id)) nodes.set(id, {classList: {remove() {}}});
      return nodes.get(id);
    },
    confirm: () => {calls.push('confirm'); return true;},
    api: async () => {calls.push('api'); return {ok: true};},
    toast: () => {},
    setTimeout: () => {},
  });
  await vm.runInContext(flags + '\n' + upgrade + '\nupgradeNow()', ctx);
  assert.deepEqual(calls, managed ? [] : ['confirm', 'api']);
  if (managed) {
    assert.match(nodes.get('managedDeploymentNotice').textContent, /GitHub Actions/);
    assert.equal(nodes.get('repoPanelTitle').textContent, 'Official repository version check');
  }
}
console.log('PASS managed deployment UI: hidden writes, guarded action, development upgrade available');
