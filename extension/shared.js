/* 公共配置、账号登录、网络请求与书签工具 —— service worker / popup / options 共用 */

export const DEFAULTS = {
  serverUrl: '',        // 主地址：通常是 NAS 内网 IP + 8765
  serverUrlAlt: '',     // 备用地址：外网 DDNS / 端口映射 / 域名 + 端口
  serverUrls: [],       // v1.2.0：管理端复制的配置里附带的其余候选地址
  username: '',         // 本应用在飞牛上注册的用户名
  password: '',         // 该用户的密码（仅保存在本机浏览器）
  sessionToken: '',     // 登录后由服务端签发的会话令牌
  sessionUser: '',      // 当前登录成功的用户名（用于界面展示）
  sessionExpiresAt: 0,  // 会话过期时间（毫秒时间戳）
  deviceName: '',
  autoSync: true,
  intervalMinutes: 15,
  syncOnChange: true,
  pullEnabled: false,
  pullFolderId: '',
  clientId: '',
  activeEndpoint: '',   // 运行时缓存：当前探测到的可用基址（含反代路径）
  activeAt: 0,
  activeKind: ''        // primary / alternate / cached
};

export const PULL_FOLDER_NAME = '来自飞牛 NAS';
export const ENDPOINT_TTL = 5 * 60 * 1000;   // 端点缓存 5 分钟
const SESSION_MARGIN = 60 * 1000;            // 会话剩余不足 1 分钟即提前续期

/**
 * 从 User-Agent 识别 Chromium 系浏览器品牌与主版本号。
 * 只用于「我的设备」列表里区分设备，不参与任何功能判断或鉴权。
 *
 * 匹配顺序很关键：Chromium 分支的 UA 普遍同时含 Chrome/<ver> 和自有标识
 * （Edge 的 Edg/、Opera 的 OPR/、Vivaldi 的 Vivaldi/ 等），必须先匹配自有
 * 标识，否则一律会被认成 Chrome。Brave 近年不再在 UA 暴露品牌，会显示成
 * Chrome，属已知限制，可在设置页自行改设备名。
 */
export function detectBrowser(ua) {
  const s = String(ua || '');
  const rules = [
    ['Microsoft Edge',   /EdgA?\/([\d.]+)/],
    ['Opera',            /OPR\/([\d.]+)|Opera\/([\d.]+)/],
    ['Vivaldi',          /Vivaldi\/([\d.]+)/],
    ['Brave',            /Brave\/([\d.]+)/],
    ['Samsung Internet', /SamsungBrowser\/([\d.]+)/],
    ['夸克浏览器',        /Quark\/([\d.]+)/],
    ['UC 浏览器',         /UBrowser\/([\d.]+)/],
    ['QQ 浏览器',         /QQBrowser\/([\d.]+)/],
    ['360 浏览器',        /QIHU|360SE|360EE/],
    ['搜狗浏览器',        /MetaSr|SogouExplorer|SE\s+([\d.]+)/],
    ['Google Chrome',    /Chrome\/([\d.]+)/],
    ['Chromium',         /Chromium\/([\d.]+)/]
  ];
  for (const [brand, re] of rules) {
    const m = re.exec(s);
    if (!m) continue;
    const raw = m[1] || m[2] || '';
    const version = raw ? raw.split('.')[0] : '';
    return { brand, version, label: version ? brand + ' ' + version : brand };
  }
  // 能加载 MV3 扩展本身就说明是 Chromium 内核，给一个中性兜底
  return { brand: 'Chromium 浏览器', version: '', label: 'Chromium 浏览器' };
}

export function defaultDeviceName() {
  const ua = navigator.userAgent || '';
  let os = '未知设备';
  if (ua.includes('Windows')) os = 'Windows';
  else if (ua.includes('Mac OS')) os = 'macOS';
  else if (ua.includes('Linux')) os = 'Linux';
  return detectBrowser(ua).brand + ' · ' + os;
}

