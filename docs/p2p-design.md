# P2P 直传分享 —— 设计方案

> 状态：**设计评审中，尚未实现**
> 目标版本：待定（基于 2.5.6 分支）
> 关联 fork：后端 `ayflying/FileCodeBox`，前端 `ayflying/FileCodeBoxFronted`（待建）

## 1. 背景与目标

### 1.1 动机

现版本上传链路（`/share/file/`、`/chunk/*`、`/presign/*`）都要求文件**先完整落到服务器**才能生成分享码。
这带来两个痛点：

1. **发布有等待**：大文件必须先传完，才能把码给别人。
2. **占用服务器资源**：存储配额与磁盘被即时占用，临时性分享尤其浪费。

### 1.2 目标

上传页新增「P2P 直传」选项（**默认勾选**）。勾选后：

- 文件**不上传服务器**，发布时**立即出码**，零等待；
- 下载者通过 P2P 直连从发布者浏览器取件；
- 发布者页面显示「请勿关闭网页」提示与在线状态；
- P2P 文件**不占用存储配额**，只受独立的 P2P 大小上限约束。

### 1.3 非目标（明确不做）

- 不做「发布者关闭页面后自动转存服务器」的保底上传（会退回传统模式，违背初衷）。
- 不做 2023 旧版主题的适配。
- 不做多 workers 下的分布式信令（先单进程，后续需要时再上 Redis）。

## 2. 设计原则

### 2.1 必须修正的认知：无法做到「完全不经过服务器」

WebRTC 建立连接需要**信令交换**（SDP / ICE），这部分必经服务器（流量极小）。
更关键的是 NAT 穿透：国内家宽与 4G/5G 大量处于运营商 CGNAT 之后，直连会失败，
此时必须由 **TURN 中继**转发流量 —— 而 TURN 就是服务器在传数据。

因此本设计的定位是 **「优先直连，兜底中继」**，而不是「绝不经过服务器」。

### 2.2 三种传输模式

| 模式 | 数据路径 | 服务器成本 | 客户端门槛 |
|------|----------|------------|------------|
| 传统模式 | 发布者 → 服务器存储 → 下载者 | 存储 + 带宽 | 无 |
| **P2P 直传** | 发布者 → 下载者（直连） | 仅信令 | WebRTC |
| **流式中转（兜底）** | 发布者 → 服务器转发 → 下载者 | 双倍带宽，**不落盘** | 无 |

流式中转作为 P2P 直连失败时的兜底：仍然满足「不落盘、发布零等待」，且**不需要 NAT 穿透、
不需要 File System Access API**，浏览器兼容性最好。

### 2.3 选型决策（已确认）

| 决策项 | 结论 |
|--------|------|
| 发布者离线后 | 分享失效，下载者看到明确提示；**不做**保底上传 |
| 直连失败但发布者在线 | 降级为服务器流式中转 |
| TURN | **自建 coturn**，通过短时临时凭据下发到前端 |
| 配额 | P2P 独立大小上限，**不计入**存储配额 |
| 主题范围 | 只改 2024 主题 |

## 3. 状态与生命周期

### 3.1 分享状态机

```
              publish
   (无) ─────────────────► offline ──hello/心跳──► online
                            ▲                        │
                            │     心跳超时 30s        │
                            └────────────────────────┘
                            │
            发布者主动 unpublish / 到达 expired_at
                            ▼
                         expired（终态）
```

- 分享存活期 = `min(发布者页面存活期, expired_at)`。
- `p2p_last_seen` 超过 `p2pHeartbeatTimeout`（默认 30s）即判定 `offline`。
- **P2P 模式禁用「按取件次数」过期**（`expire_style=count`）：数据一旦交给下载者就无法收回，
  次数限制在 P2P 下不可保证。仅支持 `day` / `hour` / `minute` / `forever`。

### 3.2 单次取件状态机

```
downloader 加入
   │
   ├─ ICE 探测成功 ──► direct 传输 ──► done
   │
   └─ ICE 探测失败 ──► relay 协商 ──► 流式转发 ──► done
```

传输模式（`direct` / `relay`）需在下载者 UI 上如实展示。

## 4. 数据模型

