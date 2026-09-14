"""P1 验收（协议层）：真实 uvicorn 服务 + 两个真实 WebSocket 客户端跑完整信令往返。

覆盖：初始化 → publish → publisher/downloader 入房 → offer/answer/ice 双向转发
→ ping/pong → done 统计 → 鉴权与边界拒绝 → 数据库落库断言。

用法（务必在仓库根目录执行）：
    python tests/manual/p2p_e2e_signal.py                 # 复用已有 data/，不删库
    python tests/manual/p2p_e2e_signal.py --reset-db      # 显式重置 data/（会清空现有数据）
    python tests/manual/p2p_e2e_signal.py --serve-probe   # 顺带把浏览器探针页挂到 /probe/

安全约定：默认**绝不**删除 data/ 目录，重置必须显式传 --reset-db。
"""

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

import aiohttp
import websockets

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

MANUAL_DIR = Path(__file__).resolve().parent
PROBE_FILE = MANUAL_DIR / "p2p_signal_probe.html"

PASSED = []
FAILED = []


def check(name: str, condition: bool, detail: str = ""):
    if condition:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append(f"{name} :: {detail}")
        print(f"  [FAIL] {name} :: {detail}")


SETUP_FORM = {
    "site_name": "P2P Test",
    "admin_password": "testpassword123",
    "confirm_password": "testpassword123",
    "upload_size_value": "1024",
    "upload_size_unit": "MB",
    "save_time_value": "0",
    "save_time_unit": "day",
    "expireStyle": ["day", "hour", "minute", "forever"],
    "code_generate_type": "number",
    "allowed_file_types": "*",
    "openUpload": "1",
    "enableChunk": "0",
    "uploadCount": "5000",
    "uploadMinute": "1",
    "errorCount": "5000",
    "errorMinute": "1",
    "loginCount": "50",
    "loginMinute": "1",
}


def reset_database():
    data_dir = REPO / "data"
    print(f"  [WARN] --reset-db 生效，正在删除 {data_dir}")
    shutil.rmtree(data_dir, ignore_errors=True)
    data_dir.mkdir(parents=True, exist_ok=True)


async def wait_started(server):
    for _ in range(300):
        if server.started:
            return
        await asyncio.sleep(0.05)
    raise SystemExit("服务未能在预期时间内启动")


async def recv_json(ws, timeout=5.0, label=""):
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        raise AssertionError(f"{label} 收到非 JSON 帧: {raw!r}")


def mount_probe_page(app):
    """仅测试用：把探针页挂到 /probe/，不改动生产代码。"""
    from fastapi.responses import FileResponse

    @app.get("/probe/p2p_signal_probe.html")
    async def _probe_page():  # pragma: no cover - 手工验收辅助
        return FileResponse(PROBE_FILE, media_type="text/html")


