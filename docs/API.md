# Fn收藏夹同步 · HTTP API

版本：`v1.4.0` · 编码：UTF-8 · 数据格式：JSON · 存储：SQLite（`bookmarks.db`）

> **v1.4.0 变更**：界面现代化 + 代码精简 + 开源发布，开发者统一为 **oldkevin**。**接口、字段、鉴权、导出格式全部不变**，与 v1.3.x 完全兼容。
>
> **v1.3.3 变更**：仅品牌与兼容性层面——插件按 UA 识别浏览器品牌、`minimum_chrome_version` 降到 91、界面与文档文案统一为「Chromium 系浏览器」。
>
> **v1.3.2 新增**：导出全面支持 **HTML / CSV / JSON** 三种通用格式——`GET /api/v1/export?format=html|csv|json` 新增 `csv`，`GET /api/v1/snapshot?id=&format=html|csv|json` 可按格式下载单份历史快照（不带 `format` 仍是原来的预览 JSON）。
>
> **v1.3.1 新增**：`GET /api/v1/snapshots/manage`、`DELETE /api/v1/snapshot`、`GET /api/v1/incoming?device=`。
>
> **v1.3.0 新增**：`GET /api/v1/devices/tree`、`GET /api/v1/browse`、`POST /api/v1/bookmarks/batch`、`DELETE /api/v1/bookmarks/trash?all=1`。

服务端提供三个基址，路由与数据完全一致：

| 通道 | 基址 | 使用者 | 鉴权 |
| --- | --- | --- | --- |
| TCP 直连 | `http://<NAS-IP>:8765` | 浏览器插件、局域网访问 | 应用账号密码换取的会话令牌 |
| 统一网关 | `https://<飞牛域名>/app/edge-bookmarks` | 桌面 Web 界面；插件也可使用 | 同上（飞牛另外校验 NAS 登录态，插件需浏览器已登录飞牛） |
| 自建反代 | `https://<统一登录地址>:<端口>/eb-api` | 浏览器插件（复用登录地址时） | 同上 |

网关通道下所有接口路径都要带 `/app/edge-bookmarks` 前缀，服务端会剥离前缀，前端通过注入 `<base href="/app/edge-bookmarks/">` 自动使用相对路径。

**自建反代**前缀任意（如 `/eb-api`），服务端三种方式识别：① 自动从 `/eb-api/api/v1/...` 推断第一段为前缀（零配置）；② `EB_PROXY_PREFIXES=/eb-api`；③ 反代剥离前缀时追加 `X-Forwarded-Prefix: /eb-api`（推荐）。该头会被严格校验，不接受 `..`、尖括号、引号，防止注入 HTML `<base>`。详见 [反向代理与统一登录地址](反向代理与统一登录地址.md)。

---

## 1. 鉴权：用户名 + 密码

自 v1.1.0 起**不再有随机访问令牌**。账号、密码哈希、会话都存在 SQLite 里。密码用 **PBKDF2-HMAC-SHA256 加盐迭代**（120000 轮）存储，不保存明文。

1. 首次调用 `POST /api/v1/setup/register` 注册管理员（仅当用户表为空时可用）。
2. 之后用 `POST /api/v1/auth/login` 换取会话令牌（有效期 **30 天**）。
3. 其余接口带 `Authorization: Bearer <token>`，或 `X-Auth-Token`，或查询参数 `token`。
4. 调试时可用 HTTP Basic：`Authorization: Basic base64(用户名:密码)`（每次校验、不留会话）。

插件保存账号密码，会话过期或服务重启后自动重新登录。

| 状态 | 含义 |
| --- | --- |
| 401 | 未登录 / 会话失效 |
| 403 | 需要管理员权限 / 原密码不正确 |
| 404 | 资源不存在或**不属于当前用户**（跨用户访问统一返回 404） |
| 409 | 网址重复（去重冲突） |
| 410 | 调用了旧版令牌接口，需升级插件 |

> v1.0.x 的 `/api/v1/token*` 与 `/api/v1/setup/claim` 已移除，调用返回 `410` 并提示升级插件。

---

## 2. 公开接口（无需登录）

### GET `/api/v1/health`

```json
{
  "ok": true, "app": "edge-bookmarks", "title": "Fn收藏夹同步",
  "version": "1.4.0", "authMode": "username-password",
  "needsAdminSetup": false, "time": "2026-11-25T09:00:00+08:00",
  "base": "/eb-api/", "channel": "direct",
  "prefixes": ["/app/edge-bookmarks", "/eb-api"]
}
```

