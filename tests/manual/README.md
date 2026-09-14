# tests/manual

手工验收脚本与探针页。这些用例需要**起真实服务**（部分还需要真实浏览器），
因此不纳入 `python -m unittest discover -s tests` 的自动跑批。

日常回归请跑 `tests/test_p2p_signaling.py`（纯逻辑，脱离网络）。

## 文件说明

| 文件 | 用途 |
| --- | --- |
| `p2p_e2e_signal.py` | 协议层端到端：起 uvicorn + 两个真实 WebSocket 客户端，跑完整信令往返、鉴权负例、落库断言 |
| `p2p_browser_acceptance.py` | 浏览器侧验收：两个独立浏览器上下文跑**真实 WebRTC**，验证 RTCPeerConnection 能 connected、DataChannel 能传真实数据 |
| `p2p_signal_probe.html` | 探针页，由 `p2p_e2e_signal.py --serve-probe` 挂载到 `/probe/p2p_signal_probe.html` |

## 使用方式

### 一、协议层端到端

```bash
# 在仓库根目录执行
python tests/manual/p2p_e2e_signal.py                        # 复用已有 data/，不删库
python tests/manual/p2p_e2e_signal.py --reset-db             # 显式重置 data/（会清空现有数据）
python tests/manual/p2p_e2e_signal.py --serve-probe          # 顺带挂载浏览器探针页
python tests/manual/p2p_e2e_signal.py --port 8899            # 指定端口，默认 8899
```

> **安全约定**：脚本默认**绝不**删除 `data/` 目录。重置必须显式传 `--reset-db`。

### 二、浏览器侧 WebRTC 验收

需要两个终端。先起一个带探针页并保持运行的服务：

```bash
# 终端 1
python tests/manual/p2p_e2e_signal.py --serve-probe --keep-alive --port 8899
```

再跑浏览器脚本：

```bash
# 终端 2
python tests/manual/p2p_browser_acceptance.py
python tests/manual/p2p_browser_acceptance.py --probe-url http://127.0.0.1:8899/probe/p2p_signal_probe.html
python tests/manual/p2p_browser_acceptance.py --chrome "C:/path/to/chrome.exe"
```

截图输出到 `tests/manual/output/`（已在 `.gitignore` 中忽略）。

## 依赖

```
pip install aiohttp websockets playwright
```

`playwright` 需要对应版本的 Chromium。如果浏览器不在默认缓存路径，
用 `--chrome` 指定可执行文件，例如：

```bash
python tests/manual/p2p_browser_acceptance.py \
  --chrome "$HOME/AppData/Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe"
```

## 已验证结论（P1）

- 协议层：52 项全过（信令 offer/answer/ice 双向往返、ping/pong、done 统计、
  错误发布令牌 / 不存在取件码 / 非法 role 均被正确拒绝并带自定义关闭码、落库字段正确）
- 浏览器侧：19 项全过（两个独立浏览器上下文 `RTCPeerConnection` 进入 `connected`，
  DataChannel 双向 `open`，真实数据经 P2P 通道送达对端）

## 已知坑

- **未初始化时的 428 探测**：站点未初始化时，中间件对「想要 HTML」的请求
  （`Accept` 为 `*/*` 或含 `text/html`）返回 **200 + 初始化页面**，而不是 428。
  探测初始化状态必须显式带 `Accept: application/json`，否则会把未初始化误判为已初始化。
- **端口占用**：Windows 上部分端口落在 Hyper-V/WSL 保留区间，绑定时会报权限错误。
  用 `netsh interface ipv4 show excludedportrange protocol=tcp` 查看保留区间后换端口。
- **旧服务残留**：重复运行时若报端口占用，先确认 `8899` 上没有上一次的进程。
