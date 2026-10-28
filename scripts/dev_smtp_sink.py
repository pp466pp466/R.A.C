"""本機開發用的 SMTP 擷取伺服器：收下應用程式寄出的信，把內容印在終端機（僅供開發，絕不用於其他環境）。

用途：在本機測試信箱驗證與忘記密碼，不需要任何真實的寄信服務或帳號。信件不會被轉寄出去。

用法（PowerShell，每個步驟各開一個終端機）：
    # 1) 啟動擷取伺服器
    .\\.venv\\Scripts\\python.exe scripts\\dev_smtp_sink.py

    # 2) 讓應用程式寄到它（行程環境變數優先於 .env）
    $env:EMAIL_VERIFICATION_REQUIRED = "true"
    $env:MAIL_SMTP_HOST = "127.0.0.1"; $env:MAIL_SMTP_PORT = "1025"
    $env:MAIL_SMTP_SECURITY = "none"; $env:MAIL_FROM = "service@example.com"
    .\\.venv\\Scripts\\python.exe -m flask --app wsgi run

連結的權杖會出現在終端機印出的信件內容裡；請用完即關閉，並把終端機的內容視為機密（權杖等同一次性密碼）。
加上 `--save 資料夾` 可另外把每封信存成 .eml 檔（原始位元組，UTF-8）。
"""
from __future__ import annotations

import argparse
import email
import sys
import time
from email import policy
from pathlib import Path

from aiosmtpd.controller import Controller

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 1025


class PrintingHandler:
    def __init__(self, save_dir: Path | None) -> None:
        self._save_dir = save_dir
        self._count = 0

    async def handle_DATA(self, server, session, envelope):  # aiosmtpd 的處理常式介面
        self._count += 1
        message = email.message_from_bytes(envelope.content, policy=policy.default)
        print("=" * 64)
        print(f"第 {self._count} 封　收件人：{', '.join(envelope.rcpt_tos)}")
        print(f"標題：{message['Subject']}")
        print("-" * 64)
        # 信件內文使用 CRLF；直接印出時 Windows 會再轉換一次，造成每行多一個空行
        print(message.get_content().replace("\r\n", "\n"))
        sys.stdout.flush()
        if self._save_dir is not None:
            self._save_dir.mkdir(parents=True, exist_ok=True)
            (self._save_dir / f"mail-{self._count:03d}.eml").write_bytes(envelope.content)
        return "250 OK"


def main() -> int:
    parser = argparse.ArgumentParser(description="本機開發用的 SMTP 擷取伺服器（僅供開發）")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"監聽位址，預設 {DEFAULT_HOST}（只接受本機連線）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"監聽連接埠，預設 {DEFAULT_PORT}")
    parser.add_argument("--save", type=Path, help="另外把每封信存成 .eml 檔的資料夾")
    args = parser.parse_args()

    # 主控台編碼無法顯示的字元以替代符號輸出，而不是讓伺服器崩潰
    sys.stdout.reconfigure(errors="replace")
    controller = Controller(PrintingHandler(args.save), hostname=args.host, port=args.port)
    controller.start()
    print(f"SMTP 擷取伺服器已啟動：{args.host}:{args.port}（Ctrl+C 結束）", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        controller.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
