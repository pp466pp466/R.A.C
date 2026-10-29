"""`flask` 自訂指令（見 AGENTS.md 第 10、11 節：維運工作以指令實作，不用行程內計時器）。

三個指令：
- `create-admin`：由部署者執行一次，建立首位管理員。網頁註冊與任何 API 都無法建立管理員或提升角色。
  密碼**不接受命令列參數**（會留在 shell 歷史與行程清單中）：互動時以不回顯的方式輸入，非互動環境
  （例如容器的一次性工作）用 `--password-stdin` 從標準輸入讀取。信箱與密碼都不會寫入日誌或輸出。
- `purge-inactive-accounts`：由平台排程器觸發（不使用行程內計時器），刪除連續 12 個月未登入的一般帳號；
  `--dry-run` 只列出筆數。輸出只有筆數，不含任何個資。
- `age-stats`：各年齡區間的人數（供定期察看用戶群），只有計數，沒有任何個別使用者的資料。
"""
from __future__ import annotations

import sys

import click
from flask import Flask, current_app

from app.runtime import get_clock, get_config
from app.services.errors import ServiceError, ValidationError
from app.runtime import get_age_policy
from app.services.registry import build_account_retention_service, build_age_stats_service, build_auth_service


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


    @app.cli.command("purge-inactive-accounts")
    @click.option("--dry-run", is_flag=True, help="只列出符合條件的帳號數量，不刪除任何資料。")
    def purge_inactive_accounts(dry_run: bool) -> None:
        """刪除連續 12 個月未登入的一般帳號（連同其生日與年齡區間）。管理員不納入；可重複執行，結果相同。"""
        factory = current_app.extensions["db_session_factory"]
        with factory() as session:
            result = build_account_retention_service(session, clock=get_clock()).purge_inactive(dry_run=dry_run)
        if dry_run:
            click.echo(f"預覽：符合條件的帳號 {result.matched} 個（未刪除任何資料）。")
        else:
            click.echo(f"已刪除 {result.deleted} 個連續 12 個月未登入的帳號。")

    @app.cli.command("age-stats")
    def age_stats() -> None:
        """各年齡區間的人數與尚未完成年齡確認的人數（只有計數，沒有任何個別使用者的資料）。"""
        factory = current_app.extensions["db_session_factory"]
        with factory() as session:
            stats = build_age_stats_service(session, age_policy=get_age_policy()).summary()
        click.echo("年齡區間人數")
        for label, number in stats.by_range:
            click.echo(f"  {label}：{number}")
        click.echo(f"  尚未完成年齡確認：{stats.not_completed}")
        click.echo(f"  合計：{stats.total}")
