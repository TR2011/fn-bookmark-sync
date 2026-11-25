#!/usr/bin/env node
/* v1.4.0 插件配置一键导入 / 多候选地址自动探测专项测试。
 *
 *   node tests/config_import_test.mjs
 *
 * 与 endpoint_test.mjs 互补：本文件只覆盖「管理端复制的配置」相关逻辑，
 * 不改动原有端点测试。
 */
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';

if (!globalThis.crypto) globalThis.crypto = webcrypto;
if (!globalThis.navigator || !globalThis.navigator.userAgent) {
  Object.defineProperty(globalThis, 'navigator',
    { value: { userAgent: 'Mozilla/5.0 Edg/131.0' }, configurable: true });
}

const store = new Map();
globalThis.chrome = {
  storage: {
    local: {
      async get(keys) {
        const out = {};
        for (const k of (Array.isArray(keys) ? keys : Object.keys(keys))) {
          if (store.has(k)) out[k] = store.get(k);
        }
        return out;
      },
      async set(patch) { for (const [k, v] of Object.entries(patch)) store.set(k, v); },
      async remove(keys) { for (const k of (Array.isArray(keys) ? keys : [keys])) store.delete(k); }
    }
  }
};

const PASS = [];
const FAIL = [];
function check(name, fn) {
  try {
    fn();
    PASS.push(name);
    console.log('  [OK]   ' + name);
  } catch (e) {
    FAIL.push(name);
    console.log('  [FAIL] ' + name + ' · ' + e.message);
  }
}
function throwsWith(fn, fragment) {
  assert.throws(fn, e => String(e.message).includes(fragment));
}

const mod = await import('../extension/shared.js');
const {
  DEFAULTS, parsePluginConfig, endpointCandidates, endpointLabel,
  resolveEndpoint, normalizeServerUrl, detectBrowser, defaultDeviceName
} = mod;

console.log('\n— 浏览器品牌识别（v1.4.0 品牌中立） —');

const UA_EDGE = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.2903.86';
const UA_CHROME = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';
const UA_OPERA = UA_CHROME + ' OPR/116.0.0.0';
const UA_VIVALDI = UA_CHROME.replace('131.0.0.0', '130.0.0.0') + ' Vivaldi/7.1.3570.42';

check('Edge UA 识别为 Microsoft Edge 131', () => {
  assert.equal(detectBrowser(UA_EDGE).label, 'Microsoft Edge 131');
});
check('Edge UA 不会被误判成 Chrome（自有标识优先）', () => {
  assert.equal(detectBrowser(UA_EDGE).brand, 'Microsoft Edge');
});
check('Chrome UA 识别为 Google Chrome 131', () => {
  assert.equal(detectBrowser(UA_CHROME).label, 'Google Chrome 131');
});
check('Opera UA 识别为 Opera 116', () => {
  assert.equal(detectBrowser(UA_OPERA).brand, 'Opera');
  assert.equal(detectBrowser(UA_OPERA).version, '116');
});
check('Vivaldi UA 识别为 Vivaldi 7', () => {
  assert.equal(detectBrowser(UA_VIVALDI).brand, 'Vivaldi');
  assert.equal(detectBrowser(UA_VIVALDI).version, '7');
});
check('360 浏览器 UA 识别', () => {
  assert.equal(detectBrowser(UA_CHROME + ' QIHU 360SE').brand, '360 浏览器');
});
check('QQ 浏览器 UA 识别', () => {
  assert.equal(detectBrowser(UA_CHROME.replace('131.0.0.0', '109.0.0.0') + ' QQBrowser/11.8').brand, 'QQ 浏览器');
});
check('未知 UA 兜底为 Chromium 浏览器', () => {
  assert.equal(detectBrowser('').brand, 'Chromium 浏览器');
  assert.equal(detectBrowser('Node.js').label, 'Chromium 浏览器');
});
check('默认设备名由品牌识别拼接（不再写死 Edge）', () => {
  const name = defaultDeviceName();
  assert.ok(name.includes(' · '), name);
  assert.ok(!name.startsWith('Edge · '), name);
  // 前半段必须是 detectBrowser 的 brand，而不是硬编码
  assert.ok(name.startsWith(detectBrowser(navigator.userAgent || '').brand + ' · '), name);
});

console.log('\n— 配置解析（管理端复制的 JSON） —');

check('DEFAULTS 含 serverUrls 数组', () => {
  assert.ok(Array.isArray(DEFAULTS.serverUrls));
});

