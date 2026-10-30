"""位址守門（load-testing 規格「只對測試或預備環境執行」）。

- 預設只對本機位址執行；
- 對其他位址執行需要明確的確認參數（`--confirm-remote`）；
- **正式環境一律拒絕**，有確認參數也一樣：網域在 `PRODUCTION_DOMAINS`（取得正式網域後填入，並同步營運文件的「負載測試」節），
  另外任何主機名稱含 `production` 的位址也拒絕（部署計畫的資源名稱都帶環境名稱，例如 `cs-web-production-…`）；
- 測試帳號一律使用保留的測試網域，不會寄出真實信件。
"""
from __future__ import annotations

from urllib.parse import urlparse

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
PRODUCTION_DOMAINS: tuple[str, ...] = ()      # 取得正式網域後填入（例如 "cs.example.com"）
TEST_EMAIL_DOMAIN = "loadtest.invalid"        # RFC 2606 保留的 .invalid：永遠不會有真實的信箱


class TargetRefused(Exception):
    """位址不被允許。訊息可以直接顯示給操作者。"""


def check_target(host: str, *, confirm_remote: bool = False, production_domains: tuple[str, ...] | None = None) -> str:
    """回傳 `local` 或 `remote`；不允許時拋出 `TargetRefused`。"""
    domains = PRODUCTION_DOMAINS if production_domains is None else production_domains
    parsed = urlparse(host if "://" in host else f"http://{host}")
    name = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not name:
        raise TargetRefused("目標位址不合法：需要 http 或 https 網址。")
    if "production" in name or any(name == domain.lower() or name.endswith("." + domain.lower()) for domain in domains):
        raise TargetRefused("拒絕：負載測試不得對正式環境執行（即使有確認參數）。請改用預備環境。")
    if name in LOCAL_HOSTS:
        return "local"
    if not confirm_remote:
        raise TargetRefused("拒絕：目標不是本機位址。確認這是測試或預備環境後，加上 --confirm-remote 才會執行。")
    return "remote"
