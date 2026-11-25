/* Fn收藏夹同步 · Web 管理界面（v1.3.1：紧凑布局 + 设备管理页 + 只读浏览 + 快照管理 · v1.3.2 多格式导出 · v1.4.0 品牌中立） */
(function () {
  'use strict';

  var SESSION_KEY = 'fn_session';
  var BM_PAGE_SIZE = 15;
  var state = {
    token: localStorage.getItem(SESSION_KEY) || '',
    user: null,
    needsAdminSetup: false,
    plugin: null,
    devices: [],
    detail: { clientId: '', items: [], sel: {} },
    browse: { devices: [], current: '' },
    snap: { items: [], devices: [], current: '', page: 0, total: 0,
            selected: {}, previewId: 0, previewTree: [] },
    manage: { items: [], q: '', page: 0, selected: {}, trash: [] },
    editingId: 0
  };

  var $ = function (id) { return document.getElementById(id); };
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function toast(msg, bad) {
    var el = $('toast');
    el.textContent = msg;
    el.className = 'toast show' + (bad ? ' bad' : '');
    clearTimeout(el._t);
    el._t = setTimeout(function () { el.className = 'toast'; }, 2600);
  }
  function fmtTime(s) {
    if (!s) return '从未';
    var d = new Date(s);
    if (isNaN(d.getTime())) return s;
    var p = function (n) { return (n < 10 ? '0' : '') + n; };
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
      ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }
  function hostOf(url) {
    try { return new URL(url).hostname.replace(/^www\./, ''); } catch (e) { return url || ''; }
  }
  function initial(url) {
    var h = hostOf(url);
    return (h || '?').slice(0, 1).toUpperCase();
  }
  function leavesOf(nodes, out) {
    out = out || [];
    (nodes || []).forEach(function (n) {
      if (n.url) out.push(n); else leavesOf(n.children, out);
    });
    return out;
  }

  /* ---------- 基础请求 ---------- */

  function clearSession() {
    state.token = '';
    state.user = null;
    localStorage.removeItem(SESSION_KEY);
  }

  function api(path, opt) {
    opt = opt || {};
    opt.headers = Object.assign({ 'Authorization': 'Bearer ' + state.token }, opt.headers || {});
    if (opt.body && typeof opt.body !== 'string') {
      opt.headers['Content-Type'] = 'application/json';
      opt.body = JSON.stringify(opt.body);
    }
    return fetch(path, opt).then(function (r) {
      if (r.status === 401) {
        clearSession();
        showLogin('登录已失效，请重新输入用户名和密码');
        throw new Error('unauthorized');
      }
      return r.json().then(function (j) {
        if (!r.ok || j.ok === false) throw new Error(j.error || ('HTTP ' + r.status));
        return j;
      });
    });
  }

  function post(path, body) {
    return fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok || j.ok === false) throw new Error(j.error || ('HTTP ' + r.status));
        return j;
      });
    });
  }

  function copy(text) {
    if (!text) { toast('没有可复制的内容', true); return Promise.resolve(); }
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); } catch (e) {}
    document.body.removeChild(ta);
    return Promise.resolve();
  }

  function showLogin(err) {
    $('app').classList.add('hidden');
    $('login').classList.remove('hidden');
    $('loginErr').textContent = err || '';
    var first = !!state.needsAdminSetup;
    $('registerBox').classList.toggle('hidden', !first);
    $('loginForm').classList.toggle('hidden', first);
  }

  /* ---------- 连接插件（只读） ---------- */

  function loadPluginConfig() {
    return api('api/v1/plugin-config').then(function (j) {
      state.plugin = j;
      $('serverAddr').value = j.serverUrl || '';
      $('altAddr').value = j.serverUrlAlt || '';
      $('altRow').classList.toggle('hidden', !j.serverUrlAlt);
      $('usernameView').value = j.username || (state.user && state.user.username) || '';
      $('configPreview').textContent = j.configText || '{}';
      $('addrCount').textContent = (j.addresses || []).length;
      $('addressList').innerHTML = (j.addresses || []).map(function (a) {
        return '<div class="address-item">' +
          '<span class="addr-dot' + (a.primary ? ' primary' : '') + '"></span>' +
          '<code>' + esc(a.url) + '</code>' +
          '<span class="addr-label">' + esc(a.label) + '</span>' +
          (a.primary ? '<span class="pill">主</span>' : '') +
          (a.alternate ? '<span class="pill alt">备</span>' : '') +
          '</div>';
      }).join('') || '<div class="empty">无法推算地址，请用 http://&lt;NAS-IP&gt;:8765 打开本页</div>';
      $('connectSummary').textContent = (j.channel === 'gateway' ? '统一网关' : '直连') +
        ' · ' + (j.addresses || []).length + ' 个候选地址 · 无需填写';
      var gw = j.isGatewayPage
        ? ' 经统一网关访问时会复用浏览器的飞牛登录态；若网关拒绝，会自动切到局域网直连。'
        : '';
      $('accessNote').innerHTML = '地址由服务端按当前访问方式自动识别，<b>无需填写</b>；复制配置后在插件设置页粘贴，再输入一次密码即可（配置不含密码）。' + gw;
    }).catch(function (e) {
      if (e.message !== 'unauthorized') toast('地址识别失败：' + e.message, true);
    });
  }

  function enterApp() {
    $('login').classList.add('hidden');
    $('app').classList.remove('hidden');
    var u = state.user || {};
    $('userTag').textContent = (u.displayName || u.username || '—') +
      (u.role === 'admin' ? ' · 管理员' : '');
    $('usersTab').classList.toggle('hidden', u.role !== 'admin');
    loadAll();
  }

  function loadAll() {
    api('api/v1/status').then(function (j) {
      $('statDevices').textContent = j.devices;
      $('statBookmarks').textContent = j.bookmarks;
      $('statSnapshots').textContent = j.snapshots;
      $('statLast').textContent = fmtTime(j.lastSync);
      $('versionTag').textContent = 'v' + (j.version || '1.4.0');
      $('dedupNote').innerHTML = '自动去重：<b>' + (j.rawBookmarks || 0) + '</b> 条来源 → <b>' +
        (j.bookmarks || 0) + '</b> 条唯一书签（去除 ' + (j.duplicatesRemoved || 0) + ' 条重复）';
    }).catch(function (e) {
      if (e.message !== 'unauthorized') toast('状态加载失败：' + e.message, true);
    });
    loadPluginConfig();
    loadDevices();
    loadManage();
    loadLogs();
    loadSnapshots();
    if (state.user && state.user.role === 'admin') loadUsers();
    if (state.browse.current !== null) loadBrowse(state.browse.current);
  }

  /* ---------- 设备列表 ---------- */

  function loadDevices() {
    return api('api/v1/devices').then(function (j) {
      state.devices = j.items;
      var box = $('deviceList');
      if (!j.items.length) {
        box.innerHTML = '<div class="empty">还没有设备同步过。展开上方「连接浏览器插件」，把配置粘贴到插件里并同步一次。</div>';
        return;
      }
      box.innerHTML = j.items.map(function (d) {
        return '<div class="device" data-open-device="' + esc(d.client_id) + '">' +
          '<div class="device-icon">' + esc((d.name || '?').slice(0, 1).toUpperCase()) + '</div>' +
          '<div class="device-info">' +
            '<strong>' + esc(d.name) + '</strong>' +
            '<span>' + esc(d.browser || '未知浏览器') + ' · ' + d.bookmark_count +
              ' 条 · 最近同步 ' + fmtTime(d.last_sync) + '</span>' +
          '</div>' +
          '<div class="device-actions">' +
            '<button class="btn small" data-export-device="' + esc(d.client_id) +
              '" title="导出为 HTML；进入设备页可选 CSV / JSON">导出 HTML</button>' +
            '<button class="btn small danger-ghost" data-del-device="' + esc(d.client_id) + '">删除</button>' +
          '</div>' +
          '<span class="device-go">›</span></div>';
      }).join('');
    }).catch(function () {});
  }

  function openDevice(cid) {
    state.detail.clientId = cid;
    state.detail.sel = {};
    $('dtFilter').value = '';
    api('api/v1/devices/tree?clientId=' + encodeURIComponent(cid)).then(function (j) {
      state.detail.items = j.items || [];
      $('dtName').textContent = j.device.name;
      $('dtMeta').textContent = (j.device.browser || '未知浏览器') + ' · ' +
        j.total + ' 条书签 · 最近同步 ' + fmtTime(j.device.last_sync) + ' · 修订 r' + j.device.revision;
      renderDeviceTree();
      $('deviceListView').classList.add('hidden');
      $('deviceDetail').classList.remove('hidden');
    }).catch(function (e) { toast('加载设备失败：' + e.message, true); });
  }

  function closeDevice() {
    state.detail.clientId = '';
    $('deviceDetail').classList.add('hidden');
    $('deviceListView').classList.remove('hidden');
    loadDevices();
  }

  /* 客户端按 path 重建树：与服务端 tree_from_rows 保持同样的结构 */
  function buildClientTree(rows) {
    var root = { folders: {}, leaves: [] };
    rows.forEach(function (r) {
      var node = root;
      (r.path || []).forEach(function (seg) {
        seg = String(seg || '').trim();
        if (!seg) return;
        node = node.folders[seg] || (node.folders[seg] = { title: seg, folders: {}, leaves: [] });
      });
      node.leaves.push(r);
    });
    function clean(n) {
      var kids = Object.keys(n.folders).map(function (k) { return clean(n.folders[k]); });
      return { title: n.title, children: kids.concat(n.leaves) };
    }
    return Object.keys(root.folders).map(function (k) { return clean(root.folders[k]); })
      .concat(root.leaves);
  }

  function renderTreeNodes(nodes, depth, selectable) {
    return (nodes || []).map(function (n) {
      var pad = 6 + depth * 16;
      if (n.url) {
        var checked = selectable && state.detail.sel[String(n.id)] ? ' checked' : '';
        var badges = selectable ? '' : (n.devices || [n.device]).filter(Boolean).map(function (d) {
          return '<span class="dev-badge">' + esc(d) + '</span>';
        }).join('');
        return '<div class="tree-row leaf-row" style="padding-left:' + pad + 'px">' +
          (selectable ? '<input type="checkbox" data-bm="' + n.id + '"' + checked + '>' : '') +
          '<a class="leaf-link" href="' + esc(n.url) + '" target="_blank" rel="noopener" title="' + esc(n.url) + '">' +
            '<span class="fav">' + esc(initial(n.url)) + '</span>' +
            '<span class="leaf-title">' + esc(n.title || n.url) + '</span>' +
            '<span class="leaf-url">' + esc(hostOf(n.url)) + '</span>' +
          '</a>' + badges +
          (selectable ? '<button class="icon-mini" data-edit-bm="' + n.id + '" title="编辑">✎</button>' : '') +
          '</div>';
      }
      var count = leavesOf([n]).length;
      return '<div class="tree-folder">' +
        '<div class="tree-row folder" style="padding-left:' + pad + 'px">' +
          (selectable ? '<input type="checkbox" data-folder-check>' : '') +
          '<span class="caret">▾</span>' +
          '<span class="fname">' + esc(n.title || '未命名') + '</span>' +
          '<span class="count">' + count + '</span>' +
        '</div>' +
        '<div class="tree-children">' + renderTreeNodes(n.children || [], depth + 1, selectable) + '</div>' +
        '</div>';
    }).join('');
  }

  function renderDeviceTree() {
    var q = $('dtFilter').value.trim().toLowerCase();
    var rows = state.detail.items;
    if (q) {
      rows = rows.filter(function (r) {
        return String(r.title || '').toLowerCase().indexOf(q) >= 0 ||
               String(r.url || '').toLowerCase().indexOf(q) >= 0;
      });
    }
    var tree = buildClientTree(rows);
    $('deviceTree').innerHTML = tree.length
      ? renderTreeNodes(tree, 0, true)
      : '<div class="empty">' + (q ? '没有匹配的书签' : '这台设备还没有同步书签') + '</div>';
    refreshSelection();
  }

  function refreshSelection() {
    var root = $('deviceTree');
    var boxes = root.querySelectorAll('[data-bm]');
    var total = 0, checked = 0;
    boxes.forEach(function (cb) {
      total++;
      if (cb.checked) { checked++; state.detail.sel[String(cb.dataset.bm)] = true; }
      else delete state.detail.sel[String(cb.dataset.bm)];
    });
    var folders = Array.prototype.slice.call(root.querySelectorAll('.tree-folder'));
    folders.reverse().forEach(function (f) {
      var own = f.querySelector(':scope > .tree-row > input[data-folder-check]');
      var kids = f.querySelectorAll('[data-bm]');
      if (!own || !kids.length) return;
      var on = 0;
      kids.forEach(function (k) { if (k.checked) on++; });
      own.checked = on === kids.length;
      own.indeterminate = on > 0 && on < kids.length;
    });
    $('dtSelected').textContent = '已选 ' + checked + ' / ' + total;
    $('dtCheckAll').checked = total > 0 && checked === total;
    $('dtCheckAll').indeterminate = checked > 0 && checked < total;
    $('dtMoveBtn').disabled = !checked;
    $('dtDeleteBtn').disabled = !checked;
  }

  function selectedIds() {
    return Object.keys(state.detail.sel).map(Number);
  }

  /* ---------- 书签管理（表格 + 回收站） ---------- */

  function loadManage() {
    return Promise.all([
      api('api/v1/bookmarks'),
      api('api/v1/bookmarks/trash')
    ]).then(function (res) {
      state.manage.items = res[0].items || [];
      state.manage.trash = res[1].items || [];
      state.manage.selected = {};
      renderBmTable();
      renderTrash();
    }).catch(function (e) {
      if (e.message !== 'unauthorized') toast('书签列表加载失败：' + e.message, true);
    });
  }

  function filteredBookmarks() {
    var q = state.manage.q.trim().toLowerCase();
    if (!q) return state.manage.items;
    return state.manage.items.filter(function (r) {
      return String(r.title || '').toLowerCase().indexOf(q) >= 0 ||
             String(r.url || '').toLowerCase().indexOf(q) >= 0;
    });
  }

  function sourceLabel(r) {
    if (r.source === 'manual') return '<span class="pill manual">手动</span>';
    var name = esc(r.device_name || r.client_id || '同步');
    if (Number(r.source_count || 0) > 1) name += ' <span class="muted">· ' + r.source_count + ' 台</span>';
    return name;
  }

  function renderBmTable() {
    var items = filteredBookmarks();
    var pages = Math.max(1, Math.ceil(items.length / BM_PAGE_SIZE));
    if (state.manage.page >= pages) state.manage.page = pages - 1;
    if (state.manage.page < 0) state.manage.page = 0;
    var start = state.manage.page * BM_PAGE_SIZE;
    var slice = items.slice(start, start + BM_PAGE_SIZE);
    $('bmCount').textContent = '共 ' + items.length + ' 条';
    $('bmPageInfo').textContent = '第 ' + (state.manage.page + 1) + ' / ' + pages + ' 页';
    $('bmPrev').disabled = state.manage.page <= 0;
    $('bmNext').disabled = state.manage.page >= pages - 1;
    $('bmCheckAll').checked = slice.length > 0 && slice.every(function (r) {
      return !!state.manage.selected[r.id];
    });
    $('bmTable').innerHTML = slice.length ? slice.map(function (r) {
      return '<tr>' +
        '<td class="col-check"><input type="checkbox" data-check-id="' + r.id + '"' +
          (state.manage.selected[r.id] ? ' checked' : '') + '></td>' +
        '<td><strong>' + esc(r.title || r.url) + '</strong>' +
          ((r.path || []).length ? '<span class="muted bm-path">' + esc(r.path.join(' / ')) + '</span>' : '') +
        '</td>' +
        '<td><a href="' + esc(r.url) + '" target="_blank" rel="noopener" class="bm-url">' +
          esc(r.url) + '</a></td>' +
        '<td>' + sourceLabel(r) + '</td>' +
        '<td class="col-ops">' +
          '<button class="btn small" data-edit-id="' + r.id + '">编辑</button> ' +
          '<button class="btn small danger-ghost" data-del-id="' + r.id + '">删除</button>' +
        '</td></tr>';
    }).join('') : '<tr><td colspan="5"><div class="empty">没有书签。点「+ 新增」，或先在插件里同步一次。</div></td></tr>';
  }

  function renderTrash() {
    var list = state.manage.trash;
    var html = list.length ? list.map(function (t) {
      return '<div class="trash-row">' +
        '<span class="trash-url">' + esc(t.url || t.url_key) + '</span>' +
        '<span class="muted">' + esc(t.title || '') + ' · ' + fmtTime(t.deleted_at) + '</span>' +
        '<span class="device-actions">' +
          '<button class="btn small" data-restore-key="' + esc(t.url_key) + '">恢复</button>' +
          '<button class="btn small danger-ghost" data-purge-key="' + esc(t.url_key) + '">移除</button>' +
        '</span></div>';
    }).join('') : '<div class="empty small">回收站为空。</div>';
    ['trashList', 'trashListDevice'].forEach(function (id) {
      if ($(id)) $(id).innerHTML = html;
    });
    ['trashCount', 'trashCountDevice'].forEach(function (id) {
      if ($(id)) $(id).textContent = list.length + ' 条';
    });
    ['clearTrash', 'clearTrashDevice'].forEach(function (id) {
      if ($(id)) $(id).disabled = !list.length;
    });
  }

  function openBmModal(item) {
    state.editingId = item ? item.id : 0;
    $('bmModalTitle').textContent = item ? '编辑书签 #' + item.id : '新增书签';
    $('bmTitle').value = item ? (item.title || '') : '';
    $('bmUrl').value = item ? (item.url || '') : '';
    $('bmPath').value = item ? (item.path || []).join(' / ') : '';
    $('bmModalErr').textContent = '';
    $('bmModal').classList.remove('hidden');
    $('bmTitle').focus();
  }
  function closeBmModal() { $('bmModal').classList.add('hidden'); }

  function saveBmModal() {
    var body = {
      title: $('bmTitle').value.trim(),
      url: $('bmUrl').value.trim(),
      path: $('bmPath').value
    };
    if (!body.url) { $('bmModalErr').textContent = '请填写网址'; return; }
    var editing = state.editingId;
    if (editing) body.id = editing;
    $('bmModalSave').disabled = true;
    api('api/v1/bookmarks', { method: editing ? 'PUT' : 'POST', body: body })
      .then(function () {
        closeBmModal();
        toast(editing ? '书签已更新' : '书签已添加');
        loadAll();
        if (state.detail.clientId) openDevice(state.detail.clientId);
      }).catch(function (e) {
        $('bmModalErr').textContent = e.message;
      }).then(function () { $('bmModalSave').disabled = false; });
  }

  function deleteBookmarks(ids, label, after) {
    if (!ids.length) return;
    if (!confirm('确定删除' + label + '？删除后同步会自动跳过这些网址，可在回收站恢复。')) return;
    api('api/v1/bookmarks?ids=' + ids.join(','), { method: 'DELETE' })
      .then(function (j) {
        toast('已删除 ' + (j.deleted || []).length + ' 条');
        loadAll();
        if (after) after();
      }).catch(function (e) { toast(e.message, true); });
  }

  function clearTrash() {
    if (!state.manage.trash.length) return;
    if (!confirm('确定清空回收站？清空后这些网址在下次同步时会被重新带回。')) return;
    api('api/v1/bookmarks/trash?all=1', { method: 'DELETE' })
      .then(function (j) {
        toast('回收站已清空（' + (j.cleared || 0) + ' 条）');
        loadAll();
      }).catch(function (e) { toast(e.message, true); });
  }

  /* ---------- 书签浏览（只读） ---------- */

  function loadBrowse(cid) {
    api('api/v1/browse' + (cid ? '?device=' + encodeURIComponent(cid) : ''))
      .then(function (j) {
        state.browse.current = cid || '';
        state.browse.devices = j.devices || [];
        renderBrowseChips(j.total || 0);
        $('treeView').innerHTML = j.tree.length
          ? renderTreeNodes(j.tree, 0, false)
          : '<div class="empty">暂无书签数据。</div>';
      }).catch(function (e) {
        if (e.message !== 'unauthorized') toast('浏览加载失败：' + e.message, true);
      });
  }

  function renderBrowseChips(total) {
    var cur = state.browse.current;
    var html = '<button class="chip' + (cur ? '' : ' active') + '" data-browse="">全部<small>' + total + '</small></button>';
    html += state.browse.devices.map(function (d) {
      return '<button class="chip' + (cur === d.clientId ? ' active' : '') +
        '" data-browse="' + esc(d.clientId) + '">' + esc(d.name) +
        '<small>' + d.count + '</small></button>';
    }).join('');
    if (!state.browse.devices.length) html += '<span class="muted">还没有设备同步</span>';
    $('browseDevices').innerHTML = html;
  }

  /* ---------- 日志 / 快照 / 用户 / 搜索 ---------- */

  function loadLogs() {
    api('api/v1/logs').then(function (j) {
      $('logList').innerHTML = j.items.length ? j.items.map(function (l) {
        return '<div class="log-row"><span class="log-time">' + fmtTime(l.at) + '</span>' +
          '<span class="tag ' + esc(l.level) + '">' + esc(l.level) + '</span>' +
          '<span>' + esc(l.message) + '</span></div>';
      }).join('') : '<div class="empty">暂无记录。</div>';
    }).catch(function () {});
  }

  /* ---------- 快照管理（v1.3.1）：列表 / 预览 / 下载 / 删除 ---------- */

  var SNAP_PAGE_SIZE = 20;

  function fmtSize(n) {
    n = Number(n) || 0;
    return n < 1024 ? n + ' B' : (n < 1024 * 1024
      ? (n / 1024).toFixed(1) + ' KB' : (n / 1024 / 1024).toFixed(1) + ' MB');
  }

  function normalizeSnapTree(nodes) {
    return (nodes || []).reduce(function (acc, n) {
      if (n.url) { acc.push(n); return acc; }
      if (!String(n.title || '').trim()) return acc.concat(normalizeSnapTree(n.children));
      acc.push({ title: n.title, children: normalizeSnapTree(n.children) });
      return acc;
    }, []);
  }

  function loadSnapshots() {
    var q = 'api/v1/snapshots/manage?limit=' + SNAP_PAGE_SIZE +
      '&offset=' + (state.snap.page * SNAP_PAGE_SIZE) +
      (state.snap.current ? '&device=' + encodeURIComponent(state.snap.current) : '');
    return api(q).then(function (j) {
      var items = j.items || [];
      // 删掉最后一页的最后一条时自动回退一页
      if (state.snap.page > 0 && !items.length) {
        state.snap.page--;
        return loadSnapshots();
      }
      state.snap.items = items;
      state.snap.devices = j.devices || [];
      state.snap.total = j.total || 0;
      state.snap.selected = {};
      renderSnapChips();
      renderSnapTable();
    }).catch(function (e) {
      if (e.message !== 'unauthorized') toast('快照加载失败：' + e.message, true);
    });
  }

  function renderSnapChips() {
    var cur = state.snap.current;
    var html = '<button class="chip' + (cur ? '' : ' active') +
      '" data-snap-device="">全部<small>' + state.snap.total + '</small></button>';
    html += state.snap.devices.map(function (d) {
      return '<button class="chip' + (cur === d.clientId ? ' active' : '') +
        '" data-snap-device="' + esc(d.clientId) + '">' + esc(d.name) + '</button>';
    }).join('');
    if (!state.snap.devices.length) html += '<span class="muted">还没有设备同步</span>';
    $('snapDevices').innerHTML = html;
  }

  function renderSnapTable() {
    var rows = state.snap.items;
    $('snapTable').innerHTML = rows.length ? rows.map(function (s) {
      var checked = state.snap.selected[s.id] ? ' checked' : '';
      return '<tr>' +
        '<td class="col-check"><input type="checkbox" data-snap-check="' + s.id + '"' + checked + '></td>' +
        '<td>' + fmtTime(s.created_at) + '</td>' +
        '<td><strong>' + esc(s.deviceName) + '</strong></td>' +
        '<td>' + (s.count || 0) + ' 条</td>' +
        '<td>' + fmtSize(s.size) + '</td>' +
        '<td class="col-ops">' +
          '<button class="btn small" data-snap-view="' + s.id + '">查看</button> ' +
          '<select class="fmt-select" data-snap-fmt="' + s.id + '" aria-label="快照导出格式">' +
            '<option value="html">HTML</option>' +
            '<option value="csv">CSV</option>' +
            '<option value="json">JSON</option>' +
          '</select> ' +
          '<button class="btn small" data-snap-download="' + s.id + '">下载</button> ' +
          '<button class="btn small danger-ghost" data-snap-del="' + s.id + '">删除</button>' +
        '</td></tr>';
    }).join('') : '<tr><td colspan="6"><div class="empty">暂无快照。设备同步一次后会自动生成。</div></td></tr>';

    var pages = Math.max(1, Math.ceil(state.snap.total / SNAP_PAGE_SIZE));
    $('snapPageInfo').textContent = '第 ' + (state.snap.page + 1) + ' / ' + pages +
      ' 页 · 共 ' + state.snap.total + ' 份';
    $('snapPrev').disabled = state.snap.page <= 0;
    $('snapNext').disabled = state.snap.page + 1 >= pages;
    $('snapCheckAll').checked = false;
    refreshSnapSelection();
  }

  function refreshSnapSelection() {
    var n = Object.keys(state.snap.selected).filter(function (k) {
      return state.snap.selected[k];
    }).length;
    $('snapSummary').textContent = n ? '已选 ' + n + ' 份' : '勾选可批量删除';
    $('snapDeleteSelBtn').disabled = !n;
  }

  function openSnapModal(id) {
    api('api/v1/snapshot?id=' + id).then(function (j) {
      state.snap.previewId = id;
      state.snap.previewTree = j.tree || [];
      var s = state.snap.items.filter(function (x) { return x.id === id; })[0] || {};
      $('snapModalTitle').textContent = '快照 #' + id + ' · ' + (s.deviceName || '设备');
      $('snapModalMeta').textContent = fmtTime(s.created_at) + ' · ' +
        (s.count || 0) + ' 条书签 · ' + fmtSize(s.size) + ' · 原始目录结构（只读）';
      var tree = normalizeSnapTree(state.snap.previewTree);
      $('snapModalTree').innerHTML = tree.length
        ? renderTreeNodes(tree, 0, false)
        : '<div class="empty">这份快照是空的。</div>';
      $('snapModal').classList.remove('hidden');
    }).catch(function (er) { toast(er.message, true); });
  }

  function closeSnapModal() {
    $('snapModal').classList.add('hidden');
    state.snap.previewId = 0;
    state.snap.previewTree = [];
  }

  function downloadSnapshot(id, fmt) {
    // 服务端按 format 直接生成 HTML / CSV / JSON 附件，预览仍走无 format 的 JSON 接口
    download('api/v1/snapshot?id=' + id + '&format=' + encodeURIComponent(fmt || 'html'));
  }

  function deleteSnapshots(params, label) {
    if (!confirm('确定删除' + label + '？\n当前书签不受影响，设备下次同步会生成新快照。')) return;
    api('api/v1/snapshot' + params, { method: 'DELETE' }).then(function (j) {
      closeSnapModal();
      toast('已删除 ' + j.deleted + ' 份快照');
      loadAll();
    }).catch(function (er) { toast(er.message, true); });
  }

  function loadUsers() {
    api('api/v1/users').then(function (j) {
      $('userList').innerHTML = j.items.map(function (u) {
        var me = state.user && u.id === state.user.id;
        return '<div class="user-row">' +
          '<div class="device-icon">' + esc((u.displayName || u.username).slice(0, 1).toUpperCase()) + '</div>' +
          '<div class="device-info">' +
            '<strong>' + esc(u.displayName || u.username) +
              ' <span class="role ' + esc(u.role) + '">' + (u.role === 'admin' ? '管理员' : '用户') + '</span>' +
              (me ? ' <span class="role self">当前</span>' : '') + '</strong>' +
            '<span>@' + esc(u.username) + ' · 最近登录 ' + fmtTime(u.lastLogin) + '</span>' +
          '</div>' +
          '<div class="device-actions">' +
            '<button class="btn small" data-reset-user="' + u.id + '" data-name="' + esc(u.username) + '">重置密码</button>' +
            (me ? '' : '<button class="btn small danger-ghost" data-del-user="' + u.id + '" data-name="' + esc(u.username) + '">删除</button>') +
          '</div></div>';
      }).join('');
    }).catch(function () {});
  }

  function renderSearch(items) {
    $('searchResult').innerHTML = items.length ? items.map(function (r) {
      return '<a class="result" href="' + esc(r.url) + '" target="_blank" rel="noopener">' +
        '<strong>' + esc(r.title || r.url) + '</strong>' +
        '<span>' + esc(r.url) + '</span>' +
        '<em>' + esc((r.path || []).join(' / ')) + ' · ' + esc(r.device_name || r.client_id) + '</em></a>';
    }).join('') : '<div class="empty">没有匹配的书签。</div>';
  }

  function download(path) {
    fetch(path, { headers: { 'Authorization': 'Bearer ' + state.token } })
      .then(function (r) {
        if (r.status === 401) { clearSession(); showLogin('登录已失效，请重新登录'); throw new Error('unauthorized'); }
        if (!r.ok) throw new Error('HTTP ' + r.status);
        var cd = r.headers.get('Content-Disposition') || '';
        var m = /filename="([^"]+)"/.exec(cd);
        return r.blob().then(function (b) { return { blob: b, name: m ? m[1] : 'bookmarks.html' }; });
      })
      .then(function (o) {
        var a = document.createElement('a');
        a.href = URL.createObjectURL(o.blob);
        a.download = o.name;
        a.click();
        setTimeout(function () { URL.revokeObjectURL(a.href); }, 3000);
        toast('导出成功');
      }).catch(function (e) { if (e.message !== 'unauthorized') toast('导出失败：' + e.message, true); });
  }

  /* 统一导出：从格式下拉读 format，可带 device 只导某台设备 */
  function exportWith(selectId, device) {
    var sel = $(selectId);
    var fmt = sel ? sel.value : 'html';
    download('api/v1/export?format=' + encodeURIComponent(fmt) +
      (device ? '&device=' + encodeURIComponent(device) : ''));
  }

  /* ---------- 登录 / 注册 ---------- */

  function saveSession(j) {
    state.token = j.token || '';
    state.user = j.user || null;
    localStorage.setItem(SESSION_KEY, state.token);
    state.needsAdminSetup = false;
  }

  $('registerBtn').addEventListener('click', function () {
    var username = $('regUsername').value.trim();
    var password = $('regPassword').value;
    var again = $('regPassword2').value;
    if (!username) { $('loginErr').textContent = '请填写管理员用户名'; return; }
    if (!password || password.length < 6) { $('loginErr').textContent = '密码至少 6 位'; return; }
    if (password !== again) { $('loginErr').textContent = '两次输入的密码不一致'; return; }
    $('registerBtn').disabled = true;
    post('api/v1/setup/register', {
      username: username, password: password, displayName: $('regDisplay').value.trim()
    }).then(function (j) {
      saveSession(j);
      $('loginErr').textContent = '';
      toast('管理员已创建');
      enterApp();
    }).catch(function (e) {
      $('loginErr').textContent = e.message;
    }).then(function () { $('registerBtn').disabled = false; });
  });

  $('loginBtn').addEventListener('click', function () {
    var username = $('usernameInput').value.trim();
    var password = $('passwordInput').value;
    if (!username || !password) { $('loginErr').textContent = '请输入用户名和密码'; return; }
    $('loginBtn').disabled = true;
    post('api/v1/auth/login', { username: username, password: password }).then(function (j) {
      saveSession(j);
      $('loginErr').textContent = '';
      $('passwordInput').value = '';
      enterApp();
    }).catch(function (e) {
      $('loginErr').textContent = e.message;
    }).then(function () { $('loginBtn').disabled = false; });
  });
  ['usernameInput', 'passwordInput'].forEach(function (id) {
    $(id).addEventListener('keydown', function (e) {
      if (e.key === 'Enter') $('loginBtn').click();
    });
  });

  $('logoutBtn').addEventListener('click', function () {
    api('api/v1/auth/logout', { method: 'POST', body: {} }).catch(function () {});
    clearSession();
    showLogin('');
  });
  $('refreshBtn').addEventListener('click', function () { loadAll(); toast('已刷新'); });

  document.querySelectorAll('[data-copy]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      copy($(btn.dataset.copy).value).then(function () { toast('已复制'); });
    });
  });

  $('copyConfigBtn').addEventListener('click', function (e) {
    e.preventDefault();
    e.stopPropagation();
    if (!state.plugin) { toast('地址尚未识别完成', true); return; }
    copy(state.plugin.configText).then(function () {
      toast('插件配置已复制，粘贴到插件设置页后只需输入密码');
    });
  });

  /* ---------- Tabs ---------- */

  function activateTab(name) {
    var t = document.querySelector('.tab[data-tab="' + name + '"]');
    if (!t) return;
    document.querySelectorAll('.tab').forEach(function (x) { x.classList.remove('active'); });
    document.querySelectorAll('.tab-panel').forEach(function (x) { x.classList.remove('active'); });
    t.classList.add('active');
    $('panel-' + name).classList.add('active');
    if (name === 'manage') loadManage();
    if (name === 'browse') loadBrowse(state.browse.current);
    if (name === 'logs') loadLogs();
    if (name === 'snapshots') loadSnapshots();
  }

  $('tabs').addEventListener('click', function (e) {
    var t = e.target.closest('.tab');
    if (!t) return;
    activateTab(t.dataset.tab);
  });

  /* ---------- 目录树交互 ---------- */

  function toggleFolder(row) {
    var body = row.nextElementSibling;
    if (!body) return;
    var collapsed = body.classList.toggle('collapsed');
    var caret = row.querySelector('.caret');
    if (caret) caret.textContent = collapsed ? '▸' : '▾';
  }

  $('deviceTree').addEventListener('click', function (e) {
    if (e.target.closest('.icon-mini')) {
      var edit = e.target.closest('[data-edit-bm]');
      var id = Number(edit.dataset.editBm);
      var item = state.detail.items.filter(function (x) { return x.id === id; })[0];
      if (item) openBmModal(item);
      return;
    }
    var row = e.target.closest('.tree-row.folder');
    if (row && !e.target.closest('input')) toggleFolder(row);
  });

  $('deviceTree').addEventListener('change', function (e) {
    var box = e.target;
    if (box.dataset.bm) { refreshSelection(); return; }
    if (box.hasAttribute('data-folder-check')) {
      var folder = box.closest('.tree-folder');
      if (!folder) return;
      folder.querySelectorAll('[data-bm]').forEach(function (k) { k.checked = box.checked; });
      refreshSelection();
    }
  });

  $('dtCheckAll').addEventListener('change', function () {
    var on = this.checked;
    $('deviceTree').querySelectorAll('[data-bm]').forEach(function (k) { k.checked = on; });
    refreshSelection();
  });

  $('dtFilter').addEventListener('input', function () {
    state.detail.sel = {};
    renderDeviceTree();
  });

  $('deviceBackBtn').addEventListener('click', closeDevice);

  $('deviceList').addEventListener('click', function (e) {
    var del = e.target.closest('[data-del-device]');
    var exp = e.target.closest('[data-export-device]');
    var open = e.target.closest('[data-open-device]');
    if (exp) {
      e.stopPropagation();
      download('api/v1/export?format=html&device=' + encodeURIComponent(exp.dataset.exportDevice));
      return;
    }
    if (del) {
      e.stopPropagation();
      if (!confirm('确定删除该设备及其全部书签快照？此操作不可恢复。')) return;
      api('api/v1/devices?id=' + encodeURIComponent(del.dataset.delDevice), { method: 'DELETE' })
        .then(function () { toast('已删除'); loadAll(); })
        .catch(function (er) { toast(er.message, true); });
      return;
    }
    if (open) openDevice(open.dataset.openDevice);
  });

  $('dtExportBtn').addEventListener('click', function () {
    if (state.detail.clientId) exportWith('deviceFmt', state.detail.clientId);
  });

  $('dtDelDeviceBtn').addEventListener('click', function () {
    var cid = state.detail.clientId;
    if (!cid) return;
    if (!confirm('确定删除该设备及其全部书签快照？此操作不可恢复。')) return;
    api('api/v1/devices?id=' + encodeURIComponent(cid), { method: 'DELETE' })
      .then(function () { toast('已删除'); closeDevice(); loadAll(); })
      .catch(function (er) { toast(er.message, true); });
  });

  $('dtDeleteBtn').addEventListener('click', function () {
    deleteBookmarks(selectedIds(), '选中的 ' + selectedIds().length + ' 条书签', function () {
      if (state.detail.clientId) openDevice(state.detail.clientId);
    });
  });

  /* 批量修改文件夹 */
  function openMoveModal() {
    var ids = selectedIds();
    if (!ids.length) { toast('请先勾选书签', true); return; }
    $('moveCount').textContent = ids.length;
    $('movePath').value = '';
    $('moveErr').textContent = '';
    $('moveModal').classList.remove('hidden');
    $('movePath').focus();
  }
  function closeMoveModal() { $('moveModal').classList.add('hidden'); }

  $('dtMoveBtn').addEventListener('click', openMoveModal);
  $('moveClose').addEventListener('click', closeMoveModal);
  $('moveCancel').addEventListener('click', closeMoveModal);
  $('moveModal').addEventListener('click', function (e) {
    if (e.target === $('moveModal')) closeMoveModal();
  });
  $('movePath').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') $('moveSave').click();
  });
  $('moveSave').addEventListener('click', function () {
    var ids = selectedIds();
    if (!ids.length) return;
    $('moveSave').disabled = true;
    post('api/v1/bookmarks/batch', { ids: ids, path: $('movePath').value })
      .then(function (j) {
        closeMoveModal();
        toast('已移动 ' + j.moved + ' 条书签');
        state.detail.sel = {};
        openDevice(state.detail.clientId);
        loadAll();
      }).catch(function (e) {
        $('moveErr').textContent = e.message;
      }).then(function () { $('moveSave').disabled = false; });
  });

  /* ---------- 书签浏览 ---------- */

  $('browseDevices').addEventListener('click', function (e) {
    var c = e.target.closest('[data-browse]');
    if (!c) return;
    loadBrowse(c.dataset.browse);
  });

  $('treeView').addEventListener('click', function (e) {
    var row = e.target.closest('.tree-row.folder');
    if (row) toggleFolder(row);
  });

  /* ---------- 书签管理事件 ---------- */

  $('addBookmarkBtn').addEventListener('click', function () { openBmModal(null); });
  $('reloadBookmarksBtn').addEventListener('click', function () { loadAll(); toast('已刷新'); });
  $('bmModalClose').addEventListener('click', closeBmModal);
  $('bmModalCancel').addEventListener('click', closeBmModal);
  $('bmModalSave').addEventListener('click', saveBmModal);
  ['bmTitle', 'bmUrl', 'bmPath'].forEach(function (id) {
    $(id).addEventListener('keydown', function (e) {
      if (e.key === 'Enter') saveBmModal();
    });
  });
  $('bmModal').addEventListener('click', function (e) {
    if (e.target === $('bmModal')) closeBmModal();
  });

  function applyFilter() {
    state.manage.q = $('bmSearch').value;
    state.manage.page = 0;
    renderBmTable();
  }
  $('bmSearch').addEventListener('input', applyFilter);
  $('bmSearchClear').addEventListener('click', function () {
    $('bmSearch').value = '';
    applyFilter();
  });
  $('bmPrev').addEventListener('click', function () { state.manage.page--; renderBmTable(); });
  $('bmNext').addEventListener('click', function () { state.manage.page++; renderBmTable(); });

  $('bmCheckAll').addEventListener('change', function () {
    var on = this.checked;
    var items = filteredBookmarks().slice(
      state.manage.page * BM_PAGE_SIZE, (state.manage.page + 1) * BM_PAGE_SIZE);
    items.forEach(function (r) { state.manage.selected[r.id] = on; });
    renderBmTable();
  });

  $('bmTable').addEventListener('click', function (e) {
    var edit = e.target.closest('[data-edit-id]');
    var del = e.target.closest('[data-del-id]');
    if (edit) {
      var id = Number(edit.dataset.editId);
      var item = state.manage.items.filter(function (x) { return x.id === id; })[0];
      if (item) openBmModal(item);
    }
    if (del) deleteBookmarks([Number(del.dataset.delId)], '这 1 条书签');
  });
  $('bmTable').addEventListener('change', function (e) {
    var cb = e.target.closest('[data-check-id]');
    if (!cb) return;
    state.manage.selected[Number(cb.dataset.checkId)] = cb.checked;
  });
  $('batchDeleteBtn').addEventListener('click', function () {
    var ids = Object.keys(state.manage.selected).filter(function (k) {
      return state.manage.selected[k];
    }).map(Number);
    if (!ids.length) { toast('请先勾选要删除的书签', true); return; }
    deleteBookmarks(ids, '选中的 ' + ids.length + ' 条书签');
  });

  /* 回收站：恢复 / 移除 / 一键清空（两个面板共用） */
  document.addEventListener('click', function (e) {
    var restore = e.target.closest('[data-restore-key]');
    var purge = e.target.closest('[data-purge-key]');
    if (restore) {
      api('api/v1/bookmarks/restore', {
        method: 'POST', body: { key: restore.dataset.restoreKey }
      }).then(function () { toast('已恢复'); loadAll(); })
        .catch(function (er) { toast(er.message, true); });
    }
    if (purge) {
      if (!confirm('彻底移除后，浏览器下次同步会重新带回该网址。确定继续？')) return;
      api('api/v1/bookmarks/trash?key=' + encodeURIComponent(purge.dataset.purgeKey),
          { method: 'DELETE' })
        .then(function () { toast('已移除'); loadManage(); })
        .catch(function (er) { toast(er.message, true); });
    }
  });
  $('clearTrash').addEventListener('click', clearTrash);
  $('clearTrashDevice').addEventListener('click', clearTrash);

  /* ---------- 搜索 / 用户 / 设置 ---------- */

  $('searchInput').addEventListener('keydown', function (e) {
    if (e.key !== 'Enter') return;
    var q = this.value.trim();
    if (!q) { renderSearch([]); return; }
    api('api/v1/search?q=' + encodeURIComponent(q)).then(function (j) { renderSearch(j.items); })
      .catch(function (er) { toast(er.message, true); });
  });

  $('userList').addEventListener('click', function (e) {
    var reset = e.target.closest('[data-reset-user]');
    var del = e.target.closest('[data-del-user]');
    if (reset) {
      var pw = prompt('为用户「' + reset.dataset.name + '」设置新密码（至少 6 位）：');
      if (pw === null) return;
      if (pw.length < 6) { toast('密码至少 6 位', true); return; }
      api('api/v1/users/password', { method: 'POST', body: { id: Number(reset.dataset.resetUser), password: pw } })
        .then(function () { toast('密码已重置，该用户会话已失效'); })
        .catch(function (er) { toast(er.message, true); });
    }
    if (del) {
      if (!confirm('删除用户「' + del.dataset.name + '」会同时清空其全部收藏夹与快照，且不可恢复。确定继续？')) return;
      api('api/v1/users?id=' + encodeURIComponent(del.dataset.delUser), { method: 'DELETE' })
        .then(function () { toast('用户已删除'); loadUsers(); })
        .catch(function (er) { toast(er.message, true); });
    }
  });

  $('addUserBtn').addEventListener('click', function () {
    var body = {
      username: $('newUsername').value.trim(),
      displayName: $('newDisplay').value.trim(),
      password: $('newPassword').value,
      role: $('newRole').value
    };
    if (!body.username) { toast('请填写用户名', true); return; }
    if (body.password.length < 6) { toast('初始密码至少 6 位', true); return; }
    api('api/v1/users', { method: 'POST', body: body }).then(function () {
      $('newUsername').value = ''; $('newDisplay').value = ''; $('newPassword').value = '';
      toast('用户已添加，把账号密码告诉对方即可');
      loadUsers();
    }).catch(function (er) { toast(er.message, true); });
  });

  $('changePwBtn').addEventListener('click', function () {
    var old = $('pwOld').value;
    var pw = $('pwNew').value;
    var again = $('pwNew2').value;
    if (!old || !pw) { toast('请填写原密码与新密码', true); return; }
    if (pw.length < 6) { toast('新密码至少 6 位', true); return; }
    if (pw !== again) { toast('两次输入的新密码不一致', true); return; }
    api('api/v1/auth/password', { method: 'POST', body: { oldPassword: old, newPassword: pw } })
      .then(function () {
        clearSession();
        $('pwOld').value = ''; $('pwNew').value = ''; $('pwNew2').value = '';
        showLogin('密码已修改，请重新登录');
        toast('密码已修改');
      }).catch(function (er) { toast(er.message, true); });
  });

  $('exportAllBtn').addEventListener('click', function () { exportWith('allFmt'); });
  $('browseExportBtn').addEventListener('click', function () { exportWith('browseFmt'); });
  /* ---------- 快照页交互 ---------- */

  $('snapDevices').addEventListener('click', function (e) {
    var b = e.target.closest('[data-snap-device]');
    if (!b) return;
    state.snap.current = b.dataset.snapDevice || '';
    state.snap.page = 0;
    loadSnapshots();
  });

  $('snapCheckAll').addEventListener('change', function () {
    var on = this.checked;
    state.snap.selected = {};
    if (on) state.snap.items.forEach(function (s) { state.snap.selected[s.id] = true; });
    $('snapTable').querySelectorAll('[data-snap-check]').forEach(function (c) { c.checked = on; });
    refreshSnapSelection();
  });

  $('snapTable').addEventListener('change', function (e) {
    var c = e.target.closest('[data-snap-check]');
    if (!c) return;
    state.snap.selected[c.dataset.snapCheck] = c.checked;
    refreshSnapSelection();
  });

  $('snapTable').addEventListener('click', function (e) {
    var v = e.target.closest('[data-snap-view]');
    var d = e.target.closest('[data-snap-download]');
    var x = e.target.closest('[data-snap-del]');
    if (v) openSnapModal(Number(v.dataset.snapView));
    else if (d) {
      var row = d.closest('tr');
      var sel = row && row.querySelector('[data-snap-fmt]');
      downloadSnapshot(Number(d.dataset.snapDownload), sel ? sel.value : 'html');
    }
    else if (x) deleteSnapshots('?id=' + Number(x.dataset.snapDel), '这 1 份快照');
  });

  $('snapDeleteSelBtn').addEventListener('click', function () {
    var ids = Object.keys(state.snap.selected).filter(function (k) {
      return state.snap.selected[k];
    });
    if (!ids.length) return;
    deleteSnapshots('?ids=' + ids.join(','), '选中的 ' + ids.length + ' 份快照');
  });
  $('snapClearBtn').addEventListener('click', function () {
    deleteSnapshots('?all=1', '全部快照');
  });
  $('snapReloadBtn').addEventListener('click', function () { loadSnapshots(); toast('已刷新'); });
  $('snapPrev').addEventListener('click', function () {
    if (state.snap.page > 0) { state.snap.page--; loadSnapshots(); }
  });
  $('snapNext').addEventListener('click', function () { state.snap.page++; loadSnapshots(); });

  $('snapModalClose').addEventListener('click', closeSnapModal);
  $('snapDownloadBtn').addEventListener('click', function () {
    if (state.snap.previewId) {
      downloadSnapshot(state.snap.previewId, $('snapModalFmt').value);
    }
  });
  $('snapDeleteOneBtn').addEventListener('click', function () {
    if (state.snap.previewId) deleteSnapshots('?id=' + state.snap.previewId, '这 1 份快照');
  });
  $('snapModal').addEventListener('click', function (e) {
    if (e.target === this) closeSnapModal();
  });
  $('snapModalTree').addEventListener('click', function (e) {
    if (e.target.closest('input') || e.target.closest('a')) return;
    var row = e.target.closest('.tree-row.folder');
    if (row) toggleFolder(row);
  });

  $('gotoSnapshotsBtn').addEventListener('click', function () { activateTab('snapshots'); });
  document.querySelectorAll('[data-jump]').forEach(function (el) {
    el.title = el.title || '点击查看';
    el.addEventListener('click', function () { activateTab(el.dataset.jump); });
  });

  /* ---------- 启动 ---------- */

  fetch('api/v1/setup/state').then(function (r) { return r.json(); }).then(function (j) {
    state.needsAdminSetup = !!j.needsAdminSetup;
    if (state.needsAdminSetup) {
      clearSession();
      showLogin('');
      return;
    }
    if (!state.token) { showLogin(''); return; }
    return api('api/v1/auth/me').then(function (me) {
      state.user = me.user;
      enterApp();
    }).catch(function () { showLogin(''); });
  }).catch(function () {
    showLogin('无法连接服务端，请确认应用已启动');
  });
})();
