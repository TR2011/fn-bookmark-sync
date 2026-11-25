/* Service Worker：定时同步、变更监听、云端书签回灌 */

import {
  PULL_FOLDER_NAME, getConfig, saveConfig, normalizeServerUrl,
  isConfigured, apiRequest, healthCheck, invalidateEndpoint, invalidateSession,
  getSyncTree, flattenBookmarks, countBookmarks, setStatus, getStatus,
  detectBrowser
} from './shared.js';

const AUTO_ALARM = 'eb-auto-sync';
const CHANGE_ALARM = 'eb-change-sync';
const MAX_IMPORT_PER_RUN = 200;
const MAX_RESTORE_PER_RUN = 1500;

let suppressChanges = false;
let busy = false;

function browserVersion() {
  // v1.4.0：品牌中立，按真实 UA 识别 Edge / Chrome / Vivaldi / Opera 等
  return detectBrowser(navigator.userAgent || '').label;
}

async function reschedule() {
  await chrome.alarms.clear(AUTO_ALARM);
  const cfg = await getConfig();
  if (cfg.autoSync && isConfigured(cfg)) {
    const period = Math.max(1, Number(cfg.intervalMinutes) || 15);
    chrome.alarms.create(AUTO_ALARM, { delayInMinutes: 1, periodInMinutes: period });
  }
}

async function scheduleChangeSync() {
  if (suppressChanges) return;
  const cfg = await getConfig();
  if (!cfg.syncOnChange || !isConfigured(cfg)) return;
  chrome.alarms.create(CHANGE_ALARM, { delayInMinutes: 0.5 });
}

/* ------------------------------------------------------------------ */
/* 云端书签回灌                                                        */
/* ------------------------------------------------------------------ */

async function ensurePullFolder(cfg) {
  if (cfg.pullFolderId) {
    try {
      const found = await chrome.bookmarks.get(cfg.pullFolderId);
      if (found && found.length) return cfg.pullFolderId;
    } catch (e) { /* 目录已被用户删除，重建 */ }
  }
  const tree = await chrome.bookmarks.getTree();
  let existing = null;
  function find(nodes) {
    for (const n of nodes || []) {
      if (!n.url && n.title === PULL_FOLDER_NAME) { existing = n.id; return; }
      find(n.children);
      if (existing) return;
    }
  }
  find(tree);
  if (existing) {
    await saveConfig({ pullFolderId: existing });
    return existing;
  }
  let parentId = '2'; // 「其他收藏夹」
  try {
    const p = await chrome.bookmarks.get('2');
    if (!p || !p.length) parentId = '1';
  } catch (e) { parentId = '1'; }
  const folder = await chrome.bookmarks.create({ parentId, title: PULL_FOLDER_NAME });
  await saveConfig({ pullFolderId: folder.id });
  return folder.id;
}

async function applyIncoming(cfg, items, folderLabel) {
  if (!Array.isArray(items) || !items.length) return 0;
  const rootId = await ensurePullFolder(cfg);
  const localTree = await getSyncTree(cfg.pullFolderId);
  const known = new Set(flattenBookmarks(localTree).map(b => b.url));
  // 同时把回灌目录里已有的网址也算进去，避免重复导入
  try {
    const sub = await chrome.bookmarks.getSubTree(rootId);
    for (const b of flattenBookmarks(sub)) known.add(b.url);
  } catch (e) { /* ignore */ }

  const folderCache = new Map([['', rootId]]);
  let imported = 0;
  suppressChanges = true;
  try {
    for (const item of items) {
      if (imported >= MAX_IMPORT_PER_RUN) break;
      if (!item || !item.url || known.has(item.url)) continue;
      const segments = [folderLabel || item.device_name || item.client_id || '其他设备',
                        ...(item.path || [])]
        .map(s => String(s || '').trim()).filter(Boolean);
      let parentId = rootId;
      let key = '';
      for (const seg of segments) {
        key += '/' + seg;
        if (folderCache.has(key)) {
          parentId = folderCache.get(key);
        } else {
          try {
            const f = await chrome.bookmarks.create({ parentId, title: seg.slice(0, 200) });
            parentId = f.id;
            folderCache.set(key, parentId);
          } catch (e) {
            parentId = rootId;
            break;
          }
        }
      }
      try {
        await chrome.bookmarks.create({
          parentId,
          title: (item.title || item.url).slice(0, 300),
          url: item.url
        });
        known.add(item.url);
        imported++;
      } catch (e) { /* 单条失败不影响整体 */ }
    }
  } finally {
    suppressChanges = false;
  }
  if (imported) await setStatus({ lastPullCount: imported, lastPullAt: new Date().toISOString() });
  return imported;
}

/* ------------------------------------------------------------------ */
/* 本机历史备份还原（v1.3.0）                                          */
/* 把本设备以往上传到 NAS 的快照整棵还原到浏览器，跳过已存在的网址。   */
/* ------------------------------------------------------------------ */

