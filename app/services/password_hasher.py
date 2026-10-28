"""密碼雜湊與密碼規則（見 AGENTS.md 第 10 節）。

- Argon2id；參數集中在 `PRODUCTION_PARAMETERS`（RFC 9106 的低記憶體建議，高於 OWASP 的最低要求）。
  testing 環境使用極便宜的參數，否則數百次雜湊會讓測試慢到無法使用；正式程式碼的參數不受影響。
- 密碼先做 Unicode NFKC 正規化再雜湊與驗證（NIST SP 800-63B 建議）。**此規則一旦有真實使用者就不能改**，
  否則既有雜湊全部失效。
- 長度 10 至 128 字（正規化後的字元數），不強制字元組合；不接受控制字元與無法編碼的字元。
- 時間一致：驗證不存在的帳號時也要做一次等價的運算（`burn`），否則回應時間會洩漏帳號是否存在。
"""
from __future__ import annotations

import unicodedata
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 128

# RFC 9106 低記憶體建議：t=3、m=64 MiB、p=4。C-5 依容器實際記憶體與壓測結果調整時，只改這裡。
PRODUCTION_PARAMETERS = {"time_cost": 3, "memory_cost": 65536, "parallelism": 4}
# 僅 testing 環境：最小可用的參數
TESTING_PARAMETERS = {"time_cost": 1, "memory_cost": 8, "parallelism": 1}

LENGTH_MESSAGE = f"密碼長度須為 {MIN_PASSWORD_LENGTH} 至 {MAX_PASSWORD_LENGTH} 字。"
CONTROL_MESSAGE = "密碼不可包含控制字元或無法使用的字元。"


def normalize_password(raw: str) -> str:
    """NFKC 正規化。不去除頭尾空白：空白也是密碼的一部分。"""
    return unicodedata.normalize("NFKC", raw)


def password_problem(raw: object) -> str | None:
    """回傳密碼不合規的原因（繁體中文），合規則回傳 None。"""
    if not isinstance(raw, str):
        return "請輸入密碼。"
    normalized = normalize_password(raw)
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError:  # 例如 JSON 中夾帶的孤立代理字元
        return CONTROL_MESSAGE
    if any(unicodedata.category(ch) == "Cc" for ch in normalized):
        return CONTROL_MESSAGE
    if not MIN_PASSWORD_LENGTH <= len(normalized) <= MAX_PASSWORD_LENGTH:
        return LENGTH_MESSAGE
    return None


def parameters_for_env(env: str) -> dict[str, int]:
    return dict(TESTING_PARAMETERS if env == "testing" else PRODUCTION_PARAMETERS)


@lru_cache(maxsize=None)
def _hasher_for(time_cost: int, memory_cost: int, parallelism: int) -> PasswordHasher:
    return PasswordHasher(time_cost=time_cost, memory_cost=memory_cost, parallelism=parallelism)


@lru_cache(maxsize=None)
def _dummy_hash(time_cost: int, memory_cost: int, parallelism: int) -> str:
    """假雜湊：供「帳號不存在」等情況做等價的驗證運算。每個行程、每組參數只產生一次（公開常數，不是狀態）。"""
    return _hasher_for(time_cost, memory_cost, parallelism).hash("dummy-password-for-timing-equalisation")


class PasswordHasherService:
    """依一組參數雜湊與驗證密碼。無狀態，可在多個請求間共用。"""

    def __init__(self, parameters: dict[str, int] | None = None) -> None:
        params = parameters or PRODUCTION_PARAMETERS
        self._key = (params["time_cost"], params["memory_cost"], params["parallelism"])
        self._hasher = _hasher_for(*self._key)

    def hash(self, raw: str) -> str:
        return self._hasher.hash(normalize_password(raw))

    def verify(self, stored_hash: str | None, raw: str) -> bool:
        """驗證密碼；沒有雜湊（例如純 Google 帳號）時仍做一次假驗證並回傳 False。從不拋出例外。"""
        if not stored_hash:
            self.burn(raw)
            return False
        try:
            return self._hasher.verify(stored_hash, normalize_password(raw))
        except (VerificationError, InvalidHashError):
            return False

    def needs_rehash(self, stored_hash: str) -> bool:
        """既有雜湊的參數是否低於目前設定（登入成功時據此升級）。"""
        try:
            return self._hasher.check_needs_rehash(stored_hash)
        except InvalidHashError:
            return True

    def burn(self, raw: str) -> None:
        """做一次與驗證等價的運算並忽略結果，使「帳號不存在」的耗時與一般驗證相當。"""
        try:
            self._hasher.verify(_dummy_hash(*self._key), normalize_password(raw))
        except (VerificationError, InvalidHashError):
            pass
