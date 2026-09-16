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
| `p2pMaxSize` | `2 * 1024**3` | P2P 单文件上限，**服务端硬闸**（独立于 `uploadSize`）。注意这是「防意外」的上限而非推荐值：下载端不支持流式落盘时，由前端在 512 MB 处给出降级提示，该阈值硬编码、不新增配置项 |
| `p2pRelayEnabled` | `1` | 是否允许流式中转兜底 |
| `p2pMaxPeers` | `3` | 单分享并发下载者上限 |
| `p2pHeartbeatTimeout` | `30` | 心跳超时秒数 |
| `p2pRoomTtl` | `900` | 空房间回收秒数 |
| `p2pStunUrls` | `[]` | STUN 列表。留空 = 按访问入口自动派生 `stun:<host>:p2pStunPort`，指向站点**内置自建 STUN**；显式写入则以其为准。**默认不含任何第三方 STUN** |
| `p2pStunEnabled` | `1` | 内置 STUN 开关（随站点进程启停，实现见 `apps/base/p2p/stun.py`） |
| `p2pStunPort` | `3478` | 内置 STUN 监听端口（容器与防火墙需放行 UDP） |
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
- 不支持时回退 Blob 拼装 + `a[download]`，并**限制该路径下的大小**（硬编码 512 MB，见 D9）；
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

### 9.3 STUN（内置）与 coturn（仅 TURN 需要）

**STUN 无需任何外部依赖，也不需要额外部署服务**：
`apps/base/p2p/stun.py` 在站点进程内实现了 RFC 5389 Binding（只做 ICE 收集 srflx
候选所需的最小集合），随应用一起启停（见 `main.py` 的 `lifespan`），端口取
`p2pStunPort`（默认 3478/udp）。

下发给前端的 STUN 地址由 `p2p_stun_urls()` 按**当前访问入口**派生，因此
「站点可达即 STUN 可达」——换内网 IP、虚拟网 IP 或域名都不需要改配置。
默认配置里**不含任何第三方 STUN**；如需改用外部 STUN，把地址写进 `p2pStunUrls`
即覆盖自动派生。

只需保证 UDP 端口可入：

```yaml
ports:
  - "12345:12345"
  - "3478:3478/udp"   # 内置 STUN
```

宿主防火墙同样要放行 3478/udp。内置 STUN 起不来（端口被占）时只降级为
「无 STUN」，站点本身照常可用。

**coturn 仅在需要 TURN 中继时才部署**（对称型 NAT 兜底 —— 此时服务器必然在传数据，
属 §2 已划定的例外）。单独部署 coturn 服务，可与站点同机：

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
部署后把 `turn:<host>:3478` 写进 `p2pTurnUrls` 并配置 `p2pTurnSecret`。

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

## 12. 决策记录（已定案）

### 12.1 产品决策

| # | 议题 | 决策 | 理由 |
|---|------|------|------|
| D1 | 发布者离线后分享如何处理 | **直接失效**，下载端明确提示；不做「关闭页面自动转存」 | 转存会退回传统模式，违背「不上传」初衷；诚实优于假装可用 |
| D2 | 直连失败但发布者在线 | **降级为服务器流式中转**（边收边发、不落盘） | 不占存储、不需 NAT 穿透、兼容性满分，且能兜住大部分直连失败场景 |
| D3 | 是否部署 TURN | **自建 coturn**，凭据走 REST 临时凭据 | 唯一能救回对称型 NAT 的手段；静态密码下发前端会被白嫖带宽 |
| D4 | P2P 是否受存储配额约束 | **不受配额约束**，由 `p2pMaxSize` 独立约束 | 不占服务器磁盘，计入配额语义混乱 |
| D5 | 主题改造范围 | **只改 2024 主题** | 2024 为 Vue3 + Vite + TS；2023 为旧版 Vue，改造成本翻倍、收益低 |
| D6 | 提取码类型 | **沿用现有 `code_generate_type`，不新增类型** | P2P 是「传输方式」而非「提取码形态」，二者正交；区分只靠 `is_p2p` 字段，避免前端维护两套取件 UI 分支 |
| D7 | 下载页在线信息呈现 | **显示「最后活跃时间」（相对时间）+ 在线/离线二态，不做「预估在线时长」** | 发布者随时可能关页面，预估准确率极低；给出虚假确定性比不给更糟。心跳本就带时间戳，实现成本为零 |
| D8 | 后台 P2P 统计图表 | **P1–P6 不做图表**，仅在管理端列表展示关键字段（在线状态、已服务次数、传输方式、累计字节） | 图表需要时间序列落库，成本高且当前无数据；先积累真实数据，再决定是否值得做 |

### 12.2 工程决策