async def main(port: int, serve_probe: bool, reset_db: bool, keep_alive: bool = False):
    if reset_db:
        reset_database()

    import uvicorn

    import main as app_module

    if serve_probe:
        mount_probe_page(app_module.app)

    base = f"http://127.0.0.1:{port}"
    wsb = f"ws://127.0.0.1:{port}"

    config = uvicorn.Config(
        app_module.app, host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    await wait_started(server)

    code = None
    publish_token = None
    try:
        async with aiohttp.ClientSession() as session:
            # 注意：未初始化时中间件对「想要 HTML」的请求（Accept 为 */* 或含 text/html）
            # 返回的是 200 + 初始化页面，而不是 428。因此这里必须显式声明只要 JSON，
            # 才能拿到明确的 428 未初始化信号。
            async with session.get(
                f"{base}/health", headers={"Accept": "application/json"}
            ) as resp:
                needs_setup = resp.status == 428
                check(
                    "health 可达（未初始化时返回 428）",
                    resp.status in (200, 428),
                    f"status={resp.status}",
                )

            if needs_setup:
                async with session.post(
                    f"{base}/setup",
                    json=SETUP_FORM,
                    headers={"Accept": "application/json"},
                ) as resp:
                    body = await resp.text()
                    check("系统初始化成功", resp.status == 200, f"{resp.status} {body[:200]}")
            else:
                print("  [SKIP] 系统已初始化，跳过 setup（如需干净环境请加 --reset-db）")

            if needs_setup:
                async with session.get(
                    f"{base}/health", headers={"Accept": "application/json"}
                ) as resp:
                    check("初始化后 health 为 200", resp.status == 200, f"status={resp.status}")

            # ---- publish ----
            async with session.post(
                f"{base}/p2p/publish",
                json={
                    "file_name": "demo.bin",
                    "file_size": 1048576,
                    "expire_value": 1,
                    "expire_style": "day",
                },
            ) as resp:
                published = await resp.json()
                check("publish 成功", resp.status == 200, str(published)[:300])

            detail = published.get("detail") or {}
            code = detail.get("code")
            publish_token = detail.get("publish_token")
            check("publish 返回 code", bool(code), str(detail)[:200])
            check("publish 返回 publish_token", bool(publish_token), str(detail)[:200])
            check("publish 不回传 TURN 静态密钥", "p2pTurnSecret" not in json.dumps(detail))

            if not code or not publish_token:
                check("publish 返回可用凭据，后续用例无法继续", False, str(published)[:300])
                return

            # ---- 边界：count 过期方式必须被拒 ----
            async with session.post(
                f"{base}/p2p/publish",
                json={
                    "file_name": "demo.bin",
                    "file_size": 10,
                    "expire_value": 3,
                    "expire_style": "count",
                },
            ) as resp:
                text = await resp.text()
                check("拒绝 count 过期方式", resp.status == 400, f"{resp.status} {text[:200]}")

            # ---- 边界：超过 p2pMaxSize 必须被拒 ----
            async with session.post(
                f"{base}/p2p/publish",
                json={
                    "file_name": "huge.bin",
                    "file_size": 3 * 1024**3,
                    "expire_value": 1,
                    "expire_style": "day",
                },
            ) as resp:
                text = await resp.text()
                check("拒绝超大文件", resp.status == 403, f"{resp.status} {text[:200]}")

            # ---- status（发布者未连接 → offline）----
            async with session.get(f"{base}/p2p/status/{code}") as resp:
                status_body = await resp.json()
                st = status_body.get("detail") or {}
                check("status 初始为 offline", st.get("online") is False, str(st)[:200])
                check("status 是 P2P 记录", st.get("is_p2p") is True, str(st)[:200])

            # ---- ice ----
            async with session.post(f"{base}/p2p/ice") as resp:
                ice_body = await resp.json()
                ice_detail = ice_body.get("detail") or {}
                servers = ice_detail.get("ice_servers") or []
                check("ice 返回 STUN", len(servers) >= 1, str(ice_detail)[:200])
                check(
                    "ice 未配置 TURN 时不谎报可用",
                    ice_detail.get("turn_enabled") is False,
                    str(ice_detail)[:200],
                )

            # ---- 信令往返 ----
            await signaling_roundtrip(wsb, code, publish_token)

        # ---- 错误路径 ----
        await negative_paths(wsb, code)

        # ---- 落库断言 ----
        await assert_persistence(code)

        if keep_alive:
            print(f"\n  [INFO] --keep-alive 生效，服务继续监听 {base}")
            print("         浏览器验收可另开终端执行：python tests/manual/p2p_browser_acceptance.py")
            print("         按 Ctrl+C 结束。")
            while not server.should_exit:
                await asyncio.sleep(1)
    finally:
        server.should_exit = True
        await task


async def signaling_roundtrip(wsb: str, code: str, publish_token: str):
    print("\n== 信令往返 ==")
    pub_url = f"{wsb}/p2p/signal/{code}?role=publisher&token={publish_token}"
    dl_url = f"{wsb}/p2p/signal/{code}?role=downloader"

    async with websockets.connect(pub_url) as pub:
        pub_welcome = await recv_json(pub, label="publisher")
        check("publisher 入房收到 room 快照", pub_welcome.get("t") == "room", str(pub_welcome)[:200])
        pub_peer_id = pub_welcome.get("peer")
        check("publisher 拿到 peer_id", bool(pub_peer_id), str(pub_welcome)[:200])

        async with websockets.connect(dl_url) as dl:
            dl_welcome = await recv_json(dl, label="downloader")
            check("downloader 入房收到 room 快照", dl_welcome.get("t") == "room", str(dl_welcome)[:200])
            dl_peer_id = dl_welcome.get("peer")
            check("downloader 拿到 peer_id", bool(dl_peer_id), str(dl_welcome)[:200])
            check(
                "downloader 快照显示发布者在线",
                dl_welcome.get("publisher_online") is True,
                str(dl_welcome)[:200],
            )

            join_msg = await recv_json(pub, label="publisher/peer-join")
            check("publisher 收到 peer-join", join_msg.get("t") == "peer-join", str(join_msg)[:200])
            check("peer-join 指向正确下载者", join_msg.get("peer") == dl_peer_id, str(join_msg)[:200])

            # offer: publisher → downloader
            await pub.send(
                json.dumps(
                    {
                        "t": "offer",
                        "peer": dl_peer_id,
                        "sdp": {"type": "offer", "sdp": "v=0\r\no=- offer-probe"},
                    }
                )
            )
            offer = await recv_json(dl, label="downloader/offer")
            check("offer 转发到下载者", offer.get("t") == "offer", str(offer)[:200])
            check("offer 带 from=publisher", offer.get("from") == pub_peer_id, str(offer)[:200])
            check(
                "offer 的 SDP 原样透传",
                (offer.get("sdp") or {}).get("sdp") == "v=0\r\no=- offer-probe",
                str(offer)[:200],
            )

            # answer: downloader → publisher
            await dl.send(
                json.dumps(
                    {
                        "t": "answer",
                        "peer": pub_peer_id,
                        "sdp": {"type": "answer", "sdp": "v=0\r\no=- answer-probe"},
                    }
                )
            )
            answer = await recv_json(pub, label="publisher/answer")
            check("answer 转发到发布者", answer.get("t") == "answer", str(answer)[:200])
            check("answer 带 from=downloader", answer.get("from") == dl_peer_id, str(answer)[:200])
            check(
                "answer 的 SDP 原样透传",
                (answer.get("sdp") or {}).get("sdp") == "v=0\r\no=- answer-probe",
                str(answer)[:200],
            )

            # ICE 双向
            await pub.send(
                json.dumps(
                    {
                        "t": "ice",
                        "peer": dl_peer_id,
                        "candidate": {"candidate": "candidate:1 1 udp 1 10.0.0.1 1 typ host"},
                    }
                )
            )
            ice_to_dl = await recv_json(dl, label="downloader/ice")
            check("ICE(pub→dl) 转发成功", ice_to_dl.get("t") == "ice", str(ice_to_dl)[:200])

            await dl.send(
                json.dumps(
                    {
                        "t": "ice",
                        "candidate": {"candidate": "candidate:2 1 udp 1 10.0.0.2 2 typ host"},
                    }
                )
            )
            ice_to_pub = await recv_json(pub, label="publisher/ice")
            check("ICE(dl→pub) 转发成功", ice_to_pub.get("t") == "ice", str(ice_to_pub)[:200])
            check("ICE 带 from=downloader", ice_to_pub.get("from") == dl_peer_id, str(ice_to_pub)[:200])

            # 心跳
            await pub.send(json.dumps({"t": "ping"}))
            pong = await recv_json(pub, label="publisher/pong")
            check("ping 得到 pong", pong.get("t") == "pong", str(pong)[:200])

            # 传输完成上报
            await pub.send(json.dumps({"t": "done", "peer": dl_peer_id, "bytes": 1048576, "mode": "direct"}))
            ack = await recv_json(pub, label="publisher/done-ack")
            check("done 收到 done-ack", ack.get("t") == "done-ack", str(ack)[:200])
            check("done-ack 统计已服务次数", ack.get("served_count") == 1, str(ack)[:200])
            check("done-ack 累计字节正确", ack.get("bytes_sent") == 1048576, str(ack)[:200])

            peer_done = await recv_json(dl, label="downloader/peer-done")
            check("下载者收到 peer-done", peer_done.get("t") == "peer-done", str(peer_done)[:200])

            # 未知类型
            await pub.send(json.dumps({"t": "nonsense"}))
            err = await recv_json(pub, label="publisher/error")
            check("未知信令类型返回 error", err.get("t") == "error", str(err)[:200])

            # 转发到不存在的 peer
            await pub.send(json.dumps({"t": "offer", "peer": "deadbeefdead", "sdp": {}}))
            err2 = await recv_json(pub, label="publisher/error2")
            check("目标 peer 不存在返回 error", err2.get("t") == "error", str(err2)[:200])

            # 缺 peer 字段
            await pub.send(json.dumps({"t": "offer", "sdp": {}}))
            err3 = await recv_json(pub, label="publisher/error3")
            check("缺 peer 字段返回 error", err3.get("t") == "error", str(err3)[:200])


async def negative_paths(wsb: str, code: str):
    print("\n== 鉴权与边界 ==")
    try:
        async with websockets.connect(
            f"{wsb}/p2p/signal/{code}?role=publisher&token=wrong-token"
        ) as ws:
            msg = await recv_json(ws, label="bad-token")
            check(
                "错误发布令牌被拒",
                msg.get("t") == "error" and msg.get("code") == "token_invalid",
                str(msg)[:200],
            )
            await asyncio.wait_for(ws.wait_closed(), timeout=5)
            check("错误令牌连接被关闭", ws.close_code == 4401, f"close_code={ws.close_code}")
    except Exception as exc:
        check("错误发布令牌被拒", False, repr(exc))

    try:
        async with websockets.connect(f"{wsb}/p2p/signal/NOPE99?role=downloader") as ws:
            msg = await recv_json(ws, label="bad-code")
            check(
                "不存在的取件码被拒",
                msg.get("t") == "error" and msg.get("code") == "room_not_found",
                str(msg)[:200],
            )
            await asyncio.wait_for(ws.wait_closed(), timeout=5)
            check("取件码不存在时连接被关闭", ws.close_code == 4404, f"close_code={ws.close_code}")
    except Exception as exc:
        check("不存在的取件码被拒", False, repr(exc))

    try:
        async with websockets.connect(f"{wsb}/p2p/signal/{code}?role=hacker") as ws:
            msg = await recv_json(ws, label="bad-role")
            check(
                "非法 role 被拒",
                msg.get("t") == "error" and msg.get("code") == "role_invalid",
                str(msg)[:200],
            )
            await asyncio.wait_for(ws.wait_closed(), timeout=5)
            check("非法 role 连接被关闭", ws.close_code == 4400, f"close_code={ws.close_code}")
    except Exception as exc:
        check("非法 role 被拒", False, repr(exc))


async def assert_persistence(code: str):
    """核验 P2P 记录：不落文件，但统计与状态要正确落库。"""
    print("\n== 数据库落库 ==")
    from apps.base.models import FileCodes
    from tortoise import Tortoise

    from core.database import get_db_config

    await Tortoise.init(config=get_db_config())
    try:
        record = await FileCodes.filter(code=code).first()
        check("记录存在", record is not None, f"code={code}")
        if record is None:
            return
        check("标记为 P2P 记录", record.is_p2p is True)
        check("未落盘（file_path 为空）", not record.file_path, f"file_path={record.file_path!r}")
        check("未落盘（uuid_file_name 为空）", not record.uuid_file_name)
        check("size 保留真实文件大小", record.size == 1048576, f"size={record.size}")
        check("令牌只存哈希", record.p2p_token_hash and len(record.p2p_token_hash) == 64)
        check("累计服务次数已落库", record.p2p_served_count == 1, f"{record.p2p_served_count}")
        check("累计字节已落库", record.p2p_bytes_sent == 1048576, f"{record.p2p_bytes_sent}")
        check("传输方式已落库", record.p2p_last_transport == "direct", f"{record.p2p_last_transport}")
    finally:
        await Tortoise.close_connections()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="P2P P1 协议层端到端验收")
    parser.add_argument("--port", type=int, default=8899, help="测试服务端口（默认 8899）")
    parser.add_argument(
        "--reset-db",
        action="store_true",
        help="运行前清空 data/ 目录（会删除现有数据，默认不删）",
    )
    parser.add_argument(
        "--serve-probe",
        action="store_true",
        help="把浏览器探针页挂到 /probe/p2p_signal_probe.html",
    )
    parser.add_argument(
        "--keep-alive",
        action="store_true",
        help="跑完测试后保持服务运行，供浏览器验收脚本使用（Ctrl+C 结束）",
    )
    args = parser.parse_args()

    asyncio.run(main(args.port, args.serve_probe, args.reset_db, args.keep_alive))

    print("\n" + "=" * 60)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print("  FAILED:", item)
    sys.exit(1 if FAILED else 0)
