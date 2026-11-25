import { getConfig, isConfigured, normalizeServerUrl, endpointLabel,
         parsePluginConfig } from './shared.js';

const $ = id => document.getElementById(id);
let cfg = null;

/* v1.3.1：同步策略独立保存。设置页原来只有「连接 NAS」卡片有保存按钮，
   折叠的同步策略卡片改完既没有入口、保存后也没重排定时任务，改动会丢失。 */
const STRATEGY_IDS = ['autoSync', 'interval', 'syncOnChange', 'pullEnabled'];
let strategyTouchedAt = 0;   // 防止页面启动时未完成的 refresh() 覆盖刚改动的策略

function strategyState(text, kind) {
  const el = $('strategyState');
  el.textContent = text;
  el.className = 'save-state' + (kind ? ' ' + kind : '');
}

function collectStrategy() {
  return {
    autoSync: $('autoSync').checked,
    intervalMinutes: Number($('interval').value) || 15,
    syncOnChange: $('syncOnChange').checked,
    pullEnabled: $('pullEnabled').checked
  };
}

async function saveStrategy(auto) {
  const data = collectStrategy();
  strategyState('正在保存…');
  const r = await send({ type: 'SAVE_STRATEGY', ...data });
  if (!r.ok) {
    strategyState('保存失败：' + r.error, 'bad');
    return false;
  }
  cfg = { ...(cfg || {}), ...data };
  strategyState(auto ? '✓ 已自动保存，定时计划已更新' : '✓ 策略已保存，定时计划已更新', 'ok');
  if (!auto) notice('同步策略已保存并立即生效');
  return true;
}

STRATEGY_IDS.forEach(id => {
  $(id).addEventListener('change', () => {
    strategyTouchedAt = Date.now();
    saveStrategy(true);
  });
});
$('saveStrategyBtn').addEventListener('click', () => saveStrategy(false));

function send(msg) {
  return new Promise(resolve => {
    chrome.runtime.sendMessage(msg, r => resolve(r || { ok: false, error: '无响应' }));
  });
}

function notice(text, bad) {
  const el = $('notice');
  el.textContent = text;
  el.className = 'notice' + (bad ? ' bad' : '') + (text ? '' : ' hidden');
  if (text) {
    clearTimeout(el._t);
    el._t = setTimeout(() => { el.className = 'notice hidden'; }, bad ? 20000 : 5000);
  }
}