export async function getConfig() {
  const cfg = await chrome.storage.local.get(Object.keys(DEFAULTS));
  const merged = { ...DEFAULTS, ...cfg };
  if (!merged.clientId) {
    merged.clientId = crypto.randomUUID();
    await chrome.storage.local.set({ clientId: merged.clientId });
  }
  if (!merged.deviceName) merged.deviceName = defaultDeviceName();
  return merged;
}

export async function saveConfig(patch) {
  await chrome.storage.local.set(patch);
  return getConfig();
}

export function normalizeServerUrl(raw) {
  let s = String(raw || '').trim();
  if (!s) return '';
  if (!/^https?:\/\//i.test(s)) s = 'http://' + s;
  let u;
  try {
    u = new URL(s);
  } catch (e) {
    return '';
  }
  // 保留路径前缀：自建反代时插件基址是 https://<统一登录地址>:<端口>/eb-api，
  // 而不是 origin。旧版本用 u.origin 会把路径丢掉，导致反代永远连不上。
  const p = u.pathname.replace(/\/+$/, '');
  return u.origin + (p && p !== '/' ? p : '');
}

/** 地址是否经过飞牛统一网关（/app/...）。网关通道需要浏览器已登录 NAS。 */
export function looksLikeGateway(cfg) {
  return [cfg && cfg.serverUrl, cfg && cfg.serverUrlAlt,
          ...(Array.isArray(cfg && cfg.serverUrls) ? cfg.serverUrls : [])]
    .some(u => /\/app\//i.test(String(u || '')));
}

export function isConfigured(cfg) {
  return !!(normalizeServerUrl(cfg.serverUrl) &&
            String(cfg.username || '').trim() &&
            String(cfg.password || ''));
}

/**
 * 解析管理端「复制插件配置」生成的 JSON。
 * 只接受地址与用户名，不读取密码（配置里本来也不含密码）。
 * 返回可直接传给 saveConfig / SAVE_CONFIG 的补丁对象。
 */
export function parsePluginConfig(raw) {
  let text = String(raw || '').trim();
  if (!text) throw new Error('请先粘贴从飞牛管理界面复制的插件配置');
  const first = text.indexOf('{');
  const last = text.lastIndexOf('}');
  if (first < 0 || last <= first) throw new Error('内容不是 JSON 配置，请在管理界面点「复制插件配置」');
  let obj;
  try {
    obj = JSON.parse(text.slice(first, last + 1));
  } catch (e) {
    throw new Error('配置解析失败：请完整复制管理界面显示的配置内容');
  }
  const primary = normalizeServerUrl(obj.serverUrl);
  if (!primary) throw new Error('配置里缺少有效的 serverUrl，请重新复制');
  const seen = new Set([primary]);
  const extra = [];
  const add = (rawUrl) => {
    const u = normalizeServerUrl(rawUrl);
    if (u && !seen.has(u)) { seen.add(u); extra.push(u); }
  };
  add(obj.serverUrlAlt);
  for (const u of (Array.isArray(obj.serverUrls) ? obj.serverUrls : [])) add(u);
  const patch = {
    serverUrl: primary,
    serverUrlAlt: extra.length ? extra[0] : '',
    serverUrls: extra.slice(1)
  };
  if (obj.username) patch.username = String(obj.username).trim();
  if (obj.deviceName) patch.deviceName = String(obj.deviceName).trim();
  return patch;
}

/* ------------------------------------------------------------------ */
/* 内外网自动识别                                                      */
/* 主地址 / 备用地址 / 上次可用地址逐个探测 /api/v1/health，           */
/* 自动选用当前网络下可达的那个，请求失败时重新探测切换。              */
/* ------------------------------------------------------------------ */

export function endpointCandidates(cfg) {
  const out = [];
  const seen = new Set();
  const push = (raw, kind) => {
    const base = normalizeServerUrl(raw);
    if (base && !seen.has(base)) {
      seen.add(base);
      out.push({ base, kind });
    }
  };
  if (cfg.activeEndpoint) push(cfg.activeEndpoint, 'cached');
  push(cfg.serverUrl, 'primary');
  push(cfg.serverUrlAlt, 'alternate');
  // 管理端「复制插件配置」会附带全部候选地址；逐个探测，任一可达即可，
  // 用户完全不需要判断该填内网 IP、直连端口还是统一登录地址的反代路径。
  for (const raw of (Array.isArray(cfg.serverUrls) ? cfg.serverUrls : []).slice(0, 8)) {
    push(raw, 'extra');
  }
  return out;
}

export function endpointLabel(kind) {
  return {
    primary: '主地址（管理页当前地址）',
    alternate: '备用地址（局域网直连）',
    extra: '配置中的附加候选地址',
    unreachable: '全部地址不可达'
  }[kind] || '上次可用地址';
}

/**
 * 探测一个地址是否真的是本服务。
 * credentials:'include' 让飞牛统一网关（/app/...）复用浏览器里已登录的
 * NAS 会话 Cookie；若网关仍返回 "invalid token"（text/plain），这里会
 * 判为失败，resolveEndpoint 自动尝试下一个地址，不会卡死。
 */
async function pingEndpoint(base, timeout) {
  const url = base + '/api/v1/health';
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout || 3500);
  try {
    const r = await fetch(url, {
      signal: controller.signal, cache: 'no-store', credentials: 'include'
    });
    if (!r.ok) return { ok: false, reason: 'HTTP ' + r.status };
    let text = null;
    try {
      text = typeof r.text === 'function' ? await r.text() : null;
    } catch (e) { text = null; }
    let j;
    try {
      j = text !== null ? JSON.parse(text) : await r.json();
    } catch (e) {
      const why = /invalid token/i.test(String(text || ''))
        ? '飞牛统一网关要求先登录 NAS（invalid token）'
        : '返回非 JSON（多半是飞牛登录页、网关未登录或反代规则未生效）';
      return { ok: false, reason: why };
    }
    if (!j || j.ok !== true) return { ok: false, reason: 'health 响应缺少 ok:true' };
    return { ok: true, reason: '', health: j };
  } catch (e) {
    const why = e && e.name === 'AbortError'
      ? '连接超时（3.5 秒）'
      : ('网络错误：' + ((e && e.message) || '连接被拒绝'));
    return { ok: false, reason: why };
  } finally {
    clearTimeout(timer);
  }
}

async function persistEndpoint(base, kind) {
  try {
    await chrome.storage.local.set({
      activeEndpoint: base, activeAt: Date.now(), activeKind: kind
    });
  } catch (e) { /* 非扩展上下文（如本地脚本）忽略 */ }
}

export async function invalidateEndpoint() {
  try {
    await chrome.storage.local.remove(['activeEndpoint', 'activeAt', 'activeKind']);
  } catch (e) { /* ignore */ }
}

/** 返回 { base, kind, isCache }；全不可达时 kind='unreachable'。 */
export async function resolveEndpoint(cfg, force = false) {
  const candidates = endpointCandidates(cfg);
  if (!candidates.length) throw new Error('服务器地址未配置或格式不正确');

  const cached = normalizeServerUrl(cfg.activeEndpoint);
  if (!force && cached && Date.now() - Number(cfg.activeAt || 0) < ENDPOINT_TTL) {
    return { base: cached, kind: String(cfg.activeKind || 'primary'), isCache: true };
  }

  const failures = [];
  for (const c of candidates) {
    const r = await pingEndpoint(c.base);
    if (r.ok) {
      const kind = c.kind === 'cached' ? String(cfg.activeKind || 'primary') : c.kind;
      await persistEndpoint(c.base, kind);
      return { base: c.base, kind, isCache: false, health: r.health };
    }
    failures.push(c.base + '/api/v1/health → ' + r.reason);
  }

  await invalidateEndpoint();
  const fallback = candidates.find(c => c.kind !== 'cached') || candidates[0];
  return { base: fallback.base, kind: 'unreachable', isCache: false, failures };
}

/* ------------------------------------------------------------------ */
/* 账号密码登录                                                        */
/* 服务端签发会话令牌；令牌过期或服务重启后自动用账号密码重新登录。     */
/* ------------------------------------------------------------------ */

async function persistSession(data) {
  const patch = {
    sessionToken: data.token || '',
    sessionUser: (data.user && data.user.username) || '',
    sessionExpiresAt: Number(data.expiresAt || 0) * 1000
  };
  try {
    await chrome.storage.local.set(patch);
  } catch (e) { /* ignore */ }
  return patch;
}

export async function invalidateSession() {
  try {
    await chrome.storage.local.set({ sessionToken: '', sessionUser: '', sessionExpiresAt: 0 });
  } catch (e) { /* ignore */ }
}

export async function loginRequest(base, username, password, timeout) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout || 15000);
  let resp;
  try {
    resp = await fetch(base + '/api/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: String(username || '').trim(), password: String(password || '') }),
      signal: controller.signal,
      cache: 'no-store',
      credentials: 'include'
    });
  } catch (e) {
    throw new Error('无法连接 ' + base + '：' + (e.message || '网络错误'));
  } finally {
    clearTimeout(timer);
  }
  let data;
  try {
    data = await resp.json();
  } catch (e) {
    throw new Error('登录接口返回了非 JSON 内容（HTTP ' + resp.status +
                    '）：若使用飞牛统一网关地址，请先在当前浏览器中登录飞牛 NAS 再重试');
  }
  if (resp.status === 401) throw new Error('用户名或密码错误，请在插件设置页检查');
  if (!resp.ok || data.ok === false || !data.token) {
    throw new Error(data.error || ('登录失败（HTTP ' + resp.status + '）'));
  }
  await persistSession(data);
  try {
    await chrome.storage.local.set({ sessionUser: (data.user && data.user.username) || '' });
  } catch (e) { /* ignore */ }
  return data;
}

