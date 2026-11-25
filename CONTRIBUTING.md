# 贡献指南

感谢你愿意改进 **Fn收藏夹同步**。本项目以 MIT 协议开源，开发者 **oldkevin**。本文说明如何本地运行、如何测试、代码风格与提交 PR 的流程。

## 一、本地跑服务端

服务端零第三方依赖，仅用 Python 3 标准库（`http.server` / `sqlite3` / `hashlib`）。最低 Python 3.8+（推荐 3.12）。

```bash
# 仓库根目录
python3 server/server.py --port 8765 --data ./data --config ./config.json
```

常用参数（均可用同名环境变量覆盖）：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--host` | `0.0.0.0` | 监听地址 |
| `--port` | `8765` | TCP 端口（插件 / 局域网） |
| `--data` | `./data` | SQLite 数据库目录 |
| `--config` | `./config.json` | 运行参数文件 |
| `--web` | `server/web` | 管理界面静态目录 |
| `--gateway-socket` | 空 | fnOS 统一网关 Unix Socket 路径（本地通常不填） |
| `--gateway-test-port` | `0` | **本地回归用**：用第二个 TCP 端口模拟网关通道 |
| `--print-setup` | - | 输出初始化状态 JSON 后退出（诊断用） |

启动后浏览器打开 `http://127.0.0.1:8765`，首次会提示注册管理员。健康检查：`curl http://127.0.0.1:8765/api/v1/health`。

> 本地 `data/` 与 `config.json` 含你的测试账号和书签，请勿提交；也不要把真实的个人数据提交到仓库。

## 二、运行测试

5 套自动化测试覆盖服务端、网关、插件逻辑与打包生命周期。前四套无需打包即可运行：

```bash
python3 tests/smoke_test.py          # 端到端：注册/登录/同步/去重/CRUD/设备树/浏览/隔离/地址识别（约 226 项）
python3 tests/gateway_test.py        # 网关与反代前缀、静态检查、界面与还原能力（约 223 项，用 --gateway-test-port 模拟网关）
node   tests/endpoint_test.mjs       # 插件端点探测、网关降级、会话失效重登与凭证复用（约 58 项）
node   tests/config_import_test.mjs  # 配置一键导入与多候选地址自动探测（约 28 项）

python3 tools/build_fpk.py           # 先生成 dist/edge-bookmarks_1.4.0_all.fpk
python3 tests/fpk_lifecycle_test.py  # FPK 安装/启动/停止生命周期模拟、包内资源（依赖上一步产物）
```

| 测试 | 覆盖重点 |
| --- | --- |
| `smoke_test.py` | 自动拉起服务端，真实走 HTTP 接口，账号体系、去重、CRUD、多用户隔离 |
| `gateway_test.py` | 前缀路由、`<base>` 注入、网关身份头防伪、网关通道下的业务接口 |
| `endpoint_test.mjs` | Node 环境 mock `fetch` 与 `chrome.storage`，验证插件登录、重登、地址探测 |
| `config_import_test.mjs` | 管理端复制配置的解析、候选地址顺序、命中缓存 |
| `fpk_lifecycle_test.py` | 解包模拟 fnOS 安装钩子、启动、status、stop，校验零副作用与包内资源 |

> Python 测试会自行拉起子进程服务端，node 测试用 data URL 加载 `shared.js`，均不需要浏览器扩展环境。修改服务端路由或插件 `shared.js` 后，请把对应测试一并跑通。

## 三、构建产物

```bash
python3 tools/make_icons.py                          # 重新生成 LOGO
python3 tools/build_fpk.py                           # 主包 + 插件 zip → dist/
python3 tools/build_fpk.py --platform x86 --no-zip   # 单架构包
python3 tools/build_fpk.py --platform arm --no-zip
python3 tools/build_fpk.py --no-port-config --no-zip # 对照包（resource={}、无 .sc）
bash   tools/build.sh                                # 在 NAS / Linux / WSL 上优先用官方 fnpack
```

