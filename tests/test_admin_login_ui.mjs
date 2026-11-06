/** Run: node tests/test_admin_login_ui.mjs; real page helpers, no browser/network/secrets. */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const admin = readFileSync(new URL('../admin.html', import.meta.url), 'utf8');
const login = readFileSync(new URL('../login.html', import.meta.url), 'utf8');
new vm.Script(admin.match(/<script>([\s\S]*?)<\/script>/)[1]);
const loginSource = login.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(loginSource);
function section(start, end) {
  const first = admin.indexOf(start);
  const last = admin.indexOf(end, first);
  assert.ok(first !== -1 && last !== -1, 'page helper markers must exist');
  return admin.slice(first, last);
}
const apiSource = section('async function api(path, opts){', '\nfunction esc(');
const bootSource = section('async function initializeAdmin(){', '\nasync function logoutAdmin(){');
const logoutSource = section('async function logoutAdmin(){', '\ninitializeAdmin();');

function nodes() {
  const map = new Map();
  return id => {
    if (!map.has(id)) map.set(id, {
      value: '', textContent: '', disabled: false, hidden: false, type: 'password',
      attributes: {}, listeners: {}, focusCount: 0,
      addEventListener(name, fn) { this.listeners[name] = fn; },
      setAttribute(name, value) { this.attributes[name] = value; },
      removeAttribute(name) { delete this.attributes[name]; },
      focus() { this.focusCount++; },
    });
    return map.get(id);
  };
}
function adminContext(fetch, extra = {}) {
  const $ = nodes();
  const redirects = [];
  const state = {refreshes: 0, starts: 0, auth: []};
  const ctx = vm.createContext({
    ADMIN_LOGIN_REQUIRED: true, ADMIN_CSRF: 'fixture-csrf',
    adminSessionReady: true, adminSessionLoading: false, KEY: 'unused-api-key', realKey: 'display-key',
    authOK: true, $, fetch,
    document: {body: {dataset: {}}},
    location: {replace: path => redirects.push(path)},
    setAuth: ok => state.auth.push(ok),
    refreshAll: () => state.refreshes++, startAdminRefresh: () => state.starts++, toast() {},
    ...extra,
  });
  return {ctx, $, redirects, state};
}

{
  const calls = [];
  const {ctx} = adminContext(async (path, opts) => {
    calls.push({path, opts});
    return {status: 200, ok: true, text: async () => '{"ok":true}'};
  });
  vm.runInContext(apiSource, ctx);
  await vm.runInContext("api('/admin/accounts', {method:'POST', headers:{Authorization:'ignored', authorization:'ignored'}})", ctx);
  assert.equal(calls[0].opts.credentials, 'same-origin');
  assert.equal(calls[0].opts.headers['X-Muse-CSRF'], 'fixture-csrf');
  assert.equal(calls[0].opts.headers.Authorization, undefined);
  assert.equal(calls[0].opts.headers.authorization, undefined);
  await vm.runInContext("api('/v1/models')", ctx);
  assert.equal(calls[1].opts.headers['X-Muse-CSRF'], undefined);
  ctx.adminSessionReady = false;
  await assert.rejects(vm.runInContext("api('/admin/accounts')", ctx), /请先确认/);
  assert.equal(calls.length, 2, 'requests must wait for a confirmed session');
}
{
  const {ctx, redirects} = adminContext(async () => ({status: 401, ok: false, text: async () => '{"detail":"expired"}'}));
  vm.runInContext(apiSource, ctx);
  await assert.rejects(vm.runInContext("api('/admin/accounts')", ctx), /登录已失效/);
  assert.deepEqual(redirects, ['/admin/login']);
  assert.equal(ctx.adminSessionReady, false);
  assert.equal(ctx.document.body.dataset.adminSessionReady, 'false');
}
{
  let finish;
  let options;
  const {ctx, state} = adminContext(async (path, opts) => {
    assert.equal(path, '/admin/session');
    options = opts;
    return new Promise(resolve => { finish = resolve; });
  }, {adminSessionReady: false});
  vm.runInContext(bootSource, ctx);
  const pending = vm.runInContext('initializeAdmin()', ctx);
  assert.equal(state.refreshes, 0);
  assert.equal(options.credentials, 'same-origin');
  assert.equal(options.cache, 'no-store');
  finish({status: 200, ok: true, json: async () => ({authenticated: true, csrf_token: 'session-csrf'})});
  assert.equal(await pending, true);
  assert.equal(state.refreshes, 1);
  assert.equal(state.starts, 1);
  assert.equal(ctx.ADMIN_CSRF, 'session-csrf');
  assert.equal(ctx.document.body.dataset.adminSessionReady, 'true');
}
for (const kind of ['expired', 'malformed', 'unavailable']) {
  const {ctx, $, redirects, state} = adminContext(async () => kind === 'expired'
    ? {status: 401, ok: false}
    : kind === 'unavailable' ? {status: 503, ok: false}
    : {status: 200, ok: true, json: async () => ({authenticated: true, csrf_token: ''})},
  {adminSessionReady: false});
  vm.runInContext(bootSource, ctx);
  assert.equal(await vm.runInContext('initializeAdmin()', ctx), false);
  assert.equal(state.refreshes, 0);
  assert.equal(ctx.adminSessionReady, false);
  if (kind === 'expired') assert.deepEqual(redirects, ['/admin/login']);
  else assert.equal($('sessionRetry').hidden, false);
}
{
  const calls = [];
  const {ctx, redirects} = adminContext(null, {api: async (path, opts) => {calls.push({path, opts});return {ok: true};}});
  vm.runInContext(logoutSource, ctx);
  await vm.runInContext('logoutAdmin()', ctx);
  assert.equal(calls[0].path, '/admin/logout');
  assert.equal(calls[0].opts.method, 'POST');
  assert.equal(ctx.ADMIN_CSRF, '');
  assert.equal(ctx.realKey, '');
  assert.equal(ctx.adminSessionReady, false);
  assert.deepEqual(redirects, ['/admin/login']);
}
{
  const prefix = section('const ADMIN_LOGIN_REQUIRED =', '/* 登录状态：');
  const $ = nodes();
  const replacements = [];
  const ctx = vm.createContext({
    document: {body: {dataset: {adminLoginRequired: 'true'}}}, $, URL, URLSearchParams,
    location: {href: 'https://console.example/admin?key=old-fixture&tab=pool#section', origin: 'https://console.example'},
    history: {replaceState: (_state, _title, path) => replacements.push(path)},
    localStorage: {getItem() {throw new Error('credential storage must not be read');}, removeItem() {throw new Error('storage can be unavailable');}},
  });
  vm.runInContext(prefix, ctx);
  assert.equal(vm.runInContext('KEY', ctx), '');
  assert.equal($('key').value, '');
  assert.deepEqual(replacements, ['/admin?tab=pool#section']);
}

