# Fn收藏夹同步

> 把 Chromium 系浏览器（Edge / Chrome / Brave / Vivaldi / Opera 等）的收藏夹自动同步到你自己的飞牛 OS（fnOS）NAS。浏览器侧是 Manifest V3 插件，NAS 侧是零第三方依赖的 Python 标准库服务，打包为 `.fpk`。数据只存在本机 SQLite，多用户各自独立空间。

| | |
| --- | --- |
| 当前版本 | **v1.4.0**（界面现代化 · 代码精简 · 开源发布） |
| 开发者 | **oldkevin** |
| 协议 | MIT（见 [LICENSE](LICENSE)） |
| 产品名 | Fn收藏夹同步（内部标识 `edge-bookmarks` 保持不变） |

---

## 特性亮点

| 特性 | 说明 |
| --- | --- |
| 账号体系 | 首次注册管理员，管理员增删用户 / 重置密码；用户名 + 密码登录，PBKDF2-HMAC-SHA256 加盐迭代哈希，30 天会话，兼容 HTTP Basic；数据按 `user_id` 完全隔离 |
| 一键连接 | 管理端按当前访问地址自动推算插件配置，一键复制（不含密码）；插件设置页粘贴即解析，只需再输一次密码 |
| 自动去重 | 忽略协议 / 大小写 / 默认端口 / 末尾斜杠 / `#锚点` / `utm_*` 跟踪参数 / 查询串顺序；`bookmarks(user_id, url_key)` 唯一，`bookmark_sources` 记录来源设备 |
| 设备管理 | 点进单台设备保留原目录树，文件夹勾选联动子项 + 半选、全选、批量改文件夹（转手动维护）、批量删除、回收站一键清空 |
| 书签浏览 | 只读视图，设备 chips 筛选、目录树、多设备来源角标 |
| 书签管理 | 增删改查、分页、搜索、回收站恢复 |
| 快照 | 每台设备保留最近 10 份；管理端按设备筛选 / 分页 / 目录树预览 / 删除；插件可一键拉回本机历史备份（跳过已存在 URL） |
| 插件能力 | 立即同步、定时 / 变动触发、按设备标签拉取其他设备书签、同步策略独立保存、配置一键导入、内外网多地址探测降级 |
| 多格式导出 | HTML（Netscape 书签）/ CSV（UTF-8 BOM）/ JSON；设备、浏览、快照均支持 |
| 三条通道 | TCP 8765 直连、Unix Socket `app.sock` 走飞牛统一网关（带 NAS 会话 Cookie）、自建反代前缀 |
| 多浏览器 | UA 识别真实浏览器品牌命名设备，MV3 service worker，最低内核 91 |

### 界面预览

> 截图占位（待补充）：管理端首页 · 设备管理页 · 书签浏览 · 快照预览 · 插件弹窗与设置页

---

## 快速开始

### 1. NAS 安装

1. 应用中心搜索并安装官方 **`python312`** 运行时（fnOS 默认不预装 Python 3）。
2. 应用中心 →「手动安装」→ 上传 `dist/edge-bookmarks_1.4.0_all.fpk` → 选择安装空间 → 启动。
   - 若因端口声明安装失败，改用 `edge-bookmarks_1.4.0_all_noport.fpk` 对照包。
   - 系统不识别 `platform=all` 时改用 `edge-bookmarks_1.4.0_x86.fpk` / `edge-bookmarks_1.4.0_arm.fpk`。
3. 桌面只保留**一个**图标「Fn收藏夹同步」，点开自动弹出新页面。

> 安装报错见 [安装失败排查](docs/安装失败排查.md)；装好了点图标没反应见 [点开无反应排查](docs/点开无反应排查.md)。

### 2. 注册管理员

第一次打开显示「首次使用 · 注册管理员」，填写用户名、显示名、密码（2–32 位用户名，6–128 位密码），创建后自动登录。注册入口只在没有任何用户时出现，且只能成功一次。之后在「用户管理」给其他人开账号。

### 3. 浏览器安装插件