### GET `/api/v1/setup/state`

```json
{
  "ok": true, "title": "Fn收藏夹同步", "version": "1.4.0",
  "needsAdminSetup": false, "userCount": 2,
  "gatewayAuth": true, "gatewayUser": "admin",
  "gatewayPrefix": "/app/edge-bookmarks", "proxyPrefixes": ["/app/edge-bookmarks"],
  "pageBase": "/app/edge-bookmarks/", "directPort": 8765,
  "pluginUrl": "http://192.0.2.10:8765",
  "pluginUrlAlt": "http://nas.example.com:8765",
  "pluginUrls": ["http://192.0.2.10:8765", "http://nas.example.com:8765"],
  "gatewaySocket": true, "sessionDays": 30
}
```

### POST `/api/v1/setup/register`

```json
{"username": "admin", "password": "Example@123456", "displayName": "管理员"}
```

响应：`{"ok":true,"registered":true,"token":"…","expiresAt":…,"sessionDays":30,"user":{…}}`

### POST `/api/v1/auth/login`

```json
{"username": "admin", "password": "Example@123456"}
```

响应同上（不含 `registered`）。

---

## 3. 账号与会话（需登录）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/auth/me` | 当前登录用户 |
| POST | `/api/v1/auth/logout` | 注销当前会话 |
| POST | `/api/v1/auth/password` | `{oldPassword,newPassword}`，改密后所有会话失效 |
| GET | `/api/v1/users` | 用户列表（**仅管理员**） |
| POST | `/api/v1/users` | `{username,password,role,displayName}` 新增用户（仅管理员） |
| POST | `/api/v1/users/password` | `{id,password}` 重置密码（仅管理员） |
| DELETE | `/api/v1/users?id=2` | 删除用户及其全部数据（仅管理员） |

用户信息不含 `password_hash`。不能删除当前登录账号，也不能删除最后一个管理员。

---

## 4. 插件配置自动识别

### GET `/api/v1/plugin-config` · 需登录

管理端「连接浏览器插件」卡片的数据来源。地址由服务端根据当前请求自动推算，界面只读，不需要任何输入框。

以下示例对应「从 `https://nas.example.com:9443/app/edge-bookmarks` 打开管理页、且 NAS 上未配置任何反代」的场景：

```json
{
  "ok": true, "app": "edge-bookmarks", "title": "Fn收藏夹同步", "version": "1.4.0",
  "username": "admin",
  "serverUrl": "https://nas.example.com:9443/app/edge-bookmarks",
  "serverUrlAlt": "http://192.0.2.10:8765",
  "serverUrls": ["https://nas.example.com:9443/app/edge-bookmarks", "http://192.0.2.10:8765", "http://nas.example.com:8765"],
  "addresses": [
    {"url": "https://nas.example.com:9443/app/edge-bookmarks", "label": "当前管理页地址 · 飞牛统一网关", "primary": true, "alternate": false},
    {"url": "http://192.0.2.10:8765", "label": "局域网直连 · 端口 8765", "primary": false, "alternate": true}
  ],
  "config": {
    "serverUrl": "https://nas.example.com:9443/app/edge-bookmarks",
    "serverUrlAlt": "http://192.0.2.10:8765",
    "serverUrls": ["http://nas.example.com:8765"],
    "username": "admin"
  },
  "configText": "{\n  \"serverUrl\": \"…\"\n}",
  "pageOrigin": "https://nas.example.com:9443",
  "pageBase": "/app/edge-bookmarks/",
  "pageUrl": "https://nas.example.com:9443/app/edge-bookmarks",
  "gatewayUrl": "https://nas.example.com:9443/app/edge-bookmarks",
  "isGatewayPage": true,
  "channel": "gateway",
  "gatewayPrefix": "/app/edge-bookmarks",
  "proxyPrefixes": [],
  "directPort": 8765,
  "autoDetected": true
}
```

规则：

- **`serverUrl` 恒等于 `pageUrl`（当前管理页地址）**，界面显示什么、插件就用什么；不再猜测任何未配置的路径。
- 候选来源：主地址、本机局域网 IP + `8765`、当前主机名 + `8765`、显式配置的 `EB_PROXY_PREFIXES`、飞牛统一网关入口。
- **`/eb-api` 等反代路径只有「当前请求经由该前缀」或显式配置 `EB_PROXY_PREFIXES` 时才下发**；未配置时不会出现在 `configText` 里。
- 统一网关 `/app/…` 会进入候选池：插件以 `credentials: 'include'` 请求，复用浏览器里已登录的飞牛会话。网关未登录时返回 `text/plain` 的 `invalid token`，health 探测判为失败并自动降级到局域网直连。
- `configText` 可直接粘贴进插件设置页，**不含密码**；插件按 `serverUrl → serverUrlAlt → serverUrls` 顺序逐个探测 `/api/v1/health`。
- `Host` / `X-Forwarded-Host` / 前缀头都经过白名单校验，恶意值不会进入配置或 HTML。