function renderParsedInfo(c) {
  const box = $('parsedInfo');
  if (!c || !normalizeServerUrl(c.serverUrl)) {
    box.classList.add('hidden');
    return;
  }
  const urls = [c.serverUrl, c.serverUrlAlt, ...(Array.isArray(c.serverUrls) ? c.serverUrls : [])]
    .map(normalizeServerUrl).filter(Boolean);
  const unique = [...new Set(urls)];
  box.innerHTML = '<strong>当前配置</strong>' +
    '<span>账号：' + (c.username ? escapeHtml(c.username) : '未设置') + '</span>' +
    unique.map((u, i) => '<span>' + (i === 0 ? '主地址' : '候选 ' + i) + '：' +
      '<code>' + escapeHtml(u) + '</code></span>').join('');
  box.classList.remove('hidden');
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fill(c) {
  cfg = c;
  $('serverUrl').value = c.serverUrl || '';
  $('serverUrlAlt').value = c.serverUrlAlt || '';
  $('username').value = c.username || '';
  $('password').value = c.password || '';
  $('deviceName').value = c.deviceName || '';
  if (Date.now() - strategyTouchedAt > 3000) {
    $('autoSync').checked = !!c.autoSync;
    $('interval').value = String(c.intervalMinutes || 15);
    $('syncOnChange').checked = !!c.syncOnChange;
    $('pullEnabled').checked = !!c.pullEnabled;
  }
  $('webLink').href = normalizeServerUrl(c.activeEndpoint || c.serverUrl) || '#';
  renderParsedInfo(c);
}

function collect() {
  return {
    serverUrl: normalizeServerUrl($('serverUrl').value),
    serverUrlAlt: normalizeServerUrl($('serverUrlAlt').value),
    serverUrls: (cfg && Array.isArray(cfg.serverUrls)) ? cfg.serverUrls : [],
    username: $('username').value.trim(),
    password: $('password').value,
    deviceName: $('deviceName').value.trim(),
    autoSync: $('autoSync').checked,
    intervalMinutes: Number($('interval').value) || 15,
    syncOnChange: $('syncOnChange').checked,
    pullEnabled: $('pullEnabled').checked
  };
}

function fmt(iso) {
  if (!iso) return '从未';
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleString('zh-CN', { hour12: false });
}

async function refresh() {
  const r = await send({ type: 'GET_STATUS' });
  if (!r.ok) return;
  fill(r.config);
  const s = r.status;
  const stateText = !r.configured ? '未配置' : (s.lastError ? '同步失败' : (s.lastSync ? '正常' : '待首次同步'));
  $('iState').textContent = stateText;
  $('iStateFull').textContent = stateText;
  $('iUser').textContent = s.sessionUser || (r.configured ? '未登录' : '—');
  $('iChannel').textContent = s.activeEndpoint
    ? endpointLabel(s.activeKind) + ' · ' + s.activeEndpoint.replace(/^https?:\/\//, '')
    : (r.configured ? '待首次探测' : '—');
  $('iChannel').title = s.activeEndpoint || '';
  $('iLast').textContent = fmt(s.lastSync);
  $('iCount').textContent = s.lastSyncCount != null ? s.lastSyncCount + ' 条' : '—';
  $('iClient').textContent = (r.config.clientId || '').slice(0, 13) + '…';
  $('iClient').title = r.config.clientId || '';
  const e = $('iError');
  e.classList.toggle('hidden', !s.lastError);
  e.textContent = s.lastError || '';
}

$('togglePassword').addEventListener('click', () => {
  const el = $('password');
  el.type = el.type === 'password' ? 'text' : 'password';
  $('togglePassword').textContent = el.type === 'password' ? '显示' : '隐藏';
});

$('parseBtn').addEventListener('click', () => {
  try {
    const patch = parsePluginConfig($('pasteArea').value);
    cfg = { ...(cfg || {}), ...patch };
    $('serverUrl').value = patch.serverUrl;
    $('serverUrlAlt').value = patch.serverUrlAlt || '';
    if (patch.username) $('username').value = patch.username;
    if (patch.deviceName) $('deviceName').value = patch.deviceName;
    $('advancedBox').open = false;
    renderParsedInfo(cfg);
    const total = 1 + (patch.serverUrlAlt ? 1 : 0) + patch.serverUrls.length;
    notice('配置已解析：' + total + ' 个候选地址、账号 ' +
      (patch.username || '（配置未带账号，请在高级里填写）') +
      '。请输入密码后点击「保存设置」。');
  } catch (e) {
    notice(e.message, true);
  }
});

$('pasteArea').addEventListener('paste', () => {
  setTimeout(() => {
    const v = $('pasteArea').value;
    if (v.includes('{') && v.includes('}')) $('parseBtn').click();
  }, 0);
});

$('testBtn').addEventListener('click', async () => {
  const data = collect();
  $('testBtn').disabled = true;
  $('testBtn').textContent = '测试中…';
  const r = await send({ type: 'TEST_CONNECTION', config: data });
  $('testBtn').disabled = false;
  $('testBtn').textContent = '测试连接并登录';
  notice(r.ok
    ? '连接并登录成功 · ' + (r.channelLabel || '主地址') +
      (r.usedBase ? '（' + r.usedBase + '）' : '') +
      ' · 账号 ' + (r.user || data.username) + ' · 服务端 v' + (r.version || '')
    : '连接失败：\n' + r.error, !r.ok);
  await refresh();
});

async function save(thenSync) {
  const data = collect();
  if (!data.serverUrl) return notice('请先粘贴插件配置，或在「高级」里填写服务器地址', true);
  if (!data.username) return notice('请填写用户名', true);
  if (!data.password) return notice('请填写密码', true);
  const r = await send({ type: 'SAVE_CONFIG', config: data });
  if (!r.ok) return notice('保存失败：' + r.error, true);
  await refresh();
  if (!thenSync) return notice('设置已保存');
  $('syncBtn').disabled = true;
  $('syncBtn').textContent = '同步中…';
  const s = await send({ type: 'SYNC_NOW', reason: 'manual' });
  $('syncBtn').disabled = false;
  $('syncBtn').textContent = '保存并立即同步';
  await refresh();
  notice(s.ok ? '同步完成，已上传 ' + s.bookmarkCount + ' 条书签' : '同步失败：' + s.error, !s.ok);
}

$('saveBtn').addEventListener('click', () => save(false));
$('syncBtn').addEventListener('click', () => save(true));
$('refreshBtn').addEventListener('click', () => { refresh(); notice('已刷新'); });

$('pullBtn').addEventListener('click', async () => {
  if (!isConfigured(await getConfig())) return notice('请先保存连接配置', true);
  $('pullBtn').disabled = true;
  const r = await send({ type: 'PULL_NOW' });
  $('pullBtn').disabled = false;
  notice(r.ok ? '拉取完成，新增 ' + r.pulled + ' 条书签（云端共 ' + r.total + ' 条）' : '拉取失败：' + r.error, !r.ok);
});

$('resetBtn').addEventListener('click', async () => {
  if (!confirm('重置后本机将被视为一台新设备，确定继续？')) return;
  await send({ type: 'RESET_CLIENT_ID' });
  await refresh();
  notice('客户端 ID 已重置');
});

refresh();
