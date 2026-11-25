/**
 * 插件「账号密码登录 + 内外网自动切换」逻辑单测（Node，无需 Edge 扩展环境）。
 *
 *   node tests/endpoint_test.mjs
 *
 * 用 data: URL 动态加载 shared.js，mock fetch 与 chrome.storage.local，
 * 验证登录换会话令牌、会话失效自动重登、主备地址探测与路径基址保留。
 */
import fs from 'node:fs';
import path from 'node:path';
import { webcrypto } from 'node:crypto';
import { fileURLToPath } from 'node:url';

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const src = fs.readFileSync(path.join(ROOT, 'extension', 'shared.js'), 'utf8');

if (!globalThis.crypto) globalThis.crypto = webcrypto;
if (!globalThis.navigator) globalThis.navigator = { userAgent: 'Node.js' };

const storage = new Map();
globalThis.chrome = {
  storage: {
    local: {
      get: async (keys) => {
        const out = {};
        const list = Array.isArray(keys) ? keys : Object.keys(keys);
        for (const k of list) if (storage.has(k)) out[k] = storage.get(k);
        return out;
      },
      set: async (patch) => { for (const [k, v] of Object.entries(patch)) storage.set(k, v); },
      remove: async (keys) => { for (const k of (Array.isArray(keys) ? keys : [keys])) storage.delete(k); }
    }
  }
};

let reachable = new Set();
let calls = [];
let wrongPassword = false;
let expireOnce = false;
let gatewayReject = false;
const GOOD_TOKEN = 'session-token-good';

function resp(status, data, text = null) {
  return {
    ok: status >= 200 && status < 300, status,
    json: async () => data,
    text: async () => (text !== null ? text : JSON.stringify(data))
  };
}

globalThis.fetch = async (url, opt = {}) => {
  const u = String(url);
  calls.push({ url: u, opt });
  let origin;
  try { origin = new URL(u).origin; } catch { throw new Error('bad url'); }
  if (!reachable.has(origin)) throw new Error('Failed to fetch');

  if (u.includes('/api/v1/auth/login')) {
    const body = JSON.parse(opt.body || '{}');
    if (wrongPassword || body.password !== 'pw123456') {
      return resp(401, { ok: false, error: '用户名或密码错误' });
    }
    return resp(200, {
      ok: true, token: GOOD_TOKEN,
      expiresAt: Math.floor(Date.now() / 1000) + 3600,
      user: { username: body.username, role: body.username === 'admin' ? 'admin' : 'user' }
    });
  }
  if (u.includes('/api/v1/health')) {
    if (gatewayReject && u.includes('/app/edge-bookmarks/')) {
      return resp(200, null, 'invalid token');   // 飞牛网关未登录时返回 text/plain
    }
    return resp(200, { ok: true, app: 'edge-bookmarks', version: '1.4.0',
                       authMode: 'username-password', needsAdminSetup: false });
  }
  if (expireOnce && opt.headers && opt.headers.Authorization === 'Bearer ' + GOOD_TOKEN) {
    expireOnce = false;
    return resp(401, { ok: false, error: '会话已失效' });
  }
  return resp(200, { ok: true });
};

const mod = await import('data:text/javascript;base64,' + Buffer.from(src).toString('base64'));
const PASS = [], FAIL = [];
function check(name, cond, detail = '') {
  (cond ? PASS : FAIL).push(name);
  console.log((cond ? '  [OK]   ' : '  [FAIL] ') + name + (detail ? ' · ' + detail : ''));
}
function reset(reach) {
  storage.clear();
  calls = [];
  reachable = new Set(reach);
  wrongPassword = false;
  expireOnce = false;
  gatewayReject = false;
}
const cfg = (over = {}) => ({
  serverUrl: 'http://192.168.1.10:8765',
  serverUrlAlt: 'https://nas.example.com:8765',
  username: 'admin',
  password: 'pw123456',
  sessionToken: '', sessionUser: '', sessionExpiresAt: 0,
  ...over
});

