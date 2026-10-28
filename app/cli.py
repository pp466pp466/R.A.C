"""`flask` 自訂指令（見 AGENTS.md 第 10、11 節：維運工作以指令實作，不用行程內計時器）。

目前只有 `create-admin`：由部署者執行一次，建立首位管理員。網頁註冊與任何 API 都無法建立管理員或提升角色。
密碼**不接受命令列參數**（會留在 shell 歷史與行程清單中）：互動時以不回顯的方式輸入，非互動環境
（例如容器的一次性工作）用 `--password-stdin` 從標準輸入讀取。信箱與密碼都不會寫入日誌或輸出。
"""
from __future__ import annotations

import sys

import click
from flask import Flask, current_app

from app.runtime import get_clock, get_config
from app.services.errors import ServiceError, ValidationError
from app.services.registry import build_auth_service


def _describe(error: ServiceError) -> str:
    if isinstance(error, ValidationError) and error.fields:
        return "；".join(error.fields.values())
    return error.message


def register_cli(app: Flask) -> None:
    @app.cli.command("create-admin")
    @click.option("--email", help="管理員的電子郵件；省略時會詢問。")
    @click.option("--display-name", help="管理員的顯示名稱；省略時會詢問。")
    @click.option(
        "--password-stdin",
        is_flag=True,
        help="從標準輸入讀取密碼（一行），供非互動環境使用；此時 --email 與 --display-name 必須以參數提供。",
    )
    def create_admin(email: str | None, display_name: str | None, password_stdin: bool) -> None:
        """建立首位管理員帳號（角色 admin、狀態 active、信箱視為已驗證）。"""
        if password_stdin:
            if not email or not display_name:
                raise click.UsageError("使用 --password-stdin 時，必須以 --email 與 --display-name 提供信箱與顯示名稱。")
            # Windows PowerShell 5.1 以管線傳字串給外部程式時，會在最前面加上 UTF-8 BOM（EF BB BF）；
            # 不去掉的話，密碼會被存成「BOM＋密碼」，之後輸入正確的密碼也對不上（與 AGENTS.md 第 8 節的 utf-8-sig 同一類問題）
            password = sys.stdin.readline().lstrip("﻿").rstrip("\r\n")
        else:
            email = email or click.prompt("電子郵件")
            display_name = display_name or click.prompt("顯示名稱")
            password = click.prompt("密碼", hide_input=True, confirmation_prompt=True)

        factory = current_app.extensions["db_session_factory"]
        with factory() as session:
            service = build_auth_service(session, config=get_config(), clock=get_clock())
            try:
                admin = service.create_admin(email=email, display_name=display_name, password=password)
            except ServiceError as exc:
                session.rollback()
                raise click.ClickException(_describe(exc)) from None
            session.commit()
        click.echo(f"已建立管理員帳號（識別碼 {admin.public_id}）。")
