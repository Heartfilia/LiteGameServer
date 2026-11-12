# LiteGame 前端接入协议 v1

> **供 `~/code/JScode/LiteGame` 的开发者或代码助手直接阅读。** 服务端已实现，当前 LiteGame 尚未接入。此文定义接口和客户端职责；API 不会主动读取或改动浏览器存储。

## 1. 目标、现有存储与接入位置

用户在游戏厅“游戏服务器”入口输入服务器地址；前端发现协议，首次要求已创建的用户名、密码，登录后自动同步，离线时继续本地游玩。没有网页注册功能。

已调研的 LiteGame 结构：

| 文件/存储 | 用途与接入建议 |
| --- | --- |
| `index.html` | 新增服务器连接、登录、同步状态与冲突入口 |
| `assets/js/main.js` | 当前大厅备份枚举 `litegame.`、`chogan.`；复用格式但不能把连接信息纳入备份 |
| `assets/js/favorites.js` | `litegame.favorites`，值为 JSON 字符串；云端下发后触发 `favoriteschange` 或重新加载 |
| `games/lib/chogan.js` | 多款游戏的共享存储，可接入写入通知 |
| `games/lib/campaign-save.js` | 版本化剧情存档与 `.backup`，已有本地冲突保护；下发后宜提示安全重载而非中途替换运行状态 |
| 各游戏独立 HTML/JS | 游戏页面通常与大厅不是同一 document，必须引入共享同步模块，不要只在首页启用 |
| `games/nes/nes.js` | IndexedDB `litegame-nes` / object store `states`，以卡带 SHA-256 为键；在保存、导入后通知同步，在恢复前检查云端 |

普通记录支持原始字符串，不为每个游戏定义数据表。NES 通过独立接口保存完整 snapshot；不上传 ROM、游戏资源或正在运行的 CPU 帧流，只同步用户已保存的即时存档。

建议新增共享 `assets/js/server-sync.js`，由大厅和需要同步的游戏页面加载。不要把 sessionStorage 中的搜索、滚动位置等临时 UI 状态上传。

## 2. 地址发现与认证

### 地址规则

用户输入例如 `https://save.example.com`，去除末尾 `/`，验证为 HTTP(S) origin，拒绝 userinfo、路径、query 和 fragment。**公网必须 HTTPS**，只在本机开发使用 HTTP。HTTPS 游戏站点不能连接普通 HTTP API（混合内容）。

当前服务按域名根路径部署，不支持用户填写任意 `/prefix`。不要求用户手填 `/api/v1`，前端访问固定发现入口：

```http
GET /api/v1/info
```

示例响应：

```json
{
  "service": "litegame-sync",
  "version": "1.0.0",
  "protocol_version": 1,
  "api_base": "/api/v1",
  "auth": "password-bearer",
  "registration": "cli-only",
  "features": ["local-storage", "nes-snapshots", "optimistic-concurrency", "deletion-tombstones"],
  "limits": {
    "body_bytes": 16777216,
    "record_bytes": 1048576,
    "nes_bytes": 12582912,
    "user_bytes": 67108864,
    "record_count": 5000,
    "batch_count": 100
  }
}
```

验证 `service` 和 `protocol_version === 1` 后再发登录请求。版本不支持应明确提示升级，不盲目发送密码。生产环境不要自动跟随跨 origin 地址变更传递密码/令牌。

### 登录

```http
POST /api/v1/auth/login
Content-Type: application/json

{"username":"我的用户名","password":"至少十二个字符的密码"}
```

```json
{
  "access_token": "不透明的随机令牌",
  "token_type": "bearer",
  "expires_at": 1797033600,
  "user": {"id": 1, "username": "我的用户名"}
}
```

`expires_at` 是 Unix **秒**，不是毫秒。默认 30 天，固定有效期；没有 refresh token。后续请求携带：

```http
Authorization: Bearer 不透明的随机令牌
```

不使用 cookie，fetch 无需 `credentials: 'include'`。前端不保存密码；令牌只用于当前服务器/账户。可以仅保存在内存／sessionStorage，或在明确告知用户后“记住登录”持久化；任何可读令牌存储都要考虑同源脚本/XSS 风险。

**连接地址、token、用户身份、device id、同步基线、游标请使用独立前缀 `lgSync.v1.` 或单独 IndexedDB。不要使用 `litegame.` 或 `chogan.`，否则现有备份功能会泄漏这些信息。** 后端还拒绝 `litegame.sync`、`litegame.server`、`litegame.auth`、`litegame.connection`、`litegame.device` 及其以 `.` 分隔的子键；不代表其他错误命名的敏感键会被自动识别。