function loginContext(response) {
  const $ = nodes();
  const calls = [];
  const redirects = [];
  const ctx = vm.createContext({
    document: {getElementById: $},
    location: {replace: path => redirects.push(path)},
    fetch: async (path, opts) => {calls.push({path, opts}); return typeof response === 'function' ? response() : response;},
  });
  vm.runInContext(loginSource, ctx);
  return {ctx, $, calls, redirects};
}
{
  const {ctx, $, calls, redirects} = loginContext({status: 200, ok: true, json: async () => ({ok: true})});
  $('password').value = 'fixture-password';
  await vm.runInContext('submitLogin({preventDefault(){}})', ctx);
  assert.equal(calls[0].path, '/admin/login');
  assert.equal(calls[0].opts.credentials, 'same-origin');
  assert.equal(calls[0].opts.method, 'POST');
  assert.equal(JSON.parse(calls[0].opts.body).password, 'fixture-password');
  assert.equal($('password').value, '');
  assert.deepEqual(redirects, ['/admin']);
  assert.equal($('loginButton').disabled, false);
  assert.equal($('loginForm').attributes['aria-busy'], undefined);
}
for (const [status, expected] of [[401, /密码不正确/], [429, /次数过多/], [503, /暂不可用/]]) {
  const {ctx, $, redirects} = loginContext({status, ok: false, json: async () => {throw new Error('Do not read or echo failure content');}});
  $('password').value = 'fixture-password';
  await vm.runInContext('submitLogin({preventDefault(){}})', ctx);
  assert.match($('loginError').textContent, expected);
  assert.equal($('password').value, '');
  assert.deepEqual(redirects, []);
}
{
  let finish;
  const {ctx, $, calls} = loginContext(() => new Promise(resolve => {finish = resolve;}));
  $('password').value = 'fixture-password';
  const pending = vm.runInContext('submitLogin({preventDefault(){}})', ctx);
  await vm.runInContext('submitLogin({preventDefault(){}})', ctx);
  assert.equal(calls.length, 1, 'double click must not submit twice');
  assert.equal($('password').disabled, true);
  finish({status: 401, ok: false});
  await pending;
  assert.equal($('password').disabled, false);
}
console.log('PASS admin login UI: session boot, cookie/CSRF, expiry/logout, no legacy credential reads, bounded password form');