1. 解压 `dist/edge-bookmarks-extension_1.4.0.zip` 到**固定目录**（不要放在临时目录，加载后不能移动）。
2. `chrome://extensions`（Edge 亦支持 `edge://extensions`）→ 开启「开发人员模式」→「加载解压缩的扩展」→ 选择解压目录。Vivaldi / Opera 用 `vivaldi://extensions` / `opera://extensions`。各浏览器入口与注意点见 [多浏览器支持](docs/多浏览器支持.md)。

### 4. 复制配置并粘贴

管理端展开「连接浏览器插件」卡片 → 点「复制插件配置」。配置形如（**不含密码**）：

```json
{
  "serverUrl": "https://nas.example.com:9443/app/edge-bookmarks",
  "serverUrlAlt": "http://192.0.2.10:8765",
  "serverUrls": ["https://nas.example.com:8765"],
  "username": "admin"
}
```

主地址恒等于你地址栏里的那个地址。插件按「上次可用 → 主地址 → 备用地址 → 其余候选」逐个探测 `/api/v1/health`，任一返回 JSON 即用，无需用户判断该填哪个。

### 5. 首次同步

插件设置页 **① 粘贴插件配置**（自动解析）→ **② 输入本应用密码** →「保存并立即同步」。成功后弹窗显示「已同步」，Web 端「设备」页出现这台设备。

---

## 核心概念

### 去重规则

| 判定 | 例子 |
| --- | --- |
| 视为同一网址 | `https://github.com/`、`http://github.com#top`、`HTTPS://GITHUB.COM` |
| 视为同一网址 | `https://example.com/?utm_source=mail` 与 `https://example.com/` |
| 视为同一网址 | `https://example.com/?b=2&a=1` 与 `https://example.com/?a=1&b=2` |
| 视为不同网址 | `https://example.com/?a=1` 与 `https://example.com/?a=2`；不同路径或非默认端口 |

- 同步时先对单次上传去重，再与云端合并；已存在的记录不覆盖标题，只刷新时间。
- 浏览器里删掉的书签，云端同步后镜像移除；手动添加的不受影响。
- 删除设备只清理「没有其他设备提供」的书签，不会误删共用网址。

### 设备

每个浏览器配置文件（clientId）是一台独立设备，UA 识别出的真实浏览器品牌作为默认名，设置页可手动改名。一台电脑装 Edge + Chrome 会出现两条，这是设计如此。

### 快照

每次同步自动生成一份该设备的完整书签树快照，每台设备保留最近 10 份。可预览 / 下载 / 删除；插件「更多操作 → 一键拉回」把某份本机历史备份整棵还原到浏览器，自动跳过已存在网址。

### 手动维护

在管理端新增、编辑、批量改文件夹的书签会转为 `manual` 来源，浏览器下次同步不会覆盖。删除进回收站，可恢复；「彻底移除」后允许下次同步重新带回。

---

## 界面导览

| 菜单 | 作用 |
| --- | --- |
| 连接浏览器插件 | 按当前地址自动推算候选地址，一键复制不含密码的配置（默认折叠成一行） |
| 设备 | 设备列表 → 点进单台设备管理页：目录树勾选、全选、批量改文件夹、批量删除、单条编辑、设备视角回收站、导出 |
| 书签浏览 | **只读**：设备 chips 筛选 + 目录树，每条书签标注来源设备（多台来源显示多个角标） |
| 书签管理 | 全局增删改查、搜索、分页、新增、编辑、单条 / 批量删除、回收站恢复与一键清空 |
| 快照 | 按设备筛选 / 分页 / 目录树预览 / HTML·CSV·JSON 下载 / 单份·批量·按设备·全部删除；顶栏快照数可点击直达 |
| 用户管理 | 仅管理员可见：增删用户、重置密码、角色管理 |
| 设置 | 同步策略说明、历史快照入口、使用提示（同步策略主要在插件设置页调整） |

顶栏是一条合并统计：设备数 / 书签数 / 快照数 / 最近同步时间。

---

## 网络与网关

服务同时监听三条通道，数据完全一致：

