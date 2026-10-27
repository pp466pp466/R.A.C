"""例外的安全描述：只回報類型與發生位置，絕不包含例外訊息（訊息可能夾帶機密）。"""
from __future__ import annotations

from pathlib import Path


def describe_exception(exc: BaseException) -> dict[str, str]:
    """回傳 {"error_type": 類型名稱, "error_location": "檔名:行號"}。"""
    tb = exc.__traceback__
    location = "unknown"
    while tb is not None:
        location = f"{Path(tb.tb_frame.f_code.co_filename).name}:{tb.tb_lineno}"
        tb = tb.tb_next
    return {"error_type": type(exc).__name__, "error_location": location}