console.log('\n— 配置与地址解析 —');
{
  check('isConfigured 需要地址+用户名+密码', mod.isConfigured(cfg()) === true);
  check('缺用户名视为未配置', mod.isConfigured(cfg({ username: '' })) === false);
  check('缺密码视为未配置', mod.isConfigured(cfg({ password: '' })) === false);
  check('缺地址视为未配置', mod.isConfigured(cfg({ serverUrl: '' })) === false);
  check('DEFAULTS 不再含 token 字段', !('token' in mod.DEFAULTS));
  check('DEFAULTS 含会话字段', 'sessionToken' in mod.DEFAULTS && 'sessionExpiresAt' in mod.DEFAULTS);
  const c = mod.endpointCandidates(cfg());
  check('主地址与备用地址都在候选里', c.length === 2 && c[0].base === 'http://192.168.1.10:8765');
  check('候选按顺序去重', mod.endpointCandidates(cfg({ serverUrlAlt: 'http://192.168.1.10:8765' })).length === 1);
  check('保留反代路径前缀',
    mod.normalizeServerUrl('https://nas.example.com:8443/eb-api/') === 'https://nas.example.com:8443/eb-api');
  check('非标端口不被丢弃',
    mod.normalizeServerUrl('https://nas.example.com:8443') === 'https://nas.example.com:8443');
  check('纯 origin 保持不变',
    mod.normalizeServerUrl('http://192.168.1.10:8765') === 'http://192.168.1.10:8765');
  check('缺少协议时自动补 http',
    mod.normalizeServerUrl('192.168.1.10:8765') === 'http://192.168.1.10:8765');
  check('网关地址能被识别为网关通道',
    mod.looksLikeGateway(cfg({ serverUrl: 'https://nas.example.com/app/edge-bookmarks' })) === true);
  check('反代地址不会被误判为网关',
    mod.looksLikeGateway(cfg({ serverUrl: 'https://nas.example.com:8443/eb-api' })) === false);
  check('通道标签可读', mod.endpointLabel('alternate').includes('备用'));
}

console.log('\n— 反向代理路径基址 —');
{
  const base = 'https://nas.example.com:8443/eb-api';
  reset(['https://nas.example.com:8443']);
  const ep = await mod.resolveEndpoint(cfg({ serverUrl: base, serverUrlAlt: '' }), true);
  check('反代地址可作为唯一端点命中', ep.kind === 'primary' && ep.base === base, ep.kind);
  check('health 请求带完整反代路径', calls.some(c => c.url === base + '/api/v1/health'));
}
{
  const base = 'https://nas.example.com:8443/eb-api';
  reset([]);
  const ep = await mod.resolveEndpoint(cfg({ serverUrl: base, serverUrlAlt: '' }), true);
  check('全不可达时返回逐地址失败明细',
    ep.kind === 'unreachable' && ep.failures.length === 1 &&
    ep.failures[0].includes(base + '/api/v1/health'));
}
{
  reset([]);
  let msg = '';
  try {
    await mod.healthCheck(cfg({ serverUrl: 'https://nas.example.com/app/edge-bookmarks', serverUrlAlt: '' }));
  } catch (e) { msg = e.message; }
  check('错误信息列出实际探测地址', msg.includes('/app/edge-bookmarks/api/v1/health'));
  check('误填网关时给出针对性提示', msg.includes('统一网关'));
}
{
  reset([]);
  let msg = '';
  try {
    await mod.healthCheck(cfg({ serverUrl: 'https://nas.example.com:8443/eb-api', serverUrlAlt: '' }));
  } catch (e) { msg = e.message; }
  check('反代失败时提示检查反代/防火墙', msg.includes('8765 已放行') || msg.includes('反向代理'));
}

