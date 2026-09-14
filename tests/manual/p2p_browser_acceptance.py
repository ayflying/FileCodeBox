"""P1 验收（浏览器侧）：两个独立浏览器上下文建立 RTCPeerConnection 并跑通数据通道。

与协议层测试的区别：这里用的是**真实 WebRTC 栈**，能证明我们转发的 SDP/ICE
确实可被浏览器协商成功，而不只是「字段搬运正确」。

前置：先启动带探针页的测试服务
    python tests/manual/p2p_e2e_signal.py --serve-probe --port 8899

再执行本脚本：
    python tests/manual/p2p_browser_acceptance.py
    python tests/manual/p2p_browser_acceptance.py --probe-url http://127.0.0.1:8899/probe/p2p_signal_probe.html

依赖：playwright（pip install playwright）+ 已缓存的 Chromium。
可用 --chrome 指定浏览器可执行文件路径。
"""

import argparse
import json
import sys
import time
from pathlib import Path

DEFAULT_CHROME = Path.home() / "AppData/Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe"
DEFAULT_PROBE = "http://127.0.0.1:8899/probe/p2p_signal_probe.html"

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail}" if detail and not ok else ""))


def probe_state(page):
    return page.evaluate("() => window.__probe ? JSON.parse(JSON.stringify(window.__probe)) : null")


def wait_for(page, expr, timeout=25.0, interval=0.3):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = page.evaluate(expr)
        except Exception as exc:
            last = f"<eval error: {exc}>"
        if last:
            return last
        time.sleep(interval)
    return last


