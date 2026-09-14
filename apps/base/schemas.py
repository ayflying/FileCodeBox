from pydantic import BaseModel
from typing import Optional


class SelectFileModel(BaseModel):
    code: str


class P2PPublishModel(BaseModel):
    """P2P 发布请求：只登记元数据，文件本身不上传服务器。"""

    file_name: str
    file_size: int
    expire_value: int = 1
    expire_style: str = "day"


class P2PUnpublishModel(BaseModel):
    """发布者主动下线，需持发布令牌。"""

    code: str
    publish_token: str


class InitChunkUploadModel(BaseModel):
    file_name: str
    chunk_size: int = 5 * 1024 * 1024
    file_size: int
    file_hash: str


class CompleteUploadModel(BaseModel):
    expire_value: int
    expire_style: str


# 预签名上传相关模型
class PresignUploadInitRequest(BaseModel):
    """预签名上传初始化请求"""
    file_name: str
    file_size: int
    expire_value: int = 1
    expire_style: str = "day"


class PresignUploadInitResponse(BaseModel):
    """预签名上传初始化响应"""
    upload_id: str
    upload_url: str
    mode: str  # "direct" 或 "proxy"
    save_path: str
    expires_in: int  # URL过期时间（秒）