async function listSnapshots(cfg) {
  const r = await apiRequest(cfg, '/api/v1/snapshots?clientId=' +
    encodeURIComponent(cfg.clientId));
  return r.items || [];
}

async function restoreSnapshot(cfg, snapshotId) {
  const data = await apiRequest(cfg, '/api/v1/snapshot?id=' + encodeURIComponent(snapshotId));
  const tree = data.tree || [];
  if (!tree.length) throw new Error('这份备份是空的');

  const rootId = await ensurePullFolder(cfg);
  const d = new Date();
  const pad = n => (n < 10 ? '0' : '') + n;
  const label = pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' +
    pad(d.getHours()) + ':' + pad(d.getMinutes());
  const base = await chrome.bookmarks.create({
    parentId: rootId, title: '本机历史备份 ' + label
  });

  const known = new Set(flattenBookmarks(await chrome.bookmarks.getTree()).map(b => b.url));
  let imported = 0;
  let skipped = 0;
  suppressChanges = true;
  try {
    const walk = async (nodes, parentId) => {
      for (const n of nodes || []) {
        if (imported >= MAX_RESTORE_PER_RUN) return;
        if (n.url) {
          if (known.has(n.url)) { skipped++; continue; }
          try {
            await chrome.bookmarks.create({
              parentId,
              title: (n.title || n.url).slice(0, 300),
              url: n.url
            });
            known.add(n.url);
            imported++;
          } catch (e) { skipped++; }
        } else if (!String(n.title || '').trim()) {
          // 快照根节点没有标题，直接展开它的子节点
          await walk(n.children, parentId);
        } else {
          try {
            const folder = await chrome.bookmarks.create({
              parentId, title: String(n.title).slice(0, 200)
            });
            await walk(n.children, folder.id);
          } catch (e) { /* 单个目录失败不影响其它 */ }
        }
      }
    };
    await walk(tree, base.id);
  } finally {
    suppressChanges = false;
  }
  if (!imported) {
    try { await chrome.bookmarks.removeTree(base.id); } catch (e) { /* ignore */ }
  }
  await setStatus({ lastPullCount: imported, lastPullAt: new Date().toISOString() });
  return { imported, skipped, folder: imported ? base.title : '' };
}

/* ------------------------------------------------------------------ */
/* 同步主流程                                                          */
/* ------------------------------------------------------------------ */

async function syncNow(reason = 'manual') {
  if (busy) return { ok: true, skipped: true, reason: 'sync-in-progress' };
  busy = true;
  await setStatus({ syncing: true });
  try {
    const cfg = await getConfig();
    if (!isConfigured(cfg)) throw new Error('尚未配置服务器地址或用户名密码，请先打开设置页');

    const tree = await getSyncTree(cfg.pullFolderId);
    const payload = {
      clientId: cfg.clientId,
      deviceName: cfg.deviceName,
      browser: browserVersion(),
      tree,
      bookmarkCount: countBookmarks(tree),
      clientTime: new Date().toISOString(),
      pull: !!cfg.pullEnabled,
      reason
    };
    const res = await apiRequest(cfg, '/api/v1/sync', {
      method: 'POST', body: payload, timeout: 90000
    });

    await setStatus({
      lastSync: new Date().toISOString(),
      lastError: '',
      lastSyncCount: res.bookmarkCount,
      serverDevices: res.devices,
      serverTotal: res.serverTotal,
      syncing: false
    });

    let pulled = 0;
    if (cfg.pullEnabled) {
      const incoming = res.incoming
        ? res.incoming.items
        : (await apiRequest(cfg, '/api/v1/incoming?exclude=' + encodeURIComponent(cfg.clientId))).items;
      pulled = await applyIncoming(cfg, incoming);
    }
    return { ok: true, bookmarkCount: res.bookmarkCount, devices: res.devices, pulled };
  } catch (e) {
    const msg = e && e.message ? e.message : String(e);
    await setStatus({ lastError: msg, syncing: false });
    return { ok: false, error: msg };
  } finally {
    busy = false;
    await setStatus({ syncing: false });
  }
}

/* ------------------------------------------------------------------ */
/* 生命周期与消息                                                      */
/* ------------------------------------------------------------------ */

chrome.runtime.onInstalled.addListener(async () => {
  await getConfig();       // 确保 clientId 生成
  await reschedule();
  const cfg = await getConfig();
  if (isConfigured(cfg)) syncNow('installed');
});
chrome.runtime.onStartup.addListener(() => { reschedule(); });

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === AUTO_ALARM) await syncNow('auto');
  else if (alarm.name === CHANGE_ALARM) {
    await chrome.alarms.clear(CHANGE_ALARM);
    await syncNow('change');
  }
});