def main(chrome: Path, probe: str, out_dir: Path):
    from playwright.sync_api import sync_playwright

    if not chrome.exists():
        raise SystemExit(f"未找到 Chromium: {chrome}（可用 --chrome 指定路径）")
    out_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=str(chrome),
            headless=True,
            args=["--allow-loopback-in-peer-connection", "--disable-dev-shm-usage"],
        )

        ctx_pub = browser.new_context()
        ctx_dl = browser.new_context()
        page_pub = ctx_pub.new_page()
        page_dl = ctx_dl.new_page()

        pub_logs, dl_logs = [], []
        page_pub.on("console", lambda m: pub_logs.append(m.text))
        page_dl.on("console", lambda m: dl_logs.append(m.text))

        print("== 1. 发布端创建 P2P 分享 ==")
        page_pub.goto(f"{probe}?role=publisher", wait_until="load")
        code = wait_for(page_pub, "() => window.__probe && window.__probe.code", timeout=20)
        check("发布端拿到取件码", bool(code), f"code={code}")
        pub_signaling = wait_for(
            page_pub, "() => window.__probe && window.__probe.signaling === 'open'", timeout=15
        )
        check("发布端信令已连接", bool(pub_signaling))

        if not code:
            browser.close()
            return report()

        print("\n== 2. 下载端接入（独立浏览器上下文）==")
        page_dl.goto(f"{probe}?role=downloader&code={code}", wait_until="load")
        dl_signaling = wait_for(
            page_dl, "() => window.__probe && window.__probe.signaling === 'open'", timeout=15
        )
        check("下载端信令已连接", bool(dl_signaling))

        print("\n== 3. 等待 RTCPeerConnection 建立 ==")
        pub_connected = wait_for(
            page_pub, "() => window.__probe && window.__probe.connection === 'connected'", timeout=30
        )
        dl_connected = wait_for(
            page_dl, "() => window.__probe && window.__probe.connection === 'connected'", timeout=30
        )
        pub_state = probe_state(page_pub)
        dl_state = probe_state(page_dl)
        check(
            "发布端 RTCPeerConnection=connected",
            bool(pub_connected),
            f"connection={pub_state.get('connection')}",
        )
        check(
            "下载端 RTCPeerConnection=connected",
            bool(dl_connected),
            f"connection={dl_state.get('connection')}",
        )
        check("发布端 peer_id 已分配", bool(pub_state.get("peerId")), str(pub_state.get("peerId")))
        check(
            "双方看到彼此的 peer_id",
            bool(pub_state.get("remotePeerId")) and bool(dl_state.get("remotePeerId")),
            f"pub→{pub_state.get('remotePeerId')} dl→{dl_state.get('remotePeerId')}",
        )

        print("\n== 4. DataChannel 可用性 ==")
        pub_dc = wait_for(
            page_pub, "() => window.__probe && window.__probe.dataChannel === 'open'", timeout=20
        )
        dl_dc = wait_for(
            page_dl, "() => window.__probe && window.__probe.dataChannel === 'open'", timeout=20
        )
        check("发布端 DataChannel=open", bool(pub_dc))
        check("下载端 DataChannel=open", bool(dl_dc))

        print("\n== 5. 真实数据经 P2P 通道传输 ==")
        payload = page_pub.evaluate("() => window.__sendProbe()")
        check(
            "发布端发出探针数据",
            isinstance(payload, str) and payload.startswith("probe-payload-"),
            str(payload),
        )

        if isinstance(payload, str) and payload.startswith("probe-payload-"):
            received = wait_for(
                page_dl,
                f"() => (window.__probe.receivedPayloads || []).includes({json.dumps(payload)})",
                timeout=15,
            )
            check("下载端经数据通道收到该数据", bool(received), str(probe_state(page_dl).get("receivedPayloads")))

        print("\n== 6. 传输完成上报与状态同步 ==")
        page_pub.evaluate("() => window.__sendDone(1048576)")
        time.sleep(1.0)
        pub_final = probe_state(page_pub)
        check("发布端无错误", not pub_final.get("errors"), str(pub_final.get("errors")))
        check("下载端无错误", not probe_state(page_dl).get("errors"), str(probe_state(page_dl).get("errors")))

        print("\n== 7. SDP/ICE 完整往返证据（事件流）==")
        events = [e.get("msg", "") for e in (pub_final.get("events") or [])]
        joined = "\n".join(events)
        check("事件流含 offer 发送", "→ offer" in joined, joined[-500:])
        check("事件流含 answer 接收", "← answer" in joined, joined[-500:])
        sent_ice = [e for e in events if e.startswith("→ ice") or e.startswith("→ ICE")]
        check("发布端发出过 ICE 候选", len(sent_ice) > 0, f"count={len(sent_ice)}")

        dl_final = probe_state(page_dl)
        dl_events = [e.get("msg", "") for e in (dl_final.get("events") or [])]
        dl_joined = "\n".join(dl_events)
        check("下载端事件流含 offer 接收", "← offer" in dl_joined, dl_joined[-500:])
        check("下载端事件流含 answer 发送", "→ answer" in dl_joined, dl_joined[-500:])
        check(
            "下载端收到远端 ICE 候选",
            "远端 ice 已加入" in dl_joined or "暂存 ice" in dl_joined,
            dl_joined[-500:],
        )

        page_pub.screenshot(path=str(out_dir / "shot_publisher.png"), full_page=True)
        page_dl.screenshot(path=str(out_dir / "shot_downloader.png"), full_page=True)
        print(f"\n截图已保存: {out_dir / 'shot_publisher.png'} / shot_downloader.png")

        print("\n--- 发布端事件流 ---")
        for item in events:
            print("   ", item)
        print("--- 下载端事件流 ---")
        for item in dl_events:
            print("   ", item)

        browser.close()

    return report()


def report():
    print("\n" + "=" * 60)
    failed = [r for r in results if not r[1]]
    print(f"通过 {len(results) - len(failed)} 项，失败 {len(failed)} 项")
    for name, _, detail in failed:
        print("  FAILED:", name, detail)
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="P2P P1 浏览器侧 WebRTC 验收")
    parser.add_argument("--chrome", type=Path, default=DEFAULT_CHROME, help="Chromium 可执行文件路径")
    parser.add_argument("--probe-url", default=DEFAULT_PROBE, help="探针页地址")
    parser.add_argument("--out-dir", type=Path, default=Path("tests/manual/output"), help="截图输出目录")
    args = parser.parse_args()

    sys.exit(main(args.chrome, args.probe_url, args.out_dir))