| 通道 | 基址 | 使用者 | 鉴权 |
| --- | --- | --- | --- |
| TCP 直连 | `http://<NAS-IP>:8765` | 插件、局域网访问 | Bearer 会话令牌（兼容 Basic） |
| 飞牛统一网关 | `https://<飞牛域名>/app/edge-bookmarks` | 桌面 Web 界面；插件也可候选 | 会话令牌 + 飞牛 NAS 登录态（插件带 Cookie） |
| 自建反代 | `https://<统一登录地址>/eb-api` | 插件（复用统一登录地址时） | Bearer 会话令牌 |

- 统一网关路径 `/app/edge-bookmarks` 由飞牛 nginx 管理，转发前校验 NAS 登录会话；未登录时网关返回 `invalid token`，插件判探测失败并自动降级局域网直连。
- 外网长期稳定使用：映射 8765，或在统一登录地址上配一条**不受飞牛应用网关登录校验**的独立反代 location。配置方法见 [反向代理与统一登录地址](docs/反向代理与统一登录地址.md)。
- 通道拓扑、前缀注入、身份头信任边界见 [统一网关与内外网](docs/统一网关与内外网.md) 与 [API](docs/API.md)。

> 安全提醒：直连通道在 HTTP 上明文传输凭据，公网暴露 8765 前建议套 HTTPS 反代或限制来源 IP。

---

## FAQ

| 现象 | 处理 |
| --- | --- |
| 点图标没反应 / 白屏 | 先 `http://<NAS-IP>:8765` 直接打开确认服务正常，再看 [点开无反应排查](docs/点开无反应排查.md)；整页白屏多为旧缓存，`Ctrl+F5` 强刷 |
| 安装报「无法创建应用程序目录」 | 先确认已装 `python312`，再看 [安装失败排查](docs/安装失败排查.md) |
| 设备树是空的 | 同步时浏览器里没有书签，或已被删除 / 进回收站；用「书签浏览 → 全部」核对 |
| 修改文件夹后同步又变回去 | 改过的书签已转手动维护，正常不会覆盖；若被改回，检查是否又恢复了旧快照 |
| 插件提示 `invalid token` | 先在浏览器里打开并登录飞牛桌面再试；插件随后会自动尝试局域网直连 |
| 插件所有地址不可达 | 确认 8765 已放行、电脑与 NAS 同网段；外网需映射 8765 或配独立反代 |
| 旧插件提示 410 | 卸载旧插件，加载 `edge-bookmarks-extension_1.4.0.zip` |
| 还原后多出「本机历史备份」目录 | 预期行为，按时间命名；确认内容无误后可手动删掉该目录 |
| Firefox / Safari 能用吗 | 不能。仅支持 Chromium 内核（≥ 91），见 [多浏览器支持](docs/多浏览器支持.md) |

---

## 开发、构建与测试

```bash
# 构建
python tools/make_icons.py                            # 重新生成 LOGO
python tools/build_fpk.py                             # 主包 + 插件 zip（输出到 dist/）
python tools/build_fpk.py --platform x86 --no-zip     # 单架构包
python tools/build_fpk.py --platform arm --no-zip
python tools/build_fpk.py --no-port-config --no-zip   # 对照包（resource={}、无 .sc）

# 5 套自动化测试
python tests/smoke_test.py          # 226 项：端到端注册/登录/同步/去重/CRUD/设备树/浏览/隔离/地址识别
python tests/gateway_test.py        # 223 项：网关与反代前缀、静态检查、界面与还原能力（用 --gateway-test-port 模拟网关）
node tests/endpoint_test.mjs        #  58 项：插件端点探测、网关降级、会话失效重登与凭证复用
node tests/config_import_test.mjs   #  28 项：配置一键导入与多候选地址自动探测
python tests/fpk_lifecycle_test.py  # FPK 安装/启动/停止生命周期模拟、包内资源（需先构建 1.4.0 主包）
```

合计约 560 项断言。前四套无需打包即可运行；`fpk_lifecycle` 需先执行 `python tools/build_fpk.py` 生成 `dist/edge-bookmarks_1.4.0_all.fpk`。

