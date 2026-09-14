"""P2P 直传分享（信令控制面）。

模块划分：
- `config.py`  站点配置的集中读取与归一化
- `tokens.py`  发布令牌与 TURN 临时凭据
- `rooms.py`   进程内房间状态与信令转发决策（不碰 WebSocket，可单测）
- `store.py`   房间状态到 FileCodes 的落库
- `views.py`   REST 控制面（publish / status / unpublish / ice）
- `signaling.py`  WebSocket 信令端点

数据面（直连传输、流式中转）在后续阶段实现。

注意：`p2p_api` 必须在导入 `views` / `signaling` **之前**定义，
因为这两个模块会从本包回导该对象以挂载路由。
"""

from fastapi import APIRouter

p2p_api = APIRouter(prefix="/p2p", tags=["P2P"])

from apps.base.p2p import views  # noqa: E402,F401  注册 REST 路由
from apps.base.p2p import signaling  # noqa: E402,F401  注册 WS 信令路由

__all__ = ["p2p_api"]