### 当前用户与退出

```http
GET /api/v1/auth/me
Authorization: Bearer ...
```

```json
{"id":1,"username":"我的用户名","revision":12,"usage_bytes":2048,"record_count":8}
```

```http
POST /api/v1/auth/logout
Authorization: Bearer ...
```

退出响应 `{"ok":true}`，该令牌立即失效。客户端停止任务并清除令牌；**不要顺手删除本地游戏记录或上传全量删除**。本地记录仍可离线使用。切换账户/服务器必须使用不同同步命名空间，并让用户明确选择是否把已有本地数据导入新账户，不能自动混用。

每次重新登录后先全量核对（`since=0`），再恢复自动同步。这兼容管理员从备份恢复数据后修订号回退的情况。401 应停止自动上传并要求登录，不要无限重试密码。

## 3. 普通记录、版本与删除

### 记录格式

```json
{
  "kind": "local",
  "key": "litegame.favorites",
  "value": "{\"version\":1,\"ids\":[\"jigsaw\"]}",
  "value_bytes": 30,
  "version": 7,
  "deleted": false,
  "updated_at": "2026-11-12T12:00:00.000Z"
}
```

- `kind`: `local` 或 `nes`。
- `value` 是 `localStorage.getItem(key)` 的**原始字符串**，不要先解析成对象；服务端不改写它。
- `value_bytes` 是服务端实际保存值的 UTF-8 字节数；上例大小仅为说明，实际以响应计算值为准。
- `version` 是这个账户的单调递增修订号。一次批量写多条时每条获得不同版本；不要求同一键版本连续。
- `updated_at` 由服务端生成，仅展示，不用设备时间决定胜负。
- `value: ""` 是合法空字符串；`value: null` 是**显式删除**。
- 删除保留 tombstone（`deleted: true`、`value: null`），同样有版本。

允许普通键：以 `litegame.`、`chogan.` 开头、长度 1–256 个字符、非空后缀、无 ASCII 控制字符，并排除上述保留键。值限额通常 1 MiB。

### 拉取（普通记录 + NES 元数据共用增量游标）

```http
GET /api/v1/records?since=0&limit=50
Authorization: Bearer ...
```

`since` 非负整数，默认 0；`limit` 1–100，默认 50。响应：

```json
{
  "records": [
    {"kind":"local","key":"litegame.favorites","value":"{\"version\":1,\"ids\":[]}","value_bytes":22,"version":7,"deleted":false,"updated_at":"2026-11-12T12:00:00.000Z"},
    {"kind":"local","key":"chogan.example.best","value":null,"value_bytes":0,"version":8,"deleted":true,"updated_at":"2026-11-12T12:01:00.000Z"},
    {"kind":"nes","key":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","value_bytes":6000000,"version":9,"deleted":false,"updated_at":"2026-11-12T12:02:00.000Z"}
  ],
  "next_cursor": 9,
  "has_more": false,
  "revision": 9
}
```

按版本升序返回每条记录的**最新状态**，不是操作历史。即使同一键改过很多次也只返回最新值。NES 仅返回元数据，不在列表里返回大存档；要额外 GET NES 接口。

处理并可靠持久化这一页（包括待处理冲突或待下载 NES）后，才能保存 `next_cursor`。`has_more` 为 true 时继续请求。最后一页无记录也正常。分页期间有并发更新时可能在后页再次看到同一键，用版本去重，最终可追上最新状态。

`since` 大于服务端修订号时返回 409 `cursor_invalid`：保留本地数据与待同步队列，清除游标及旧版本假设，以 `since=0` 全量重新核对，必要时让用户确认。不要因此清空云端。

### 批量提交普通记录

```http
POST /api/v1/records/batch
Authorization: Bearer ...
Content-Type: application/json

{
  "changes": [
    {"key":"litegame.favorites","value":"{\"version\":1,\"ids\":[\"jigsaw\"]}","expected_version":7},
    {"key":"chogan.example.best","value":null,"expected_version":8}
  ]
}
```

规则：

- 一批 1–100 条，同批不能重复键；按整个请求体限制分批，不能只按条数。
- 所有字段必须提供；`expected_version` 必须是非负整数，不能是字符串、布尔值。
- `expected_version: 0` 表示预期远端**从未有过这条键**，不表示“强制覆盖”。
- 已删除记录也有版本；重新创建必须携带删除标记的当前版本。
- 新键显式提交 null 也会创建删除标记，客户端不应无意义批量删键。
- **全批原子性**：任意一条冲突、超限或无效，整批不写入。
- 不支持通配清空或“上传一个对象就替换整个账户”。

