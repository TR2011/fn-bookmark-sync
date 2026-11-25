import {
  getConfig, normalizeServerUrl, endpointLabel,
  countBookmarks, getSyncTree
} from './shared.js';

const $ = id => document.getElementById(id);
document.getElementById('version').textContent = 'v' + chrome.runtime.getManifest().version;

let currentConfig = null;
let snapshotsLoaded = false;
let devicesLoaded = false;
let pullDevice = '';       // '' = 全部设备

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}

function fmt(iso) {
  if (!iso) return '尚未同步';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const p = n => (n < 10 ? '0' : '') + n;
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  return (sameDay ? '今天 ' : (d.getMonth() + 1) + '/' + d.getDate() + ' ') +
    p(d.getHours()) + ':' + p(d.getMinutes());
}

function send(msg) {
  return new Promise(resolve => {
    chrome.runtime.sendMessage(msg, r => resolve(r || { ok: false, error: '无响应' }));
  });
}

function setState(kind, text) {
  const el = $('stateBadge');
  el.className = 'state ' + kind;
  $('stateText').textContent = text;
}

async function loadSnapshots(force) {
  if (snapshotsLoaded && !force) return;
  const sel = $('snapSelect');
  const r = await send({ type: 'LIST_SNAPSHOTS' });
  if (!r.ok) {
    sel.innerHTML = '<option value="">历史备份不可用</option>';
    $('restoreBtn').disabled = true;
    return;
  }
  snapshotsLoaded = true;
  const items = r.items || [];
  sel.innerHTML = items.length
    ? items.map(s => '<option value="' + s.id + '">' +
        fmt(s.created_at) + ' · ' + s.count + ' 条</option>').join('')
    : '<option value="">暂无历史备份</option>';
  $('restoreBtn').disabled = !items.length;
}

/** 拉取设备标签：只列本账号下的「其他」设备，chip 样式与管理端一致。 */
async function loadPullDevices(force) {
  if (devicesLoaded && !force) return;
  const box = $('pullDevices');
  const r = await send({ type: 'LIST_DEVICES' });
  if (!r.ok) {
    box.innerHTML = '<span class="chip muted-chip">设备列表不可用</span>';
    $('pullDeviceCount').textContent = '';
    return;
  }
  devicesLoaded = true;
  const items = r.items || [];
  $('pullDeviceCount').textContent = items.length ? items.length + ' 台可选' : '暂无其他设备';
  if (pullDevice && !items.some(d => d.clientId === pullDevice)) pullDevice = '';
  box.innerHTML =
    '<button type="button" class="chip' + (pullDevice === '' ? ' active' : '') +
      '" data-device="" data-name="全部">全部</button>' +
    items.map(d =>
      '<button type="button" class="chip' + (pullDevice === d.clientId ? ' active' : '') +
        '" data-device="' + esc(d.clientId) + '" data-name="' + esc(d.name) +
        '" title="' + esc(d.name) + ' · ' + esc(d.lastSync || '未同步') + '">' +
        esc(d.name) + ' <small>' + d.count + '</small></button>').join('');
}