---

## 5. 书签（自动去重 + 增删改查）

`bookmarks` 对 `(user_id, url_key)` 唯一。`url_key` 由规范化网址计算，忽略协议差异、大小写、默认端口、末尾斜杠、`#锚点`、`utm_*` 等跟踪参数与查询参数顺序。

### GET `/api/v1/bookmarks` · 需登录

| 参数 | 说明 |
| --- | --- |
| `q` | 标题 / 网址模糊筛选 |
| `limit` | 每页条数（1–1000，默认全部） |
| `offset` | 偏移量 |

```json
{
  "ok": true, "total": 42, "count": 15, "dedup": true,
  "items": [{
    "id": 12, "user_id": 1, "client_id": "dev-1", "node_id": "31",
    "title": "GitHub", "url": "https://github.com/", "path": ["收藏夹栏", "开发"],
    "date_added": 1700000002, "url_key": "…", "source": "sync",
    "created_at": "…", "updated_at": "…",
    "device_name": "书房电脑", "source_count": 2
  }],
  "tree": [{"title": "书房电脑", "children": []}]
}
```

`source`：`sync`（插件同步）/ `manual`（管理端添加或编辑过，同步不会覆盖）。
`source_count`：有多少台设备提供过这个网址。

### GET `/api/v1/bookmarks/item?id=12`

返回 `{"ok":true,"item":{…}}`；不存在或不属于当前用户返回 `404`。

### POST `/api/v1/bookmarks` · 新增

```json
{"title": "示例站点", "url": "https://example.com/", "path": ["收藏夹栏", "常用"]}
```

`path` 可为数组或 `A / B` 字符串。网址重复 `409`；非法协议 / 空网址 `400`。

### PUT / PATCH `/api/v1/bookmarks` · 编辑

```json
{"id": 12, "title": "新标题", "url": "https://example.com/new", "path": ["工作"]}
```

- 只更新传来的字段。
- 网址冲突返回 `409`。
- 改网址后旧网址自动进回收站，该条转为 `manual`，防止下次同步又带回旧网址。
- 编辑结果不会被浏览器端同步覆盖。

### DELETE `/api/v1/bookmarks?id=12` 或 `?ids=12,13,14`

```json
{"ok": true, "deleted": [12], "hint": "已删除的网址会在下次同步自动跳过，可在回收站恢复"}
```

也可 `POST /api/v1/bookmarks/delete` 传 `{"ids":[12,13]}`。跨用户 id 不会出现在 `deleted` 里。