console.log('\n— 账号密码登录 —');
{
  reset(['http://192.168.1.10:8765']);
  const h = await mod.healthCheck(cfg());
  check('测试连接会实际登录', calls.some(c => c.url.endsWith('/api/v1/auth/login')));
  check('登录请求体含用户名密码', (() => {
    const login = calls.find(c => c.url.endsWith('/api/v1/auth/login'));
    const b = JSON.parse(login.opt.body);
    return b.username === 'admin' && b.password === 'pw123456';
  })());
  check('返回登录用户信息', h.user && h.user.username === 'admin');
  check('会话令牌写入 storage', storage.get('sessionToken') === GOOD_TOKEN);
  check('登录账号写入 storage', storage.get('sessionUser') === 'admin');
  check('会话过期时间已记录', Number(storage.get('sessionExpiresAt')) > Date.now());
  check('返回命中通道', h.channel === 'primary' && h.usedBase === 'http://192.168.1.10:8765');
}
{
  reset(['http://192.168.1.10:8765']);
  wrongPassword = true;
  let msg = '';
  try { await mod.healthCheck(cfg()); } catch (e) { msg = e.message; }
  check('密码错误给出明确提示', msg.includes('用户名或密码错误'));
  check('密码错误不写入会话', !storage.has('sessionToken'));
}
{
  reset(['http://192.168.1.10:8765']);
  let msg = '';
  try { await mod.healthCheck(cfg({ username: '', password: '' })); } catch (e) { msg = e.message; }
  check('未填账号密码时提示补齐', msg.includes('用户名和密码'));
}

console.log('\n— 自动识别内外网 —');
{
  reset(['http://192.168.1.10:8765', 'https://nas.example.com:8765']);
  const ep = await mod.resolveEndpoint(cfg(), true);
  check('内网可达时选主地址', ep.kind === 'primary' && ep.base === 'http://192.168.1.10:8765', ep.kind);
  check('结果写入端点缓存', storage.get('activeEndpoint') === ep.base);
}
{
  reset(['https://nas.example.com:8765']);
  const ep = await mod.resolveEndpoint(cfg(), true);
  check('主地址不可达时自动切到备用地址',
    ep.kind === 'alternate' && ep.base === 'https://nas.example.com:8765', ep.kind);
  check('备用地址被探测到', calls.some(c => c.url.startsWith('https://nas.example.com:8765/api/v1/health')));
}
{
  reset(['http://192.168.1.10:8765', 'https://nas.example.com:8765']);
  storage.set('activeEndpoint', 'http://192.168.1.10:8765');
  storage.set('activeAt', Date.now());
  storage.set('activeKind', 'primary');
  const before = calls.length;
  const ep = await mod.resolveEndpoint(cfg({ activeEndpoint: storage.get('activeEndpoint'),
                                             activeAt: storage.get('activeAt'),
                                             activeKind: 'primary' }));
  check('缓存未过期时直接复用', ep.isCache === true && calls.length === before);
}
{
  reset([]);
  const ep = await mod.resolveEndpoint(cfg(), true);
  check('全部不可达时返回 unreachable', ep.kind === 'unreachable');
  check('全不可达时清空缓存', !storage.has('activeEndpoint'));
}

console.log('\n— apiRequest 会话与重试 —');
{
  reset(['http://192.168.1.10:8765']);
  const r = await mod.apiRequest(cfg(), '/api/v1/sync', { method: 'POST', body: { clientId: 'x' } });
  check('自动登录后同步成功', r.ok === true);
  check('同步请求带 Bearer 会话令牌', calls.some(c => c.url.endsWith('/api/v1/sync') &&
    c.opt.headers.Authorization === 'Bearer ' + GOOD_TOKEN));
  const loginCount = () => calls.filter(c => c.url.endsWith('/api/v1/auth/login')).length;
  check('首次只登录一次', loginCount() === 1);
  // 只把登录写入 storage 的会话字段合并进配置，账号密码仍由 cfg() 提供
  const stored = await mod.getConfig();
  const cached = cfg({
    sessionToken: stored.sessionToken,
    sessionUser: stored.sessionUser,
    sessionExpiresAt: stored.sessionExpiresAt
  });
  await mod.apiRequest(cached, '/api/v1/incoming');
  check('有效会话复用，不再重复登录', loginCount() === 1, '登录次数 ' + loginCount());
}
{
  reset(['http://192.168.1.10:8765']);
  expireOnce = true;
  const r = await mod.apiRequest(cfg(), '/api/v1/sync', { method: 'POST', body: {} });
  check('会话失效时自动重登并重试成功', r.ok === true);
  check('重登后重新发起业务请求',
    calls.filter(c => c.url.endsWith('/api/v1/sync')).length === 2);
  check('重登触发第二次登录',
    calls.filter(c => c.url.endsWith('/api/v1/auth/login')).length === 2);
}
{
  // 探测阶段两个都通，但真实请求打到主地址时网络断开 → 应重探并改用备用
  reset(['http://192.168.1.10:8765', 'https://nas.example.com:8765']);
  const realFetch = globalThis.fetch;
  let firstSync = true;
  globalThis.fetch = async (url, opt) => {
    if (String(url).includes('/api/v1/sync') && firstSync) {
      firstSync = false;
      reachable = new Set(['https://nas.example.com:8765']);   // 模拟离开内网
      throw new Error('Failed to fetch');
    }
    return realFetch(url, opt);
  };
  const r = await mod.apiRequest(cfg(), '/api/v1/sync', { method: 'POST', body: { clientId: 'x' } });
  check('主地址失联后自动切换并重试成功', r.ok === true);
  check('重试打到了备用地址',
    calls.some(c => c.url.startsWith('https://nas.example.com:8765/api/v1/sync')));
  check('新端点已写回缓存', storage.get('activeEndpoint') === 'https://nas.example.com:8765');
  globalThis.fetch = realFetch;
}
{
  reset(['http://192.168.1.10:8765']);
  await mod.healthCheck(cfg());
  await mod.invalidateSession();
  check('invalidateSession 清空会话', storage.get('sessionToken') === '' &&
    Number(storage.get('sessionExpiresAt')) === 0);
}