/** 取一个可用会话令牌：缓存有效则复用，否则用账号密码重新登录。 */
export async function getSession(cfg, force = false, baseOverride = '') {
  if (!force && cfg.sessionToken && Number(cfg.sessionExpiresAt || 0) > Date.now() + SESSION_MARGIN) {
    return cfg.sessionToken;
  }
  let base = baseOverride;
  if (!base) {
    const ep = await resolveEndpoint(cfg);
    if (ep.kind === 'unreachable') {
      const detail = (ep.failures || []).join('\n') || '未配置任何服务器地址';
      throw new Error('所有服务器地址均不可达：\n' + detail);
    }
    base = ep.base;
  }
  if (!String(cfg.username || '').trim() || !String(cfg.password || '')) {
    throw new Error('尚未配置用户名和密码，请先打开设置页');
  }
  const data = await loginRequest(base, cfg.username, cfg.password);
  return data.token;
}

function unreachableError(failures) {
  return '所有服务器地址均不可达：\n' + (failures || []).join('\n');
}

export async function apiRequest(cfg, path, options = {}) {
  if (!isConfigured(cfg)) {
    throw new Error('尚未配置服务器地址或用户名密码，请先打开设置页');
  }

  const ep = await resolveEndpoint(cfg);
  if (ep.kind === 'unreachable') throw new Error(unreachableError(ep.failures));
  const firstToken = await getSession(cfg, false, ep.base);

  const requestOnce = async (base, token) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), options.timeout || 20000);
    let resp;
    try {
      resp = await fetch(base + path, {
        method: options.method || 'GET',
        headers: {
          'Authorization': 'Bearer ' + token,
          'Content-Type': 'application/json',
          ...(options.headers || {})
        },
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
        signal: controller.signal,
        credentials: 'include'
      });
    } catch (e) {
      if (e.name === 'AbortError') {
        throw Object.assign(new Error('连接超时：' + base + '（检查 NAS 地址与网络）'),
                            { retryable: true });
      }
      throw Object.assign(new Error(
        '无法连接 ' + base + '：' + e.message +
        '（若使用 HTTPS 自签证书，请先在浏览器中信任该证书）'), { retryable: true });
    } finally {
      clearTimeout(timer);
    }

    let data;
    try {
      data = await resp.json();
    } catch (e) {
      throw new Error('服务器返回了非 JSON 内容（HTTP ' + resp.status + '），请确认端口是否正确');
    }
    if (resp.status === 401) throw Object.assign(new Error('会话已失效'), { authFailed: true });
    if (!resp.ok || data.ok === false) throw new Error(data.error || ('HTTP ' + resp.status));
    return data;
  };

  try {
    return await requestOnce(ep.base, firstToken);
  } catch (e) {
    if (e.authFailed) {
      // 服务端重启 / 会话过期 / 密码被重置：用保存的账号密码重新登录一次
      await invalidateSession();
      const fresh = await getSession(cfg, true, ep.base);
      return await requestOnce(ep.base, fresh);
    }
    if (!e.retryable) throw e;
    await invalidateEndpoint();
    const freshCfg = { ...cfg, activeEndpoint: '', activeAt: 0, activeKind: '' };
    const ep2 = await resolveEndpoint(freshCfg, true);
    if (ep2.kind === 'unreachable') throw new Error(unreachableError(ep2.failures));
    const token2 = await getSession(cfg, false, ep2.base);
    return await requestOnce(ep2.base, token2);
  }
}