成功响应：

```json
{
  "records": [
    {"kind":"local","key":"litegame.favorites","value":"{\"version\":1,\"ids\":[\"jigsaw\"]}","value_bytes":30,"version":10,"deleted":false,"updated_at":"2026-11-12T12:03:00.000Z"},
    {"kind":"local","key":"chogan.example.best","value":null,"value_bytes":0,"version":11,"deleted":true,"updated_at":"2026-11-12T12:03:00.000Z"}
  ],
  "revision":11
}
```

**重要：写响应的 `revision` 不是拉取游标，不能拿它覆盖本地 pull cursor。** 其他设备可能在上次拉取之后、这次写入之前修改过别的键。只有处理拉取响应后，才能推进 `next_cursor`，否则会漏掉别人改动。

### 冲突

```json
{
  "error": {
    "code": "version_conflict",
    "message": "No changes were applied",
    "conflicts": [
      {
        "key":"litegame.favorites",
        "current":{"kind":"local","key":"litegame.favorites","value":"云端当前原始字符串","value_bytes":27,"version":12,"deleted":false,"updated_at":"2026-11-12T12:04:00.000Z"}
      }
    ]
  }
}
```

HTTP 409。`current` 在远端从未存在时为 null。NES 冲突只返回元数据，需单独下载内容。前端保留本地副本与云端副本，提供：

- **使用云端**：安全写回本地/IndexedDB，更新基线；活动游戏宜提示重载。
- **保留本地**：用户确认后用最新远端版本再次提交；若期间再次变化，继续返回冲突，不能循环强推。
- **稍后处理**：持久化待处理冲突，暂停这一条键的自动上传，不影响其他独立记录。

若失败批中有不冲突的其他键，可去掉冲突项再提交。收藏的自动合并不是无脑取并集（会撤销用户取消收藏）；除非前端实现了有基线的可靠合并，否则统一让用户选择。

## 4. NES 独立存档接口

键是卡带 SHA-256，必须 **64 位小写十六进制**。来自现有 `nes.js` 的 snapshot：

```json
{
  "format":1,
  "emulator":"jsnes-2.1.0",
  "sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "title":"卡带标题",
  "savedAt":"2026-11-12T12:00:00.000Z",
  "state":{"cpu":{},"ppu":{},"mmap":{},"papu":{}}
}
```

这里只展示结构，真正上传时保留完整 emulator state。后端检查必要字段/大小/哈希一致性，但**不会运行模拟器验证全部内部状态**；下载后仍要使用现有 `applySnapshot` 的深度验证与卡带匹配检查。不要在载入错误卡带时强行套用。

### 保存

```http
PUT /api/v1/nes/{sha256}
Authorization: Bearer ...
Content-Type: application/json

{"expected_version":0,"snapshot":完整的snapshot对象}
```

响应与普通批量写入相同形状，但 `records` 只有一个 NES 元数据，没有 `value`，也不回传大存档。更新使用上次的远端版本。默认存档上限 12 MiB，整个 HTTP 请求上限 16 MiB。

### 下载

```http
GET /api/v1/nes/{sha256}
Authorization: Bearer ...
```

```json
{
  "kind":"nes",
  "key":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "version":13,
  "deleted":false,
  "value_bytes":6000000,
  "updated_at":"2026-11-12T12:00:00.000Z",
  "snapshot":{"format":1,"emulator":"jsnes-2.1.0","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","title":"卡带标题","savedAt":"2026-11-12T12:00:00.000Z","state":{"cpu":{},"ppu":{},"mmap":{},"papu":{}}}
}
```

从未保存时为 404 `not_found`；已删除为 **200**、`deleted: true`、`snapshot: null`，保留版本。下载时内容可能已比列表元数据更新，使用 GET 实际返回的版本，不要把旧列表版本配给新内容。

### 删除

```http
DELETE /api/v1/nes/{sha256}?expected_version=13
Authorization: Bearer ...
```

成功返回一个 NES 删除标记的写回执。不会删除 ROM 或其他存档。

## 5. 客户端同步算法（必须实现的约束）

### 本地同步元信息

按 **服务器 origin + 账户 ID** 隔离保存：

- pull cursor。
- 每条 `(kind,key)` 已核对的远端版本及共同基线（原始值/可靠摘要）。
- 本地变动队列、显式删除操作、未解决冲突。
- NES 待下载项与版本。

不要把正在显示的“云端时间”当共同基线，不要仅比较客户端时间。下载完整性可用 Web Crypto SHA-256 做本地摘要；服务端没有以客户端摘要作为授权依据。