| # | 议题 | 决策 | 理由 |
|---|------|------|------|
| D9 | `p2pMaxSize` 默认值 | **保持 2 GB**，定位为「服务端硬闸」而非推荐值 | 该值受下载端落盘方式制约：Chromium 可边收边写（受磁盘限制），Firefox/Safari 只能 Blob 拼装。硬闸取宽，体验约束交给前端能力检测处理（512 MB 提示阈值**硬编码**，不新增配置项，避免配置面膨胀） |
| D10 | 目标版本号 | **`v2.6.0`** | 新增功能性能力属 minor 变更；由 release-please 依据 `feat:` 提交自动升版，不手工改 `VERSION` |
| D11 | 前端 fork 与构建引用 | fork **已建**（`ayflying/FileCodeBoxFronted`，`main`）；Dockerfile 的 `FRONTEND_2024_REF` **暂不切换**，待前端实际改动落地（P4 前）再切 | 当前 fork 与上游代码一致，提前切换只产生无意义 diff |
| D12 | 多 worker 下的信令 | **先强制单进程信令**，不引入 Redis pub/sub | 现状 `WORKERS=1`；在真实出现多 worker 需求前不做分布式信令，避免过度设计 |

### 12.3 尚未决议（不阻塞 P1）

- 前端开发期联调方式（本地 Vite dev server 直连本地后端，还是构建产物挂载）—— 进入 P4 前确定。
- 是否把 P2P 传输统计写入 `usage_logs` 供后续分析 —— 待 D8 有数据后再评估。

## 13. P1 落地记录（已完成）

P1 范围：**WS 信令骨架 + 房间鉴权**。数据面（直连传输、流式中转）留待 P2/P3。

### 13.1 代码结构

| 文件 | 职责 |
|------|------|
| `apps/base/p2p/__init__.py` | 包入口，定义 `p2p_api = APIRouter(prefix="/p2p")` |
| `apps/base/p2p/config.py` | 站点配置集中读取与归一化；`build_public_p2p_config()` 保证不下发 `p2pTurnSecret` |
| `apps/base/p2p/tokens.py` | 发布令牌哈希与校验、coturn REST 临时凭据、ICE servers 组装 |
| `apps/base/p2p/rooms.py` | 进程内房间表 + `SignalingRouter`（只产投递决策，不碰 WebSocket，可单测） |
| `apps/base/p2p/store.py` | 房间状态落库（在线状态、累计统计、主动下线），心跳写库做 15s 节流 |
| `apps/base/p2p/stun.py` | 内置 STUN（RFC 5389 Binding），随应用 lifespan 启停，不依赖第三方 STUN |
| `apps/base/p2p/views.py` | REST 控制面：`/p2p/publish`、`/p2p/status/{code}`、`/p2p/unpublish`、`/p2p/ice` |
| `apps/base/p2p/signaling.py` | WS 端点 `/p2p/signal/{code}`，只做连接生命周期与消息搬运 |
| `apps/base/migrations/migrations_007.py` | 幂等增量迁移：7 个 P2P 列 + `is_p2p` 索引 |

### 13.2 落地时的关键取舍

- **`/p2p/publish` 不调用 `reserve_storage`**，不写 `file_path` / `uuid_file_name`，
  只登记元数据（落实 D4）。因此该记录没有可删的文件。
- **过期清理分支**：`core/tasks.py` 对 `is_p2p` 记录只调 `room_manager.drop_room()`，
  不调 `file_storage.delete_file()`，避免对不存在的文件做删除。
- **发布端重连顶替**：同一 code 的新发布者连接会顶替旧连接，旧连接收到 `replaced` 错误帧
  并以 `4412` 关闭。这样刷新页面不会导致「房间被自己占满」。
- **信令层的独立可测性**：`rooms.SignalingRouter` 只返回 `Delivery` 投递决策，
  不直接操作 WebSocket，因此信令语义可以在没有网络的情况下完整单测。
- **P1 二进制帧**返回 `relay_unavailable` 错误帧，明确告知数据面尚未启用，
  不做「假装在传」的静默丢弃。

### 13.3 关闭码约定

| 关闭码 | 含义 |
|--------|------|
| `4400` | role 参数不合法 |
| `4401` | 发布令牌无效 |
| `4404` | 取件码不存在 |
| `4409` | 并发下载者已达上限 |
| `4410` | 分享已失效 |
| `4412` | 发布端已在新的连接恢复服务，本连接被顶替 |
| `4429` | 触发 IP 限流 |
| `4503` | 站点未启用 P2P 直传 |

失败时统一「先 `accept()` 再回 `error` 帧、随后关闭」，让前端能拿到具体原因，
而不是只看到无上下文的握手失败。

### 13.4 验证结果

| 层级 | 脚本 | 结果 |
|------|------|------|
| 单元测试 | `tests/test_p2p_signaling.py` | 55 项全过（令牌、ICE 组装、配置归一化、房间生命周期、信令转发决策、迁移幂等） |
| 协议层端到端 | `tests/manual/p2p_e2e_signal.py` | 52 项全过（含鉴权负例与落库断言） |
| 浏览器侧验收 | `tests/manual/p2p_browser_acceptance.py` | 19 项全过（真实 WebRTC 协商成功、DataChannel 真实数据送达） |

验收标准「两个浏览器同一 code 能建立 `RTCPeerConnection` 并进入 `connected`」已达成：
两个独立浏览器上下文进入 `connectionState=connected`，DataChannel 双向 `open`，
发布端发出的探针数据经 P2P 通道在下载端收到，事件流可见完整 offer / answer / ICE 往返。

> 复现方式见 `tests/manual/README.md`。该文档另记录了「未初始化时 428 探测」
> 与「Windows 保留端口区间」两个已踩过的坑。