### 回收站

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/bookmarks/trash` | `[{url_key,url,title,deleted_at}]` |
| POST | `/api/v1/bookmarks/restore` | `{"key":"…"}` 或 `{"url":"…"}` 恢复 |
| DELETE | `/api/v1/bookmarks/trash?key=…` | 彻底移除记录（允许浏览器下次同步重新带回） |
| DELETE | `/api/v1/bookmarks/trash?all=1` | 一键清空回收站，返回 `{"ok":true,"cleared":N,"hint":"…"}`；`all` 也接受 `true` / `yes` |

> 一键清空同样意味着「下次同步会重新带回这些网址」，接口返回的 `hint` 里已带上该提示，界面需原样展示。

### POST `/api/v1/bookmarks/batch` · 需登录

批量把书签移动到指定目录（设备管理页「修改文件夹」按钮使用）。

```json
{"ids": [12, 13, 14], "path": ["工作", "项目"]}
```

`path` 也接受字符串形式 `"工作/项目"`（按 `/` 拆层级）。

响应：

```json
{"ok": true, "moved": 3, "hint": "已转为手动维护，浏览器下次同步不会覆盖"}
```

- 移到根目录：`"path": []` 或 `"path": ""`。**必须带 `path` 字段**，缺字段返回 `400`；`ids` 为空或没有合法数字编号同样返回 `400`。
- 层级最多 12 段、每段最长 120 字，超出部分自动截断。
- 移动后这些记录 `source` 转为 `manual`，并同步更新 `bookmark_sources.path`，因此**不会被下次浏览器同步按旧目录覆盖回去**。
- 跨用户 id 不计入 `moved`（静默跳过）。

### GET `/api/v1/search?q=github` · 需登录

返回 `{"ok":true,"items":[…]}`，内容为去重后的书签。

### GET `/api/v1/incoming?exclude=<clientId>` · 需登录

供插件回灌：包含「其他设备提供过」以及「管理端手动添加」的书签，去重后返回。

可追加 `device=<clientId>` 只取某台设备提供过的书签（插件「更多操作」里点设备标签即走这个参数）：

```json
{"ok": true, "total": 3, "device": "uuid", "deviceName": "第二台电脑", "items": [{"id":…, "url":…, "path":[…], "device_name":…}]}
```

- `device` 必须是当前用户名下的设备，未知返回 `404`；不传则为全部。`deviceName` 是该设备的显示名，插件用它给导入目录命名（缺省时退回书签自带的 `device_name`）。
- `exclude` 仍建议带上本机 `clientId`，避免把自己刚上传的书签又拉回来。

---

## 6. 同步

### POST `/api/v1/sync` · 需登录（插件调用）

```json
{
  "clientId": "uuid", "deviceName": "书房电脑", "browser": "Microsoft Edge 131",
  "tree": [{"id": "0", "title": "", "children": []}],
  "bookmarkCount": 3, "pull": false, "reason": "manual"
}
```

响应：

```json
{
  "ok": true, "revision": 4, "bookmarkCount": 5, "uniqueCount": 2,
  "duplicatesRemoved": 3, "skippedDeleted": 0,
  "serverTotal": 42, "devices": 2, "serverTime": "…",
  "incoming": {"total": 0, "items": []}
}
```

- `bookmarkCount` 是上传的原始条数；`uniqueCount` 是去重后写入的条数。
- `duplicatesRemoved` = 单次上传内部重复 + 与云端已有记录重复。
- `skippedDeleted` = 因回收站记录而跳过的条数。
- `pull=true` 时附带 `incoming`。
- 同步后自动生成 HTML 备份（应用自有 `exports/` 及可选 `EB_SHARE_DIR`）。

---

## 7. 状态、设备、快照、导出、日志

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/status` | `devices / bookmarks / rawBookmarks / duplicatesRemoved / trash / snapshots / lastSync / version` |
| GET | `/api/v1/devices` | 当前用户的设备列表 |
| GET | `/api/v1/devices/tree?clientId=` | 单台设备保留目录结构的书签树 |
| DELETE | `/api/v1/devices?id=<clientId>` | 删除设备及其快照、来源；清理无人提供的书签 |
| GET | `/api/v1/snapshots?clientId=` | 快照列表（插件用，原始字段） |
| GET | `/api/v1/snapshots/manage?device=&limit=&offset=` | 管理端列表，带设备名与体积 |
| GET | `/api/v1/snapshot?id=` | 读取某次完整书签树（跨用户返回 404）；可加 `format=html\|csv\|json` 直接下载附件 |
| DELETE | `/api/v1/snapshot?id=\|ids=\|device=\|all=1` | 删除快照 |
| GET | `/api/v1/logs` | 最近 100 条日志 |
| GET | `/api/v1/export?format=html\|csv\|json&device=` | 导出去重后的书签，`device` 按来源设备过滤；三种格式见下方「导出格式」 |

`status` 示例：

```json
{
  "ok": true, "devices": 2, "bookmarks": 42, "rawBookmarks": 60,
  "duplicatesRemoved": 18, "trash": 1, "snapshots": 12,
  "lastSync": "…", "serverTime": "…", "version": "1.4.0"
}
```

### 导出格式

`GET /api/v1/export` 导出当前用户的**去重后全部书签**（可加 `device=<clientId>` 只导某台设备提供过的书签）；`GET /api/v1/snapshot?id=<n>&format=…` 导出**某一份历史快照**的原始目录树。两者共用同一套 `format`：

| format | Content-Type | 文件名 | 用途 |
| --- | --- | --- | --- |
| `html`（默认） | `text/html; charset=utf-8` | `bookmarks-<时间>.html` / `snapshot-<id>-<时间>.html` | Netscape 书签文件，Chromium 系浏览器 / Firefox 可直接「导入书签」；保留目录层级 |
| `csv` | `text/csv; charset=utf-8` | `bookmarks-<时间>.csv` / `snapshot-<id>-<时间>.csv` | 通用表格：标题 / 网址 / 目录 / 来源设备 / 添加时间；带 UTF-8 BOM，Excel 打开不乱码 |
| `json` | `application/json; charset=utf-8` | `bookmarks-<时间>.json` / `snapshot-<id>-<时间>.json` | 完整结构（`items` + `tree`），便于程序处理与备份 |