### 首次连接 / 新设备 / 重新登录

1. 发现服务、登录，确认账户身份。
2. 拉取 `since=0` 全部页面，保留本地现有数据，不能先整体覆盖。
3. 本地没有、云端有：下载恢复；本地有、远端从未有：提示用户是否上传本机已有记录。
4. 两边值完全一致：建立基线；两边都存在但不同且没有共同基线：询问保留哪边。
5. 云端删除标记不能当“从未存在”；本地还有值时要核对，不能自动用 `expected_version=0` 复活。
6. 建好基线后启用后台同步。切换账户尤其要避免把上一个账户的本地存档自动上传给新账户。

### 正常同步

1. 从游戏存储写入点记录变更，防抖提交（例如 1–3 秒）；本地保存成功不依赖网络。
2. 定期增量拉取（例如活动时 15–30 秒）、回到前台、网络恢复时触发。多个标签页使用 Web Locks（支持时）或其他选主机制，防止重复调度。
3. 云端变化且本地未变：安全下发并更新基线。
4. 本地变化且云端未变：使用基线版本上传。
5. 两边都变：值相同可更新基线，否则记录冲突。
6. 下发写本地时加“正在应用云端”保护，避免被写入监听再次当成本地操作上传。
7. 上传期间本地可能继续改变：只对**这次发出的快照**确认完成，不能把更新后的本地值错误标记为已上传。
8. 发现/写入冲突或网络结果不明时重新拉取核对，HTTP 超时并不证明服务端没有提交；不要把重试冲突当数据损坏。

`storage` 事件只通知**其他**同源 document，同一个页面调用 `localStorage.setItem` 不会收到自身事件；IndexedDB 也不会触发它。建议在共享存储层/明确写入点通知同步，辅以低频快照扫描。不要只监听大厅事件，不要把所有游戏页面都当成嵌在大厅内。`localStorage` 全量扫描发现缺失也不能直接视为删除：清缓存、新设备、隐私模式可能导致本地空库。

### 删除和运行中恢复

只有明确的用户操作或可信已跟踪删除才上传 tombstone。清除连接配置、退出登录、首次空库、浏览器存储异常不能产生云端删除。

下发普通记录时 `deleted` 为 true 才调用 `removeItem`，其他情况保存原始字符串。涉及运行中的剧情、棋局、模拟器应先保存本地副本、提示重载/安全读档，不能仅改底层存储而让内存状态下一秒覆盖云端恢复值。现有 CampaignSave 有 expected 值校验，尊重其锁定与备份机制。

### 离线与错误重试

- 网络错误、5xx：保留队列，指数退避并加抖动，联网/前台时再试。
- 401：暂停并请求登录；不自动保存/重发密码。
- 409：进入冲突/全量核对流程，不自动换成远端版本强推。
- 413 / 422：提示具体问题，暂停失败项，不无限重试。注意 HTTP body 限制包括双重 JSON 转义。
- 429：遵守 `Retry-After`（应用登录限流返回秒数；代理错误可能不含此头）。
- 页面关闭：不依赖最后一次 fetch 一定完成；应把队列持久化。`sendBeacon` 不能设置本协议需要的 Bearer 请求头，不要把令牌改塞 URL。

## 6. fetch 示例

下例只是协议调用示例，**不是完整同步模块**，不能替代基线、队列及冲突 UI。

```js
function normalizeServer(input) {
  const url = new URL(input.trim());
  if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password ||
      url.search || url.hash || !['', '/'].includes(url.pathname)) {
    throw new Error('请输入服务器 origin，例如 https://save.example.com');
  }
  return url.origin;
}

async function call(server, path, { token, method = 'GET', body } = {}) {
  const response = await fetch(server + path, {
    method,
    credentials: 'omit',
    redirect: 'error',
    cache: 'no-store',
    headers: {
      ...(token ? { Authorization: 'Bearer ' + token } : {}),
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {})
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    signal: AbortSignal.timeout(30000)
  });
  const result = await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error(result?.error?.message || 'HTTP ' + response.status);
    error.status = response.status;
    error.detail = result?.error;
    error.retryAfter = response.headers.get('Retry-After');
    throw error;
  }
  return result;
}

const server = normalizeServer('https://save.example.com');
const info = await call(server, '/api/v1/info');
if (info.service !== 'litegame-sync' || info.protocol_version !== 1) {
  throw new Error('不兼容的同步服务');
}
const session = await call(server, '/api/v1/auth/login', {
  method: 'POST', body: { username: '已在服务器创建的用户名', password: '用户输入的密码' }
});
const token = session.access_token; // 不记录日志，不进入游戏备份。

// 只有在确认首次导入并处理同名冲突后才上传；这里示范一个不存在的新键。
const receipt = await call(server, '/api/v1/records/batch', {
  token, method: 'POST',
  body: { changes: [{ key: 'litegame.example', value: '{"score":42}', expected_version: 0 }] }
});
// receipt.revision 不能作为 pull cursor！

let cursor = 0;
let more;
do {
  const page = await call(server, '/api/v1/records?since=' + cursor + '&limit=50', { token });
  // await reconcileAndPersist(page.records); // 实际实现：基线核对、冲突、NES 下载等。
  cursor = page.next_cursor; // 必须在上述处理可靠保存后再持久化游标。
  more = page.has_more;
} while (more);
```