for (const ev of ['onCreated', 'onRemoved', 'onChanged', 'onMoved']) {
  chrome.bookmarks[ev].addListener(() => { scheduleChangeSync(); });
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    switch (msg && msg.type) {
      case 'SYNC_NOW':
        return await syncNow(msg.reason || 'manual');

      case 'PULL_NOW': {
        const cfg = await getConfig();
        let url = '/api/v1/incoming?exclude=' + encodeURIComponent(cfg.clientId);
        if (msg.clientId) url += '&device=' + encodeURIComponent(msg.clientId);
        const inc = await apiRequest(cfg, url);
        const n = await applyIncoming(cfg, inc.items, inc.deviceName || '');
        return { ok: true, pulled: n, total: inc.total, device: inc.device || '' };
      }

      case 'LIST_DEVICES': {
        const cfg = await getConfig();
        if (!isConfigured(cfg)) throw new Error('请先在设置页填写服务器地址与账号密码');
        const r = await apiRequest(cfg, '/api/v1/devices');
        const items = (r.items || [])
          .filter(d => d.client_id !== cfg.clientId)   // 只列本账号的「其他」设备
          .map(d => ({ clientId: d.client_id, name: d.name || d.client_id,
                       count: d.bookmark_count || 0, lastSync: d.last_sync || '' }));
        return { ok: true, items };
      }

      case 'LIST_SNAPSHOTS': {
        const cfg = await getConfig();
        if (!isConfigured(cfg)) throw new Error('请先在设置页填写服务器地址与账号密码');
        const items = await listSnapshots(cfg);
        return { ok: true, items };
      }

      case 'RESTORE_SNAPSHOT': {
        const cfg = await getConfig();
        if (!isConfigured(cfg)) throw new Error('请先在设置页填写服务器地址与账号密码');
        const id = Number(msg.id || 0);
        if (!id) throw new Error('请选择要还原的历史备份');
        const r = await restoreSnapshot(cfg, id);
        return { ok: true, ...r };
      }

      case 'TEST_CONNECTION': {
        const cfg = await getConfig();
        const patch = msg.config ? { ...cfg, ...msg.config } : cfg;
        // healthCheck 会真正调用 /api/v1/auth/login，验证账号密码并换取会话令牌
        const h = await healthCheck(patch);
        return { ok: true, version: h.version, app: h.app,
                 user: (h.user && h.user.username) || patch.username || '',
                 role: (h.user && h.user.role) || '',
                 channelLabel: h.channelLabel, usedBase: h.usedBase };
      }

      case 'GET_STATUS': {
        const status = await getStatus();
        const cfg = await getConfig();
        return { ok: true, status, configured: isConfigured(cfg), config: cfg };
      }

      case 'GET_CONFIG':
        return { ok: true, config: await getConfig() };

      case 'SAVE_CONFIG': {
        const before = await getConfig();
        const patch = { ...msg.config };
        if ('serverUrl' in patch) patch.serverUrl = normalizeServerUrl(patch.serverUrl);
        if ('serverUrlAlt' in patch) patch.serverUrlAlt = normalizeServerUrl(patch.serverUrlAlt);
        if ('serverUrls' in patch) {
          patch.serverUrls = (Array.isArray(patch.serverUrls) ? patch.serverUrls : [])
            .map(u => normalizeServerUrl(u)).filter(Boolean).slice(0, 8);
        }
        const cfg = await saveConfig(patch);
        // 账号或密码变了，旧会话必须作废，下次同步会自动用新账号登录
        const accountChanged = ['serverUrl', 'serverUrlAlt', 'serverUrls', 'username', 'password']
          .some(k => String(before[k] || '') !== String(cfg[k] || ''));
        if (accountChanged) await invalidateSession();
        await invalidateEndpoint();   // 地址变更后强制重新探测内外网
        await reschedule();
        return { ok: true, config: cfg };
      }

      case 'SAVE_STRATEGY': {
        // v1.3.1：同步策略单独保存。不覆盖连接信息，也不要求重新填密码，
        // 且保存后立即重排定时任务——否则改了间隔不会生效。
        const patch = {
          autoSync: !!msg.autoSync,
          intervalMinutes: Math.max(1, Number(msg.intervalMinutes) || 15),
          syncOnChange: !!msg.syncOnChange,
          pullEnabled: !!msg.pullEnabled
        };
        const cfg = await saveConfig(patch);
        if (!patch.syncOnChange) await chrome.alarms.clear(CHANGE_ALARM);
        await reschedule();
        return { ok: true, config: cfg };
      }

      case 'RESET_CLIENT_ID': {
        await saveConfig({ clientId: crypto.randomUUID(), pullFolderId: '' });
        return { ok: true };
      }

      default:
        return { ok: false, error: '未知消息类型' };
    }
  })().then(sendResponse).catch(e => sendResponse({ ok: false, error: e.message || String(e) }));
  return true; // 异步 sendResponse
});