更多贡献指引见 [CONTRIBUTING.md](CONTRIBUTING.md)，版本变更见 [CHANGELOG.md](CHANGELOG.md)。

### 目录结构

```
server/
  server.py              HTTP + SQLite + 去重 + CRUD + 设备树 + 地址推算（零第三方依赖）
  web/                   管理界面（设备管理 / 只读浏览 / 书签 CRUD / 快照）
extension/               浏览器插件（Manifest V3）
  shared.js              配置解析、账号登录、端点探测、书签工具
  background.js          定时/变动同步、其他设备回灌、本机历史备份还原
  options.* / popup.*    设置页与弹窗
fpk/
  manifest               版本 / 端口 / 桌面入口（内部标识不可改）
  cmd/main               安装钩子零副作用；start 负责启动与探活
  app/ui/config          唯一桌面图标（type=url，自动弹新页面）
  config/resource        port-config → EdgeBookmarks.sc（放行 8765/tcp）
tools/build_fpk.py       同步源码 → app.tgz → 校验 checksum → 打 .fpk / 插件 zip
tools/make_icons.py      生成 LOGO（16 / 32 / 48 / 64 / 128 / 256）
tests/                   5 套自动化测试
docs/                    部署、API、排障文档
dist/                    发布产物（FPK 与插件 zip）
```

### 产物

| 文件 | 用途 |
| --- | --- |
| `edge-bookmarks_1.4.0_all.fpk` | 主安装包（纯 Python，通吃 x86 / ARM，含端口放行） |
| `edge-bookmarks_1.4.0_all_noport.fpk` | 对照包：`resource={}`、无 `.sc`；仅当主包因端口声明安装失败时使用 |
| `edge-bookmarks_1.4.0_x86.fpk` / `..._arm.fpk` | 单架构包，系统不识别 `platform=all` 时换用 |
| `edge-bookmarks-extension_1.4.0.zip` | 浏览器插件分发包（Chromium 系通用） |

### 接口速览

完整接口参考见 [docs/API.md](docs/API.md)。除注册与登录外均需 `Authorization: Bearer <会话令牌>`（兼容 HTTP Basic）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/setup/register` | 首次注册管理员 |
| POST | `/api/v1/auth/login` | 用户名密码登录 |
| GET | `/api/v1/plugin-config` | 自动推算的插件地址 + 可复制配置 |
| GET | `/api/v1/devices/tree?clientId=` | 单设备信息 + 保结构目录树 + 快照 |
| GET | `/api/v1/browse?device=` | 只读书签浏览（带设备标记） |
| POST | `/api/v1/bookmarks/batch` | 批量改文件夹 `{ids, path}` |
| GET / POST | `/api/v1/bookmarks` | 列表（`q` / `limit` / `offset`）/ 新增 |
| PUT / PATCH | `/api/v1/bookmarks` | 编辑书签 |
| DELETE | `/api/v1/bookmarks?id=` 或 `?ids=` | 删除（进回收站） |
| GET | `/api/v1/bookmarks/trash` | 回收站列表 |
| DELETE | `/api/v1/bookmarks/trash?all=1` | 一键清空回收站 |
| POST | `/api/v1/bookmarks/restore` | 从回收站恢复 |
| GET | `/api/v1/snapshots?clientId=` | 该设备快照列表（插件还原用） |
| GET | `/api/v1/snapshots/manage?device=` | 管理端快照列表（带设备名与体积） |
| GET | `/api/v1/snapshot?id=` | 预览目录树；`&format=html\|csv\|json` 直接下载 |
| DELETE | `/api/v1/snapshot?id=\|ids=\|device=\|all=1` | 删除快照 |
| GET | `/api/v1/incoming?exclude=&device=` | 按设备拉取其他设备书签 |
| GET | `/api/v1/export?format=html\|csv\|json&device=` | 导出书签，`device` 按设备过滤 |
| POST | `/api/v1/sync` | 插件全量同步（自动去重） |

---

## 许可证

MIT License —— 详见 [LICENSE](LICENSE)。开发者：**oldkevin**。