应用 CORS 开放 `Authorization`、`Content-Type` 请求头，以及 GET/POST/PUT/DELETE（中间件处理 OPTIONS），并暴露 `Retry-After` 响应头。前端遇到 CORS 报错应要求部署者检查来源白名单，不使用 `mode: 'no-cors'`，它无法读取接口响应。

## 7. 错误约定与接口总表

应用错误统一为：

```json
{"error":{"code":"稳定的机器错误码","message":"说明","其他字段":"按错误类型提供"}}
```

验证错误的 `fields` 只包含位置、类型和说明，不回显输入。Nginx、CDN、网络层可能返回非 JSON，所以客户端要保留 HTTP 状态并兼容解析失败。

| HTTP | code | 含义 |
| --- | --- | --- |
| 401 | `unauthorized` / `invalid_credentials` | 缺少、失效登录或登录失败 |
| 404 | `not_found` | 对应 NES 从未保存 |
| 409 | `version_conflict` | 记录版本不匹配，全批未提交 |
| 409 | `cursor_invalid` | 游标领先于服务器，需要全量核对 |
| 413 | `body_too_large` | 整个 HTTP 请求过大 |
| 413 | `record_too_large` | 单条记录/存档过大 |
| 413 | `quota_exceeded` | 账户值容量或记录数量超限 |
| 422 | `validation_error` / `sha256_mismatch` / `invalid_snapshot` | 请求不符合协议 |
| 429 | `login_rate_limited` | 登录尝试过多，带 Retry-After |

| 方法 | 路径 | 认证 | 用途 |
| --- | --- | --- | --- |
| GET | `/healthz` | 否 | 最小健康检查 |
| GET | `/api/v1/info` | 否 | 服务与协议发现 |
| POST | `/api/v1/auth/login` | 否 | 登录 |
| POST | `/api/v1/auth/logout` | 是 | 撤销当前令牌 |
| GET | `/api/v1/auth/me` | 是 | 身份、容量与修订号 |
| GET | `/api/v1/records` | 是 | 增量列表，包含 NES 元数据 |
| POST | `/api/v1/records/batch` | 是 | 原子批量普通记录变更 |
| GET | `/api/v1/nes/{sha256}` | 是 | 获取 NES 快照或删除标记 |
| PUT | `/api/v1/nes/{sha256}` | 是 | 保存 NES 快照 |
| DELETE | `/api/v1/nes/{sha256}` | 是 | 显式删除，query 必填 expected_version |
| GET | `/docs`, `/redoc`, `/openapi.json` | 否 | 自动接口文档，不包含存档数据 |

**没有 `/register` 或 HTTP 用户管理接口。** 管理员在部署机器执行 `sudo litegame-server user add "用户名"`。

## 8. 前端接入验收清单

- [ ] 未连接服务器时，现有离线游玩、收藏、备份功能不受影响。
- [ ] 输入 origin 能发现服务；错误地址/协议版本有明确提示。
- [ ] 只有服务器 CLI 创建的用户能登录，无注册入口。
- [ ] 首次空浏览器不会清空云端；首次已有数据先核对再导入。
- [ ] 游戏独立页面也会记录并同步变化，不仅首页生效。
- [ ] 两台设备更新不同键不会丢数据；同一键冲突有选择 UI。
- [ ] 退出/禁用/重置密码后，旧令牌不能继续上传。
- [ ] 请求超时后能核对已提交数据，离线队列不会丢。
- [ ] 本地删除与云端删除正确处理，不会被旧设备复活。
- [ ] NES 用正确卡带 SHA-256 及完整 snapshot，下载后仍验证再恢复。
- [ ] token、服务器配置、同步元信息不进入导出备份或云端游戏记录。
- [ ] 恢复备份、重新登录、切换账户都进行安全的全量核对。