- `format` 传其他值返回 `400`；三种响应都带 `Content-Disposition: attachment`。
- HTML 的目录层级与 CSV 的「目录」列来自原始 `path`（快照来自快照树本身）；JSON 另外包含扁平 `items`。
- `GET /api/v1/snapshot?id=<n>` **不带** `format` 时仍返回 `{"ok":true,"tree":[…]}`，供管理端预览弹窗使用。
- 管理端「我的设备」「书签浏览」「设备详情」的导出按钮，以及「快照」列表与预览弹窗，都通过下拉选择这三种格式。

---

## 8. 设备管理页与只读书签浏览

### GET `/api/v1/devices/tree?clientId=…` · 需登录

单台设备的书签，**保留它同步时的目录层级**，供「设备 → 点进某台设备」的管理页使用。

```json
{
  "ok": true,
  "device": {
    "user_id": 1, "client_id": "uuid", "name": "书房电脑",
    "browser": "Microsoft Edge 131", "last_sync": "2026-11-25T10:22:31",
    "bookmark_count": 3, "revision": 4, "created_at": "2026-11-01T09:00:00"
  },
  "total": 3,
  "items": [
    {"id": 12, "url": "https://github.com", "url_key": "github.com",
     "title": "GitHub", "path": ["工作", "开发"], "date_added": 1726000000,
     "source": "sync", "device_name": "书房电脑", "devices": ["书房电脑"]}
  ],
  "tree": [{"title": "工作", "children": [{"title": "开发", "children": [
    {"id": 12, "title": "GitHub", "url": "https://github.com", "dateAdded": 1726000000,
     "device": "书房电脑", "devices": ["书房电脑"], "source": "sync"}
  ]}]}],
  "snapshots": [{"id": 7, "client_id": "uuid", "created_at": "2026-11-25T10:20:00", "count": 3}]
}
```

- 缺 `clientId` 返回 `400`；设备不存在或属于其他用户返回 `404`。
- `device` 是 `devices` 表的原始行（**蛇形命名**，与 `/api/v1/devices` 返回的条目一致）。
- `items` 是扁平列表（带 `path`），前端据此重建树以支持勾选与批量操作；`tree` 是同结构的现成树（叶子用驼峰 `dateAdded`），可直接渲染。
- 标题取 `COALESCE(source.title, bookmark.title)`：管理端改过标题就显示改后的值，路径始终取**该设备**的来源记录。
- 同一条书签被多台设备提供过时，本接口只返回该设备记录的 path，与其他设备互不影响。

### GET `/api/v1/browse?device=<clientId>` · 需登录

「书签浏览」页用的只读视图：**当前用户全部设备的去重书签**，并标记每条来自哪些设备。

```json
{
  "ok": true,
  "total": 42,
  "devices": [{"clientId": "uuid", "name": "书房电脑", "count": 30, "lastSync": "…"}],
  "tree": [
    {"title": "工作", "children": [
      {"title": "GitHub", "url": "https://github.com", "id": 12, "devices": ["书房电脑", "笔记本"]}
    ]}
  ]
}
```

- `device` 缺省为全部设备；传未知 `clientId` 返回 `404`。
- `devices[].count` 是该设备提供的书签条数，用于渲染顶部设备标签。
- 叶子节点的 `devices` 为来源设备名数组（可能多个），前端渲染成多个设备角标。
- 本接口**不提供任何写操作**，管理动作请走第 5 节的增删改查与 `bookmarks/batch`。

---

## 9. 快照管理

### GET `/api/v1/snapshots/manage?device=&limit=&offset=` · 需登录

管理端「快照」菜单的数据源。与插件用的 `/api/v1/snapshots` 分开，避免破坏旧插件的字段形状。

```json
{
  "ok": true, "total": 12,
  "devices": [{"clientId": "uuid", "name": "书房电脑"}],
  "items": [
    {"id": 27, "client_id": "uuid", "deviceName": "书房电脑",
     "created_at": "2026-11-25T14:10:00", "count": 3, "size": 812}
  ]
}
```

