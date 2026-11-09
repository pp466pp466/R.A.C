"""臨時診斷（用完即刪，不是正式功能）：在 Render 上確認為什麼連不上資料庫。

用法：Render 的 Docker Command 設成 `python -m app._diag_db`。印出 DIAG 開頭的幾行後，交棒給 gunicorn，服務照常運作。
只印驅動清單、主機解析、連線結果與錯誤訊息；**不印密碼**（只印長度）。
"""
import os
import socket
import sys
import time


def say(*parts: object) -> None:
    print("DIAG", *parts, flush=True)


def main() -> None:
    import pyodbc
    from sqlalchemy import create_engine, make_url, text

    say("python", sys.version.split()[0], "pyodbc", pyodbc.version)
    say("drivers", pyodbc.drivers())
    raw = os.environ.get("DATABASE_URL", "")
    say("DATABASE_URL 長度", len(raw), "含空白", any(c.isspace() for c in raw), "含引號", ('"' in raw or "'" in raw))
    try:
        url = make_url(raw)
    except Exception as error:
        say("網址解析失敗", type(error).__name__)
        return
    say("網址", "driver=", url.drivername, "host=", url.host, "db=", url.database, "user=", url.username,
        "密碼長度=", len(url.password or ""), "query=", dict(url.query))

    try:
        started = time.monotonic()
        infos = socket.getaddrinfo(url.host, 1433, type=socket.SOCK_STREAM)
        say("DNS 解析成功", sorted({info[4][0] for info in infos}), f"{time.monotonic() - started:.2f}s")
        started = time.monotonic()
        with socket.create_connection((url.host, 1433), timeout=15):
            say("TCP 1433 可連線", f"{time.monotonic() - started:.2f}s")
    except Exception as error:
        say("DNS/TCP 失敗", type(error).__name__, str(error)[:200])

    started = time.monotonic()
    try:
        engine = create_engine(url, connect_args={"timeout": 60})
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        say("SQLAlchemy 連線成功", f"{time.monotonic() - started:.2f}s")
    except Exception as error:
        original = getattr(error, "orig", error)
        say("SQLAlchemy 連線失敗", type(error).__name__, type(original).__name__, f"{time.monotonic() - started:.2f}s")
        say("原始錯誤", str(original.args)[:800])


if __name__ == "__main__":
    try:
        main()
    except Exception as error:                        # 診斷本身出錯也不能讓服務起不來
        say("診斷程式例外", type(error).__name__, str(error)[:300])
    if not os.environ.get("DIAG_NO_EXEC"):
        os.execvp("gunicorn", ["gunicorn", "--config", "gunicorn.conf.py", "wsgi:app"])