/** 测试连接：探测地址 + 用账号密码实际登录一次。 */
export async function healthCheck(cfg) {
  const ep = await resolveEndpoint(cfg, true);
  if (ep.kind === 'unreachable') {
    const detail = (ep.failures || []).join('\n') || '未配置任何服务器地址';
    const hint = looksLikeGateway(cfg)
      ? '\n\n配置里包含飞牛统一网关地址（/app/...）：该通道要求浏览器已登录飞牛 NAS。请先在当前浏览器中打开并登录飞牛桌面，再点「测试连接」；若仍提示 invalid token，说明浏览器会话未带给插件，此时请用局域网直连 http://<NAS-IP>:8765，或在飞牛上把 8765 端口映射/反向代理到你的域名。'
      : '\n\n请确认：① NAS 已开机且应用显示「运行中」；② 主地址与当前管理页地址一致；③ 若开启防火墙，8765 已放行。';
    throw new Error('以下地址均探测失败：\n' + detail + hint);
  }
  if (!String(cfg.username || '').trim() || !String(cfg.password || '')) {
    throw new Error('请填写用户名和密码（与飞牛 Web 界面登录账号相同）');
  }
  const session = await loginRequest(ep.base, cfg.username, cfg.password);
  const j = ep.health || {};
  return { ...j, usedBase: ep.base, channel: ep.kind,
           channelLabel: endpointLabel(ep.kind), user: session.user || null };
}