- `device` 按设备筛选；`limit` 默认 100、上限 200，`offset` 用于分页；非法的 `limit`/`offset` 返回 `400`。
- `deviceName` 取自 `devices` 表；设备已被删除但快照还在时显示「已删除的设备」。
- `size` 是快照 JSON 的字节数，供界面显示体积。

### DELETE `/api/v1/snapshot` · 需登录

四种删法（可组合，`all=1` 优先级最高）：

| 参数 | 说明 |
| --- | --- |
| `?id=27` | 删除一份 |
| `?ids=27,28,29` | 批量删除 |
| `?device=<clientId>` | 清空某台设备的全部快照 |
| `?all=1`（也接受 `true` / `yes`） | 清空当前用户的全部快照 |

```json
{"ok": true, "deleted": 3, "hint": "快照已删除；当前书签不受影响，设备下次同步会生成新快照"}
```

- 四个参数都不带返回 `400`；跨用户 id 静默跳过，不会报 404。
- 删除**只影响历史快照**，`bookmarks` / `bookmark_sources` / 回收站都不动；设备下次同步会自动产生新快照。
- 操作会写一条 `WARN` 日志。

---

## 10. CORS

```
# 请求带 Origin 时（例如 chrome-extension://<id>）
Access-Control-Allow-Origin: chrome-extension://<id>
Access-Control-Allow-Credentials: true
Vary: Origin
Access-Control-Allow-Methods: GET, POST, PUT, PATCH, DELETE, OPTIONS
Access-Control-Allow-Headers: Authorization, Content-Type, X-Auth-Token

# 无 Origin 的普通请求
Access-Control-Allow-Origin: *
```

回显具体 `Origin` 并允许 `credentials`，以便插件走飞牛统一网关时携带 NAS 会话 Cookie。本服务鉴权只认 `Authorization: Bearer`，不依赖 Cookie，因此回显任意 Origin 不会引入 CSRF 风险。`OPTIONS` 直接返回 `204`。插件来源 `chrome-extension://…` 可直接调用。

---

## 11. curl 示例

下面示例假设服务在 `http://192.0.2.10:8765`，账号 `admin` / 密码 `Example@123456`（请换成你自己的地址与密码）。

```bash
# 登录
curl -X POST http://192.0.2.10:8765/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"Example@123456"}'

# 复制插件配置
curl -H 'Authorization: Bearer <token>' \
  http://192.0.2.10:8765/api/v1/plugin-config

# 新增书签
curl -X POST -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' \
  -d '{"title":"示例","url":"https://example.com/"}' \
  http://192.0.2.10:8765/api/v1/bookmarks

# 用 Basic 直接查看去重后列表
curl -u admin:Example@123456 http://192.0.2.10:8765/api/v1/bookmarks

# 某台设备的目录树 / 全部设备的只读浏览
curl -H 'Authorization: Bearer <token>' \
  'http://192.0.2.10:8765/api/v1/devices/tree?clientId=<uuid>'
curl -H 'Authorization: Bearer <token>' 'http://192.0.2.10:8765/api/v1/browse'

# 批量移动 / 一键清空回收站
curl -X POST -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' \
  -d '{"ids":[12,13],"path":["工作","项目"]}' \
  http://192.0.2.10:8765/api/v1/bookmarks/batch
curl -X DELETE -H 'Authorization: Bearer <token>' \
  'http://192.0.2.10:8765/api/v1/bookmarks/trash?all=1'

# 按设备拉取 / 快照列表 / 删除快照
curl -H 'Authorization: Bearer <token>' \
  'http://192.0.2.10:8765/api/v1/incoming?exclude=<self-uuid>&device=<other-uuid>'
curl -H 'Authorization: Bearer <token>' \
  'http://192.0.2.10:8765/api/v1/snapshots/manage?limit=20&offset=0'
curl -X DELETE -H 'Authorization: Bearer <token>' \
  'http://192.0.2.10:8765/api/v1/snapshot?ids=27,28'

# 三种通用导出格式（HTML / CSV / JSON）
curl -OJ -H 'Authorization: Bearer <token>' \
  'http://192.0.2.10:8765/api/v1/export?format=csv'
curl -OJ -H 'Authorization: Bearer <token>' \
  'http://192.0.2.10:8765/api/v1/export?format=html&device=<uuid>'
curl -OJ -H 'Authorization: Bearer <token>' \
  'http://192.0.2.10:8765/api/v1/snapshot?id=27&format=csv'
curl -OJ -H 'Authorization: Bearer <token>' \
  'http://192.0.2.10:8765/api/v1/snapshot?id=27&format=html'
```