console.log('\n— 书签树工具 —');
{
  const tree = [{ id: '0', title: '', children: [
    { id: '1', title: '收藏夹栏', children: [
      { id: '11', title: '开发', children: [
        { id: '111', title: 'GitHub', url: 'https://github.com/' }
      ] }
    ] }
  ] }];
  const flat = mod.flattenBookmarks(tree);
  check('拍平书签计数正确', mod.countBookmarks(tree) === 1 && flat.length === 1);
  check('保留多级文件夹路径',
    JSON.stringify(flat[0].path) === JSON.stringify(['收藏夹栏', '开发']),
    JSON.stringify(flat[0].path));
}

console.log('\n— 飞牛统一网关通道与登录态复用（v1.3.1） —');
{
  reset(['https://nas.example.com:9443', 'http://192.168.1.10:8765']);
  const ep = await mod.resolveEndpoint(cfg({
    serverUrl: 'https://nas.example.com:9443/app/edge-bookmarks',
    serverUrlAlt: 'http://192.168.1.10:8765'
  }), true);
  check('统一网关地址可作为主地址命中',
    ep.kind === 'primary' && ep.base === 'https://nas.example.com:9443/app/edge-bookmarks', ep.kind);
  check('health 探测携带 credentials（复用飞牛会话）',
    calls.some(c => c.url.includes('/api/v1/health') && c.opt.credentials === 'include'));
  check('网关路径在请求 URL 中完整保留',
    calls.some(c => c.url === 'https://nas.example.com:9443/app/edge-bookmarks/api/v1/health'));
}
{
  reset(['https://nas.example.com:9443', 'http://192.168.1.10:8765']);
  gatewayReject = true;
  const ep = await mod.resolveEndpoint(cfg({
    serverUrl: 'https://nas.example.com:9443/app/edge-bookmarks',
    serverUrlAlt: 'http://192.168.1.10:8765'
  }), true);
  check('网关返回 invalid token 时自动降级到局域网直连',
    ep.kind === 'alternate' && ep.base === 'http://192.168.1.10:8765', ep.kind);
  gatewayReject = false;
}
{
  reset(['http://192.168.1.10:8765']);
  await mod.apiRequest(cfg(), '/api/v1/status');
  check('业务请求也携带 credentials',
    calls.some(c => c.url.includes('/api/v1/status') && c.opt.credentials === 'include'));
  check('登录请求也携带 credentials',
    calls.some(c => c.url.includes('/api/v1/auth/login') && c.opt.credentials === 'include'));
}
{
  check('附加候选里的网关地址也能被识别',
    mod.looksLikeGateway(cfg({
      serverUrls: ['https://nas.example.com:9443/app/edge-bookmarks']
    })) === true);
}

console.log('\n===== 结果：%d 通过 / %d 失败 =====', PASS.length, FAIL.length);
if (FAIL.length) {
  for (const f of FAIL) console.log('  失败：' + f);
  process.exit(1);
}
