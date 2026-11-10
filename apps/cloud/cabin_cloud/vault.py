"""云端保管的凭证：加密落库，页面只显示掩码。密钥来自环境变量，不进数据库。"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings


class VaultError(RuntimeError):
    pass


def _fernet() -> Fernet:
    key = get_settings().secret_key
    if not key:
        raise VaultError("没有配置 CABIN_SECRET_KEY，不能保存凭证")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode("utf8")).digest()))


def seal(text: str) -> str:
    return _fernet().encrypt(text.encode("utf8")).decode("ascii")


def unseal(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf8")
    except InvalidToken as e:
        raise VaultError("凭证解不开，CABIN_SECRET_KEY 可能换过，请重新填写") from e