### 4.1 `FileCodes` 新增字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `is_p2p` | BooleanField(default=False) | 是否 P2P 分享 |
| `p2p_token_hash` | CharField(64, null) | 发布端令牌哈希（原文仅发布者持有） |
| `p2p_status` | CharField(16, default="offline") | online / offline / expired |
| `p2p_last_seen` | DatetimeField(null) | 发布端最后心跳 |
| `p2p_served_count` | IntField(default=0) | 成功完成传输的次数 |
| `p2p_bytes_sent` | BigIntField(default=0) | 累计传出字节 |
| `p2p_last_transport` | CharField(16, null) | direct / relay |

P2P 记录中 `file_path` / `uuid_file_name` 留空，`size` 存文件真实大小供前端展示。

新增迁移 `apps/base/migrations/migrations_007.py`。

### 4.2 运行态（不落库）

信令房间状态（在线下载者列表、PeerConnection 映射、中继缓冲队列）**只存进程内存**。
原因见 §9.1。

## 5. 配置项

`core/settings.py:DEFAULT_CONFIG` 新增，并经管理面板可改：

| 键 | 默认 | 说明 |
|----|------|------|
| `enableP2P` | `1` | 站点级总开关 |
| `p2pDefaultChecked` | `1` | 上传页勾选框默认状态 |
| `p2pMaxSize` | `2 * 1024**3` | P2P 单文件上限（独立于 `uploadSize`） |
| `p2pRelayEnabled` | `1` | 是否允许流式中转兜底 |
| `p2pMaxPeers` | `3` | 单分享并发下载者上限 |
| `p2pHeartbeatTimeout` | `30` | 心跳超时秒数 |
| `p2pRoomTtl` | `900` | 空房间回收秒数 |
| `p2pStunUrls` | `["stun:stun.l.google.com:19302"]` | STUN 列表 |
| `p2pTurnUrls` | `[]` | TURN 列表（如 `turn:your.host:3478`） |
| `p2pTurnSecret` | `""` | coturn `static-auth-secret`，**仅服务端可见** |
| `p2pTurnTtl` | `7200` | TURN 临时凭据有效期 |

`build_public_config()` 下发除 `p2pTurnSecret` 外的纯前端可见项。TURN 凭据走
§7.2 的临时凭据接口按需签发。

## 6. 接口设计