async function render() {
  const res = await send({ type: 'GET_STATUS' });
  if (!res.ok) { setState('bad', '异常'); return; }
  const { status, configured, config } = res;
  currentConfig = config;
  $('userTag').textContent = status.sessionUser || (configured ? '未登录' : '未配置');
  $('setupHint').classList.toggle('hidden', configured);
  $('syncBtn').disabled = !configured;
  $('webBtn').disabled = !configured;
  $('pullBtn').disabled = !configured;
  $('lastSync').textContent = status.lastSync ? '上次 ' + fmt(status.lastSync) : '尚未同步';

  const ch = $('channelTag');
  if (status.activeEndpoint) {
    ch.textContent = (status.activeKind === 'alternate' ? '备用' : '主地址') +
      ' · ' + status.activeEndpoint.replace(/^https?:\/\//, '');
    ch.title = endpointLabel(status.activeKind) + ' · ' + status.activeEndpoint;
    ch.classList.remove('hidden');
  } else {
    ch.classList.add('hidden');
  }

  $('cloudCount').textContent = status.serverTotal != null ? status.serverTotal : '—';
  $('deviceCount').textContent = status.serverDevices != null ? status.serverDevices : '—';

  if (status.syncing) {
    setState('busy', '同步中…');
    $('syncBtn').textContent = '同步中…';
    $('syncBtn').disabled = true;
  } else {
    $('syncBtn').textContent = '立即同步';
    $('syncBtn').disabled = !configured;
    if (!configured) setState('idle', '未连接');
    else if (status.lastError) setState('bad', '同步失败');
    else if (status.lastSync) setState('good', '已同步');
    else setState('idle', '待首次同步');
  }

  const err = $('errorBox');
  err.classList.toggle('hidden', !status.lastError);
  err.textContent = status.lastError || '';
  $('okBox').classList.add('hidden');

  try {
    const tree = await getSyncTree(config.pullFolderId);
    $('localCount').textContent = countBookmarks(tree);
  } catch (e) {
    $('localCount').textContent = '—';
  }

  const hint = $('pullHint');
  if (status.lastPullCount) {
    hint.textContent = '上次拉取导入 ' + status.lastPullCount + ' 条（' + fmt(status.lastPullAt) + '）';
    hint.classList.remove('hidden');
  } else {
    hint.classList.add('hidden');
  }

  if (configured) {
    loadSnapshots(false);
    loadPullDevices(false);
  }
}

function showOk(text) {
  const el = $('okBox');
  el.textContent = text;
  el.classList.remove('hidden');
  $('errorBox').classList.add('hidden');
}

function showError(text) {
  const el = $('errorBox');
  el.textContent = text;
  el.classList.remove('hidden');
  $('okBox').classList.add('hidden');
}

$('syncBtn').addEventListener('click', async () => {
  setState('busy', '同步中…');
  $('syncBtn').disabled = true;
  $('syncBtn').textContent = '同步中…';
  $('okBox').classList.add('hidden');
  const r = await send({ type: 'SYNC_NOW', reason: 'manual' });
  if (r.ok === false) {
    await render();
    showError(r.error);
    return;
  }
  if (r.skipped) return;
  await loadSnapshots(true);     // 刚同步会产生一份新快照
  await loadPullDevices(true);   // 云端设备列表可能新增
  await render();
  showOk('同步完成 · ' + r.bookmarkCount + ' 条书签已上传' + (r.pulled ? '，导入 ' + r.pulled + ' 条' : ''));
  setState('good', '已同步');
});

$('restoreBtn').addEventListener('click', async () => {
  const id = $('snapSelect').value;
  if (!id) { showError('请先选择一份历史备份'); return; }
  $('restoreBtn').disabled = true;
  $('restoreBtn').textContent = '还原中…';
  const r = await send({ type: 'RESTORE_SNAPSHOT', id: Number(id) });
  $('restoreBtn').disabled = false;
  $('restoreBtn').textContent = '一键拉回';
  if (!r.ok) return showError('还原失败：' + r.error);
  await render();
  showOk(r.imported
    ? '已还原 ' + r.imported + ' 条到「' + r.folder + '」' + (r.skipped ? '，跳过 ' + r.skipped + ' 条已存在' : '')
    : '这份备份里的网址都已存在，无需导入');
});

$('pullDevices').addEventListener('click', e => {
  const b = e.target.closest('.chip[data-device]');
  if (!b) return;
  pullDevice = b.dataset.device || '';
  $('pullDevices').querySelectorAll('.chip').forEach(x =>
    x.classList.toggle('active', (x.dataset.device || '') === pullDevice));
});

$('pullBtn').addEventListener('click', async () => {
  const active = $('pullDevices').querySelector('.chip.active');
  const label = pullDevice
    ? (active ? (active.dataset.name || '所选设备') : '所选设备')
    : '全部设备';
  $('pullBtn').disabled = true;
  $('pullBtn').textContent = '拉取中…';
  const r = await send({ type: 'PULL_NOW', clientId: pullDevice });
  $('pullBtn').disabled = false;
  $('pullBtn').textContent = '拉取所选设备';
  if (!r.ok) return showError(r.error);
  await render();
  showOk(r.pulled
    ? '已从「' + label + '」导入 ' + r.pulled + ' 条书签'
    : '「' + label + '」没有新书签需要导入');
});

function openOptions() { chrome.runtime.openOptionsPage(); }
$('settingsBtn').addEventListener('click', openOptions);
$('optionsLink').addEventListener('click', openOptions);
$('setupBtn').addEventListener('click', openOptions);

$('webBtn').addEventListener('click', async () => {
  const cfg = currentConfig || await getConfig();
  const url = normalizeServerUrl(cfg.activeEndpoint || cfg.serverUrl);
  if (url) chrome.tabs.create({ url });
});

document.addEventListener('visibilitychange', () => {
  if (!document.hidden) render();
});

render();