export function flattenBookmarks(tree) {
  const out = [];
  function walk(nodes, path) {
    for (const n of nodes || []) {
      if (n.url) {
        out.push({ id: n.id, title: n.title || '', url: n.url, path: [...path] });
      } else {
        const next = n.title ? [...path, n.title] : path;
        walk(n.children || [], next);
      }
    }
  }
  walk(tree, []);
  return out;
}

export function countBookmarks(tree) {
  return flattenBookmarks(tree).length;
}

/** 获取整棵收藏夹树，并剔除「来自飞牛 NAS」回灌目录，避免自我循环 */
export async function getSyncTree(pullFolderId) {
  const tree = await chrome.bookmarks.getTree();
  if (!pullFolderId) return tree;
  function strip(nodes) {
    return (nodes || [])
      .filter(n => n.id !== pullFolderId)
      .map(n => ({ ...n, children: strip(n.children) }));
  }
  return strip(tree);
}

export async function getStatus() {
  const d = await chrome.storage.local.get([
    'lastSync', 'lastError', 'lastSyncCount', 'serverTotal',
    'serverDevices', 'syncing', 'lastPullCount', 'lastPullAt',
    'activeEndpoint', 'activeAt', 'activeKind',
    'sessionUser', 'sessionExpiresAt'
  ]);
  return d;
}

export async function setStatus(patch) {
  await chrome.storage.local.set(patch);
}