产物命名：`edge-bookmarks_1.4.0_<platform>.fpk`、`edge-bookmarks-extension_1.4.0.zip`。`dist/` 不忽略，发布时随仓库分发。

## 四、代码风格

| 范围 | 约定 |
| --- | --- |
| Python | 标准库优先，不引入 pip 依赖；4 空格缩进；`snake_case` 命名；UTF-8 编码；公开接口保持向后兼容 |
| JavaScript | ES module（`background.js` 为 MV3 module service worker）；`camelCase` 命名；只使用标准 `chrome.*` API |
| 接口 | 新增路由放 `server/server.py` 的 `do_GET` / `do_POST` / `_mutate` / `do_DELETE`；路径统一 `/api/v1/` 前缀；跨用户资源统一返回 404；变更同步更新 [docs/API.md](docs/API.md) |
| 文档 | 中文、结构化（表格优先）；示例域名用 `nas.example.com`，示例网段用 `192.0.2.x`，示例账号 `admin` / 密码 `Example@123456`；不要写真实人名、真实域名或真实 IP |
| 版本 | 发布新版本时同步更新 `fpk/manifest`、`extension/manifest.json`、`README.md`、`docs/`、`CHANGELOG.md` 中的版本号与产物名 |

## 五、内部标识不可改清单

以下标识属于 fnOS 应用识别与历史数据兼容的关键字段，**任何 PR 都不得修改**。产品显示名「Fn收藏夹同步」保持不变。

| 标识 | 位置 | 改动后果 |
| --- | --- | --- |
| `appname = edge-bookmarks` | `fpk/manifest`、桌面入口、插件配置 | fnOS 会当成新应用，旧书签数据留在原目录 |
| 路径前缀 `/app/edge-bookmarks` | 统一网关地址、`EB_GATEWAY_PREFIX`、反代规则、文档 | 同上；网关与反代全部失效 |
| `edgebm` 专用用户 / 组 | 安装钩子、数据卷属主 | 权限失效，服务无法读写数据 |
| `EdgeBookmarks.sc` | `config/resource` 端口放行 | 飞牛端口配置引用它，8765 不再放行 |
| `edge-bookmarks-extension_*.zip` | 产物文件名 | 分发与文档引用，造成版本混乱 |
| `bookmarks(user_id, url_key)` 唯一约束 | SQLite `bookmarks` 表 | 破坏去重与多用户隔离 |
| URL 规范化规则（`normalize_url` / `url_key`） | 服务端 | 破坏去重，导致同步循环或数据重复 |

> 品牌中立化只改面向用户的**文案**。如果你发现某处仍写死「Edge」，欢迎提交文案修正，但不要动上面任何内部标识。

## 六、提交 PR 流程

1. **开 Issue 讨论**：较大改动先开 Issue 说明问题与方案，避免重复劳动。
2. **Fork 并建分支**：基于 `main` 创建功能分支，如 `fix/plugin-endpoint-fallback`。
3. **小步提交**：每个提交只做一件事，提交信息用中文简述改动（如 `修复网关未登录时插件降级失效`）。
4. **跑通测试**：按第二节跑完相关测试；涉及打包改动时务必额外跑 `fpk_lifecycle_test.py`。
5. **不提交敏感内容**：不要提交 `data/`、`config.json`、真实书签导出、个人域名 / IP / 账号密码；`.gitignore` 已忽略常见临时文件与 `.workbuddy/`。
6. **发 PR**：PR 描述里写清楚「改了什么 / 为什么 / 怎么验证」，引用相关 Issue；确认 CI / 测试通过。
7. **审阅**：维护者会在可读性、兼容性、内部标识清单三方面审阅；可能需要你补充测试或文档。

## 七、报告问题

报 Bug 时请附：版本号、浏览器与内核版本、fnOS 版本、复现步骤、相关日志（`$TRIM_PKGVAR/server.log`、`STARTUP_STATUS.txt`），并**先脱敏**真实域名、IP、账号。安装类问题对照 [安装失败排查](docs/安装失败排查.md)，点图标无反应对照 [点开无反应排查](docs/点开无反应排查.md)。