### 6.1 REST

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/p2p/publish` | 创建 P2P 分享。入参 `file_name` / `file_size` / `expire_value` / `expire_style`。返回 `{code, publish_token, expires_at}`。**不调用 `reserve_storage`** |
| GET | `/p2p/status/{code}` | 查询在线状态、文件名、大小、已服务次数 |
| POST | `/p2p/unpublish` | 发布者主动下线，释放房间 |
| POST | `/p2p/ice` | 签发 TURN 临时凭据（见 §7.2） |

`/p2p/publish` 校验：

- `file_size <= p2pMaxSize`；
- `validate_file_type(file_name)`（**只能校验文件名**，服务端不持有内容）；
- `expire_style` 属于 `{day, hour, minute, forever}`。

### 6.2 `/share/select/` 分支

对 `is_p2p=True` 的记录：

- 返回 `{"type": "p2p", "code": ..., "online": bool, ...}`，**不返回 `download_url`**；
- **不调用 `consume_file_usage()`**（不消耗 `used_count` / `expired_count`），过期只按 `expired_at` 判定。

### 6.3 WebSocket 信令

```
WS /p2p/signal/{code}?role=publisher&token=<publish_token>
WS /p2p/signal/{code}?role=downloader
```

鉴权：publisher 必须持有效 `publish_token`；downloader 必须持有效 `code`。复用 `ip_limit` 做洪泛限制。

**JSON 文本帧**：

| `t` | 方向 | 载荷 | 说明 |
|-----|------|------|------|
| `hello` | 双向 | `role` | 入房 |
| `room` | →发布者 | `peers[]` | 当前下载者列表 |
| `peer-join` | →发布者 | `peer` | 新下载者，可发起 offer |
| `peer-left` | →发布者 | `peer` | 下载者离开，关闭对应 PC |
| `offer` / `answer` | 双向 | `peer`, `sdp` | 转发给对端 |
| `ice` | 双向 | `peer`, `candidate` | 转发给对端 |
| `mode` | 双向 | `peer`, `mode` | `direct` / `relay` |
| `relay-start` / `relay-ready` | 双向 | `peer` | 中继握手 |
| `pause` / `resume` | →发布者 | `peer` | 背压控制 |
| `done` | →发布者 | `peer`, `bytes` | 传输完成，累加统计 |
| `error` | →双端 | `message` | 错误 |

**二进制帧**（仅 relay 模式）：

```
[1B peer_id 长度][peer_id][payload]
```

服务端按 `peer_id` 直接转发给目标下载者，**不落盘**。

### 6.4 背压

服务端为每个下载者维护发送缓冲上限（建议 4 MB）。超限时向发布者发 `pause`，
缓冲回落到阈值以下发 `resume`。避免中转时服务端内存被慢速下载者拖爆。

## 7. 安全设计

### 7.1 令牌

- `publish_token`：`secrets.token_urlsafe(32)`，**库中只存哈希**（与现有密码哈希策略一致）。
- 发布者前端用 `localStorage` 保存 `code + publish_token`，刷新页面后可恢复服务。

### 7.2 TURN 临时凭据（必须做，否则 TURN 会被白嫖）

**不得**把静态 TURN 账号密码下发到前端。采用 coturn REST API 模式：

```
username   = f"{expiry_ts}:{user_id}"
credential = base64(hmac_sha1(p2pTurnSecret, username))
```

`POST /p2p/ice` 返回：

```json
{
  "ice_servers": [
    { "urls": ["stun:..."]},
    { "urls": ["turn:host:3478"], "username": "...", "credential": "..." }
  ],
  "expires_in": 7200
}
```

凭据短时效（默认 2h），过期后客户端重新调用。

### 7.3 已知安全退化（需在文档与后台明确告知）

1. **服务端无法做内容扫描**：文件不经过服务器，`allowed_file_types` 只能校验文件名。
   对公开站点这是滥用敞口，管理面板需明确警示，并建议配合 `page_explain` 声明责任。
2. **互相暴露公网 IP**：WebRTC 直连时双方看到对方真实 IP（传统模式下 IP 是隐藏的）。
   若需隐藏，只能强制走 TURN（`iceTransportPolicy: "relay"`）——这会牺牲直连性能。
3. **信令房间探测**：需鉴权 + 单 IP 房间数限流，防止被枚举探测发布者在线状态。

## 8. 前端改动（需先 fork 前端仓库）

### 8.1 前置动作

`themes/` 在 `.gitignore` 中，Dockerfile 构建时从上游 clone 前端：

```dockerfile
RUN git clone ... https://github.com/vastsa/FileCodeBoxFronted.git /build/fronted-2024
```

因此必须：

1. Fork `vastsa/FileCodeBoxFronted` → `ayflying/FileCodeBoxFronted`；
2. 修改 `Dockerfile` 的 `FRONTEND_2024_REF` 指向 fork；
3. 前端改完后打 tag，Dockerfile 用固定 tag 而非 `main`，保证构建可复现。

### 8.2 2024 主题（Vue3 + Vite + TS）改动点

| 文件 | 改动 |
|------|------|
| `src/views/SendFileView.vue` | 新增 P2P 勾选框（默认勾选，读 `p2pDefaultChecked`） |
| `src/composables/useP2PPublisher.ts` | 新增：文件句柄、WS、RTCPeerConnection 管理、relay 分支 |
| `src/composables/useP2PDownloader.ts` | 新增：收流、边收边写、进度、断点重试 |
| `src/components/P2PPublishStatus.vue` | 新增：在线指示灯、已服务次数、传输进度、关闭警告 |
| `src/views/RetrievewFileView.vue` | P2P 分支 UI：在线/离线、传输模式标识 |
| `src/services/*` | 新增 `/p2p/*` 接口封装 |
| `src/views/manage/*` | 后台 P2P 列表 + 全局开关 |

### 8.3 发布端保活（对应「不要关闭网页」提示）

- `beforeunload` 拦截跳转/关闭；
- `navigator.wakeLock` 阻止息屏；
- 可见性变化提示（切到后台仍可用，但关闭标签即失效）；
- 优先用 `showOpenFilePicker()` 拿 `FileSystemFileHandle`，刷新页面后可重新授权继续服务；
  不支持时回退 `<input type="file">`（刷新即需重选）。

### 8.4 下载端落盘

- 优先 `showSaveFilePicker()` + `WritableStream` **边收边写**（Chromium）；
- 不支持时回退 Blob 拼装 + `a[download]`，并**限制该路径下的大小**（建议 512 MB）；
- 分块 + 每块 sha256 校验，支持断点重试。

## 9. 部署变更

### 9.1 信令与 worker 数（重要）

信令房间状态存进程内存，**`WORKERS > 1` 会导致发布者与下载者可能落在不同进程而无法握手**。

- 现阶段：P2P 启用时锁定 `WORKERS=1`，启动时检测到冲突则打 warning；
- 后续如确需横向扩展：信令层改 Redis pub/sub，或按 `code` 做一致性哈希路由。

### 9.2 反向代理

Nginx 需为 `/p2p/signal/` 放行 WebSocket 升级：

```nginx
proxy_http_version 1.1;
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade";
proxy_read_timeout 3600s;
```

### 9.3 coturn

新增 coturn 服务（可与站点同机部署）：

```
listening-port=3478
tls-listening-port=5349
realm=<your-domain>
external-ip=<public-ip>
use-auth-secret
static-auth-secret=<p2pTurnSecret>
min-port=49152
max-port=65535
```

需在防火墙放行 3478/tcp+udp、5349/tcp+udp 及 49152-65535/udp 端口段。
若服务器本身在 NAT 后，`external-ip` 必须写公网地址。

### 9.4 清理任务

`core/tasks.py` 新增 `clean_orphan_p2p_rooms()`：回收心跳超时的 online 状态、
清理 `expired_at` 已过的 P2P 记录、释放空房间内存。

## 10. 分阶段实施与验收

| 阶段 | 内容 | 验收标准 |
|------|------|----------|
| **P1** | WS 信令骨架 + 房间鉴权 | 两个浏览器同一 code 能建立 `RTCPeerConnection` 并进入 `connected`；日志可见 offer/answer/ice 完整往返 |
| **P2** | 直连传输 | 跨机传输 100 MB 文件成功，**sha256 与服务端记录一致**，进度条走完，`p2p_served_count` +1 |
| **P3** | 流式中转兜底 + coturn | 强制 `iceTransportPolicy=relay` 后文件仍能完整传输，`p2p_last_transport=relay`；临时凭据过期后重新签发可用 |
| **P4** | 发布端体验 | Chromium 刷新页面后能恢复句柄继续服务；`beforeunload` 弹确认；Wake Lock 生效；发布者关闭页面后下载端 5s 内显示「发布者已离线」 |
| **P5** | 管理端与配置 | 后台可见 P2P 列表/在线状态/已服务次数，可强制失效；站点开关生效；**P2P 文件不计入 quota 统计**（对比 `storageLimit` 核验） |
| **P6** | 边界与降级 | >2 GB 文件在 Chromium 边收边写成功；Firefox/Safari 下载端给出明确降级提示；手机发布端被禁用并给出原因 |

每阶段结束需在 **217 调试环境**验证，并按项目习惯产出中文 commit 记录。

## 11. 已知限制

1. **发布端仅桌面 Chromium 体验完整**：Firefox / Safari 无 File System Access API，刷新后需重选文件。
2. **手机不能当发布端**：移动端后台标签会被系统挂起，连接必断。UI 需直接禁用并说明。
3. **多下载者受发布端上行带宽限制**：N 个并发下载者会等分发布者上行；需并发上限 + 队列。
4. **按次数过期不可用**：见 §3.1。
5. **服务端无内容审计能力**：见 §7.3。
6. **休眠/锁屏会断连**：电脑休眠后连接断开，恢复后需重连（下载端支持断点续传）。

## 12. 待定项

- P2P 分享是否需要独立的提取码类型（沿用 `code_generate_type` 即可？）
- 是否在下载页显示发布者的「预估在线时长」而非仅在线/离线
- 后台是否需要 P2P 传输统计的图表（复用现有统计模块？）
- `p2pMaxSize` 默认值取 2 GB 是否合适（受下载端落盘方式制约）