const full = JSON.stringify({
  serverUrl: 'http://192.168.1.10:8765',
  serverUrlAlt: 'https://nas.example.com:8443/eb-api',
  serverUrls: ['http://nas.example.com:8765', 'https://nas.example.com:8443/eb-api'],
  username: 'admin'
});
check('解析完整配置并规范化', () => {
  const p = parsePluginConfig(full);
  assert.equal(p.serverUrl, 'http://192.168.1.10:8765');
  assert.equal(p.serverUrlAlt, 'https://nas.example.com:8443/eb-api');
  assert.equal(p.username, 'admin');
});
check('反代路径前缀被保留（不被截成 origin）', () => {
  const p = parsePluginConfig(full);
  assert.ok(p.serverUrlAlt.endsWith('/eb-api'));
  assert.equal(normalizeServerUrl('https://nas.example.com:8443/eb-api'),
               'https://nas.example.com:8443/eb-api');
});
check('重复候选地址被去掉', () => {
  const p = parsePluginConfig(full);
  const all = [p.serverUrl, p.serverUrlAlt, ...p.serverUrls];
  assert.equal(new Set(all).size, all.length);
  assert.equal(all.length, 3);
});
check('只有主地址时没有备用地址', () => {
  const p = parsePluginConfig('{"serverUrl":"http://10.0.0.2:8765","username":"u1"}');
  assert.equal(p.serverUrl, 'http://10.0.0.2:8765');
  assert.equal(p.serverUrlAlt, '');
  assert.deepEqual(p.serverUrls, []);
});
check('缺少协议时自动补 http', () => {
  const p = parsePluginConfig('{"serverUrl":"192.168.1.8:8765"}');
  assert.equal(p.serverUrl, 'http://192.168.1.8:8765');
});
check('去掉地址末尾斜杠', () => {
  assert.equal(normalizeServerUrl('https://nas.example.com:8443/eb-api/'),
               'https://nas.example.com:8443/eb-api');
});
check('容错：配置前后带说明文字', () => {
  const p = parsePluginConfig('插件配置如下：\n' + full + '\n请粘贴');
  assert.equal(p.serverUrl, 'http://192.168.1.10:8765');
});
check('空内容给出明确提示', () => {
  throwsWith(() => parsePluginConfig(''), '请先粘贴');
});
check('非 JSON 给出明确提示', () => {
  throwsWith(() => parsePluginConfig('http://192.168.1.10:8765'), '不是 JSON');
});
check('缺 serverUrl 给出明确提示', () => {
  throwsWith(() => parsePluginConfig('{"username":"admin"}'), 'serverUrl');
});
check('坏 JSON 给出明确提示', () => {
  throwsWith(() => parsePluginConfig('{serverUrl: }'), '解析失败');
});
check('配置解析结果不含密码字段', () => {
  const p = parsePluginConfig(full);
  assert.equal('password' in p, false);
});

console.log('\n— 多候选地址自动探测 —');

const cfg = {
  serverUrl: 'http://192.168.1.10:8765',
  serverUrlAlt: 'http://nas.example.com:8765',
  serverUrls: ['https://nas.example.com:8443/eb-api', 'http://172.16.0.2:8765'],
  activeEndpoint: '', activeAt: 0, activeKind: ''
};
check('endpointCandidates 包含附加候选', () => {
  const list = endpointCandidates(cfg);
  assert.equal(list.length, 4);
  assert.deepEqual(list.map(x => x.kind), ['primary', 'alternate', 'extra', 'extra']);
});
check('候选地址去重', () => {
  const list = endpointCandidates({ ...cfg,
    serverUrlAlt: cfg.serverUrl,
    serverUrls: [cfg.serverUrl, cfg.serverUrls[0], 'http://10.0.0.99:8765'] });
  assert.equal(list.length, 3);
});
check('附加候选地址标签可读', () => {
  assert.equal(endpointLabel('extra'), '配置中的附加候选地址');
});
check('最多只取 8 个附加候选', () => {
  const many = Array.from({ length: 20 }, (_, i) => 'http://10.0.0.' + (i + 1) + ':8765');
  assert.equal(endpointCandidates({ ...cfg, serverUrls: many }).length, 10);
});

const originalFetch = globalThis.fetch;
globalThis.fetch = async (url) => {
  if (String(url).includes('/eb-api')) {
    return new Response(JSON.stringify({ ok: true, app: 'edge-bookmarks', version: '1.4.0' }),
                        { status: 200, headers: { 'Content-Type': 'application/json' } });
  }
  throw new Error('network down');
};
// 异步用例单独执行
try {
  const ep = await resolveEndpoint(cfg, true);
  assert.equal(ep.base, 'https://nas.example.com:8443/eb-api');
  assert.equal(ep.kind, 'extra');
  PASS.push('主/备不可达时自动命中反代候选地址');
  console.log('  [OK]   主/备不可达时自动命中反代候选地址');
} catch (e) {
  FAIL.push('主/备不可达时自动命中反代候选地址');
  console.log('  [FAIL] 主/备不可达时自动命中反代候选地址 · ' + e.message);
}
try {
  assert.equal(store.get('activeEndpoint'), 'https://nas.example.com:8443/eb-api');
  PASS.push('命中地址写入缓存');
  console.log('  [OK]   命中地址写入缓存');
} catch (e) {
  FAIL.push('命中地址写入缓存');
  console.log('  [FAIL] 命中地址写入缓存 · ' + e.message);
}
globalThis.fetch = originalFetch;

console.log('\n===== 结果：%d 通过 / %d 失败 =====', PASS.length, FAIL.length);
if (FAIL.length) {
  for (const f of FAIL) console.log('  失败：' + f);
  process.exit(1);
}
