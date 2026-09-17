# @Time    : 2023/8/15 09:51
# @Author  : Lan
# @File    : settings.py
# @Software: PyCharm
from pathlib import Path

ADMIN_SESSION_EXPIRE_DEFAULT = 30 * 24 * 60 * 60
ADMIN_SESSION_EXPIRE_MIN = 24 * 60 * 60
ADMIN_SESSION_EXPIRE_MAX = 365 * 24 * 60 * 60

BASE_DIR = Path(__file__).resolve().parent.parent
data_root = BASE_DIR / "data"

if not data_root.exists():
    data_root.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    "file_storage": "local",
    "storage_path": "",
    "storageLimit": 0,
    "name": "文件快递柜 - FileCodeBox",
    "description": "开箱即用的文件快传系统",
    "notify_title": "系统通知",
    "notify_content": '欢迎使用 FileCodeBox，本程序开源于 <a href="https://github.com/vastsa/FileCodeBox" target="_blank">Github</a> ，欢迎Star和Fork。',
    "page_explain": "请勿上传或分享违法内容。根据《中华人民共和国网络安全法》、《中华人民共和国刑法》、《中华人民共和国治安管理处罚法》等相关规定。 传播或存储违法、违规内容，会受到相关处罚，严重者将承担刑事责任。本站坚决配合相关部门，确保网络内容的安全，和谐，打造绿色网络环境。",
    "keywords": "FileCodeBox, 文件快递柜, 口令传送箱, 匿名口令分享文本, 文件",
    "s3_access_key_id": "",
    "s3_secret_access_key": "",
    "s3_bucket_name": "",
    "s3_endpoint_url": "",
    "s3_region_name": "auto",
    "s3_signature_version": "s3v2",
    "s3_hostname": "",
    "s3_addressing_style": "auto",
    "s3_proxy": 0,
    "max_save_seconds": 0,
    "aws_session_token": "",
    "onedrive_domain": "",
    "onedrive_client_id": "",
    "onedrive_username": "",
    "onedrive_password": "",
    "onedrive_root_path": "filebox_storage",
    "onedrive_proxy": 0,
    "webdav_root_path": "filebox_storage",
    "webdav_proxy": 0,
    "admin_token": "",
    "jwt_secret": "",
    "adminSessionExpire": ADMIN_SESSION_EXPIRE_DEFAULT,
    "openUpload": 1,
    "uploadSize": 1024 * 1024 * 10,
    "allowed_file_types": ["*"],
    "expireStyle": ["day", "hour", "minute", "forever", "count"],
    "code_generate_type": "secret",
    "uploadMinute": 1,
    "enableChunk": 0,
    "webdav_url": "",
    "webdav_password": "",
    "webdav_username": "",
    "opacity": 0.9,
    "background": "",
    "uploadCount": 10,
    "themesChoices": [
        {
            "name": "2023",
            "key": "themes/2023",
            "author": "Lan",
            "version": "1.0",
        },
        {
            "name": "2024",
            "key": "themes/2024",
            "author": "Lan",
            "version": "1.0",
        },
    ],
    "themesSelect": "themes/2024",
    "errorMinute": 1,
    "errorCount": 10,
    "loginCount": 5,
    "loginMinute": 15,
    "serverWorkers": 1,
    "serverHost": "0.0.0.0",
    "serverPort": 12345,
    "showAdminAddr": 0,
    "robotsText": "User-agent: *\nDisallow: /",
    "trustedProxies": [],
    # ---- P2P 直传（详见 docs/p2p-design.md §5）----
    # 站点级总开关
    "enableP2P": 1,
    # 上传页勾选框默认状态
    "p2pDefaultChecked": 1,
    # P2P 单文件上限，服务端硬闸；不计入 storageLimit（见决策 D4/D9）
    # 0 = 不限制：P2P 文件不落服务端磁盘，大小只受两端设备约束
    "p2pMaxSize": 0,
    # 是否允许流式中转兜底
    "p2pRelayEnabled": 1,
    # 单分享并发下载者上限
    "p2pMaxPeers": 3,
    # 发布端心跳超时秒数，超过即判离线
    "p2pHeartbeatTimeout": 30,
    # 无人在线的房间回收秒数
    "p2pRoomTtl": 900,
    # STUN 列表。留空 = 跟随站点访问入口自动派生（stun:<访问域名或IP>:p2pStunPort），
    # 指向站点内置的自建 STUN，不依赖任何第三方 STUN。
    # 如需指定外部 STUN，在此显式写入（如 ["stun:stun.example.com:3478"]）即覆盖自动派生。
    "p2pStunUrls": [],
    # 内置 STUN 开关（随站点进程启停，RFC 5389 Binding）
    "p2pStunEnabled": 1,
    # 内置 STUN 监听端口
    "p2pStunPort": 3478,
    # TURN 列表（如 turn:your.host:3478）
    "p2pTurnUrls": [],
    # coturn static-auth-secret，仅服务端可见，绝不下发前端
    "p2pTurnSecret": "",
    # TURN 临时凭据有效期（秒）
    "p2pTurnTtl": 7200,
}


class Settings:
    def __init__(self, defaults=None):
        self.default_config = defaults or {}
        self.user_config = {}

    def __getattr__(self, attr):
        if attr in self.user_config:
            return self.user_config[attr]
        if attr in self.default_config:
            return self.default_config[attr]
        raise AttributeError(
            f"'{self.__class__.__name__}' object has no attribute '{attr}'"
        )

    def __setattr__(self, key, value):
        if key in ["default_config", "user_config"]:
            super().__setattr__(key, value)
        else:
            self.user_config[key] = value

    def items(self):
        return {**self.default_config, **self.user_config}.items()


settings = Settings(DEFAULT_CONFIG)
