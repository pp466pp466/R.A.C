"""`flask` 自訂指令（見 AGENTS.md 第 10、11 節：維運工作以指令實作，不用行程內計時器）。

十個指令：
- `llm-probe`：開發用，不屬於維運。呼叫 RPG 敘事用途的模型端點一次，印出回覆、模型、延遲、token 與結束原因，
  讓人在 RPG 實作之前先評估端點好不好用（含結構化輸出的穩定度）。只能在 development 與 testing 執行；**不寫帳本與任何資料表**，
  不顯示金鑰，也不印供應商的錯誤本文。
- `create-admin`：由部署者執行一次，建立首位管理員。網頁註冊與任何 API 都無法建立管理員或提升角色。
  密碼**不接受命令列參數**（會留在 shell 歷史與行程清單中）：互動時以不回顯的方式輸入，非互動環境
  （例如容器的一次性工作）用 `--password-stdin` 從標準輸入讀取。信箱與密碼都不會寫入日誌或輸出。
- `purge-inactive-accounts`：由平台排程器觸發（不使用行程內計時器），刪除連續 12 個月未登入的一般帳號；
  `--dry-run` 只列出筆數。輸出只有筆數，不含任何個資。
- `purge-unconfirmed-accounts`：由平台排程器**每小時**觸發，刪除玩家首次以 Google 登入後 24 小時內沒有完成年齡確認的帳號
  （管理員與沒有期限記錄的舊帳號不納入）；`--dry-run` 只列出筆數。輸出只有筆數，不含任何個資。
- `check-alerts`：由平台排程器每 5 分鐘觸發，評估五項只靠資料庫量得到的營運告警（全站用量、模型錯誤率、模型設定錯誤、
  對話延遲 p95、帳號鎖定激增），狀態變化時寄電子郵件給 `ALERT_EMAIL_TO`（進入、升級、每 6 小時提醒、恢復各一封）；
  `--dry-run` 與沒有設定收件人時只評估並顯示結果，不寄信也不改變狀態。輸出只有告警名稱、狀態與數值；寄信失敗或某項評估失敗時以非零代碼結束。
- `purge-expired-data`：由平台排程器觸發，清除超過保存期限的資料（對話與訊息 12 個月、用量帳本 12 個月、失敗的模型原文 14 天、
  驗證權杖到期後 7 天、管理稽核 24 個月、帳號鎖定計次 7 天、中斷或被擋回合的部分內容 14 天、RPG 存檔最後活動後 12 個月）；`--dry-run` 只列出各類筆數。輸出只有各類的筆數，不含任何內容；有任一類失敗時以非零代碼結束。
- `execute-account-deletions`：由平台排程器觸發，刪除「申請刪除、寬限期（7 天）已滿」的帳號（與閒置清除共用同一個抹除出口）；
  `--dry-run` 只列出筆數。輸出只有筆數，不含任何個資。
- `age-stats`：各年齡區間的人數（供定期察看用戶群），只有計數，沒有任何個別使用者的資料。
- `seed-scripts`：**部署時**由人工或部署流程執行（不排程、不在應用程式啟動時執行；部署順序是備份、遷移、部署、種子），
  讀取隨映像封裝的內建劇本資料檔（`app/data/scripts/*.json`），全部驗證通過才在單一交易內寫入（全有或全無），以種子鍵與內容雜湊
  冪等地建立、更新（新增版本並取代舊版）或略過；`--dry-run` 只列出結果。輸出只有種子鍵與筆數，不含劇本內容；
  資料不合規時以非零代碼結束並指出檔名與欄位路徑，沒有寫入任何資料。
- `replay-deletions`：**備份還原後、對外開放流量之前**手動執行（不得排程），依 `deletion_tombstones`（以及由日誌取出的
  `使用者編號,刪除時間` 檔案，`--ids-file`）重新抹除復活的帳號；只抹除建立時間不晚於刪除時間且不是管理員的帳號，
  `--dry-run` 只列筆數。檔案先整體驗證，格式錯誤時在抹除任何帳號之前失敗，只指出行號；輸出只有筆數，不含檔案路徑與內容。
"""
from __future__ import annotations

import sys

import click
from flask import Flask, current_app

from app.config import DEV_TOOL_ENVS
from app.llm.types import LLMBlockedError, LLMConfigError, LLMContent, LLMRequest, LLMTransientError, LLMValidationError
from app.runtime import get_clock, get_config, get_mail_sender, get_rpg_llm_provider
from app.services.errors import ServiceError, ValidationError
from app.runtime import get_age_policy
from app.services.alert_messages import ALERT_NAMES, SEVERITY_LABELS, format_threshold, format_value
from app.services.llm_output import parse_llm_json
from app.services.registry import (
    build_account_deletion_executor, build_account_retention_service, build_age_stats_service, build_auth_service,
    build_alert_service, build_data_retention_service, build_deletion_replay_service, build_script_seed_service,
)
from app.services.cs_prompt import leak_marker
from app.services.deletion_replay_service import ReplayFileError, parse_replay_file
from app.services.script_seed_service import SEED_CREATED, SEED_UNCHANGED, SEED_UPDATED, SeedDataError

# ---- 指令分類（見 deployment-plan 規格「排程指令的頻率與失敗處理」）----
# 由平台排程器觸發的指令與頻率（cron，時區 Asia/Taipei）。部署計畫（scripts/launch_plan.py）從這裡產生排程，
# 一致性測試（tests/test_scheduled_commands.py）要求每個註冊的指令不是排程類就是人工操作類：新增指令而兩邊都沒登記會失敗。
# 每日的三個錯開時間，避免同時佔用資料庫連線；check-alerts 的窗口刻意重疊（見營運文件）。
SCHEDULED_COMMANDS: dict[str, str] = {
    "check-alerts": "*/5 * * * *",
    "purge-unconfirmed-accounts": "0 * * * *",          # 每小時：24 小時的補填期限，逾時帳號不該停留太久
    "purge-inactive-accounts": "0 3 * * *",
    "execute-account-deletions": "20 3 * * *",
    "purge-expired-data": "40 3 * * *",
}
# 人工操作的指令：開發用、一次性或還原時才執行，不得排程。
MANUAL_COMMANDS: tuple[str, ...] = ("llm-probe", "create-admin", "age-stats", "replay-deletions", "seed-scripts")

# ---- llm-probe：固定的無害文字與結構描述（繁體中文），不含任何個資 ----
PROBE_SYSTEM = "你是一位說書人。請一律使用繁體中文（台灣用語）回答，內容保持輕鬆無害。"
PROBE_TEXT = "請用兩三句話，描寫一座清晨剛醒來的海邊小鎮。"
PROBE_STRUCTURED_SYSTEM = "你是一位說書人。請一律使用繁體中文（台灣用語），並嚴格依指定的 JSON 格式回覆。"
PROBE_STRUCTURED_TEXT = "請為一座清晨的海邊小鎮取一個標題，並選出它的氣氛。"
PROBE_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "mood": {"type": "string", "enum": ["calm", "tense", "joyful"]}},
    "required": ["title", "mood"],
}
PROBE_MAX_TEXT_CHARS = 500

# purge-expired-data 輸出的類別名稱（對應 lifecycle_events 的固定列舉）
RETENTION_LABELS = {
    "conversations": "對話與訊息",
    "ledger": "用量帳本",
    "error_detail": "失敗的模型原文",
    "auth_tokens": "驗證權杖",
    "admin_audit": "管理稽核紀錄",
    "lock_strikes": "帳號鎖定計次",
    "turn_content": "中斷或被擋回合的部分內容",
    "sessions": "RPG 存檔與回合",
}


def _describe(error: ServiceError) -> str:
    if isinstance(error, ValidationError) and error.fields:
        return "；".join(error.fields.values())
    return error.message


ALERT_ACTION_LABELS = {
    "firing": "進入告警", "escalate": "升級", "remind": "提醒", "resolve": "已恢復", "lower": "降級（不寄信）", "none": "沒有動作",
}
ALERT_OUTCOME_LABELS = {
    "notified": "已通知", "delivery_failed": "通知失敗", "quiet": "已更新狀態（不寄信）", "unchanged": "狀態不變",
    "lost_race": "已由另一個檢查處理", "check_failed": "評估失敗",
}


def describe_alert_result(result, *, preview: bool) -> str:
    """一項告警的一行輸出：名稱、狀態、數值與門檻，以及（預覽時）正式執行會做的動作或（正式執行時）實際結果。沒有任何個資。"""
    name = ALERT_NAMES[result.key]
    evaluation = result.evaluation
    if evaluation is None:
        return f"{name}：{ALERT_OUTCOME_LABELS[result.outcome]}"
    if evaluation.hold:
        state = "樣本不足，維持原狀"
    elif evaluation.severity is None:
        state = "正常"
    else:
        state = f"{SEVERITY_LABELS[evaluation.severity]}（目前 {format_value(result.key, evaluation.value)}，門檻 {format_threshold(result.key, evaluation.threshold)}）"
    if preview:
        return f"{name}：{state}；正式執行會：{ALERT_ACTION_LABELS[result.action]}"
    tail = ALERT_OUTCOME_LABELS[result.outcome]
    if result.action in ("firing", "escalate", "remind", "resolve"):
        tail = f"{ALERT_ACTION_LABELS[result.action]}，{tail}"
    return f"{name}：{state}；{tail}"


def _count(value: int | None) -> str:
    """供應商沒有提供的用量顯示為「未提供」，不是 0。"""
    return "未提供" if value is None else str(value)


def register_cli(app: Flask) -> None:
    @app.cli.command("llm-probe")
    @click.option("--text", default=None, help=f"送給模型的短文字（最多 {PROBE_MAX_TEXT_CHARS} 字）；省略時使用固定的無害文字。")
    @click.option("--structured", is_flag=True, help="改以固定的結構描述呼叫，並回報輸出是否通過驗證（評估 JSON 穩定度）。")
    def llm_probe(text: str | None, structured: bool) -> None:
        """（開發用）呼叫 RPG 敘事用途的模型端點一次，印出回覆與用量；不寫帳本與任何資料表。"""
        config = get_config()
        if config.env not in DEV_TOOL_ENVS:
            raise click.ClickException("這個指令只能在 development 與 testing 執行。")
        if text is not None and not 0 < len(text) <= PROBE_MAX_TEXT_CHARS:
            raise click.UsageError(f"--text 必須是 1 至 {PROBE_MAX_TEXT_CHARS} 個字元。")
        provider = get_rpg_llm_provider()
        if provider is None:
            raise click.ClickException("RPG 用途尚未設定：請在 .env 設定 OPENROUTER_API_KEY 與 OPENROUTER_MODEL。")

        settings = config.rpg_llm.llm if config.rpg_llm else config.llm
        request = LLMRequest(
            model=settings.primary_model,
            system_instruction=PROBE_STRUCTURED_SYSTEM if structured else PROBE_SYSTEM,
            contents=(LLMContent("user", (text or (PROBE_STRUCTURED_TEXT if structured else PROBE_TEXT),)),),
            response_schema=PROBE_SCHEMA if structured else None,
            timeout_seconds=settings.call_timeout_seconds,
            max_output_tokens=settings.max_output_tokens,
        )
        try:
            response = provider.generate(request)
        # 失敗只印分類與狀態碼：轉接器本來就不保留供應商的回應本文，這裡也不猜測或轉述它
        except LLMTransientError as exc:
            raise click.ClickException(f"呼叫失敗（暫時性錯誤：{exc.kind}，狀態碼 {exc.http_status or '無'}，耗時 {exc.latency_ms} 毫秒）。") from None
        except LLMBlockedError as exc:
            raise click.ClickException(f"呼叫被內容安全過濾阻擋（{exc.phase}）。") from None
        except LLMConfigError as exc:
            raise click.ClickException(
                f"設定錯誤（狀態碼 {exc.http_status or '無'}）：請檢查金鑰、模型名稱與餘額；模型可能已下架或沒有可用的端點。"
            ) from None

        click.echo(f"模型：{response.model}")
        click.echo(f"延遲：{response.latency_ms} 毫秒")
        click.echo(
            f"token：輸入 {_count(response.prompt_tokens)}、輸出 {_count(response.completion_tokens)}、思考 {_count(response.thinking_tokens)}"
        )
        click.echo(f"結束原因：{response.finish_reason}")
        click.echo("--- 回覆 ---")
        click.echo(response.text)
        if structured:
            click.echo("--- 結構化驗證 ---")
            try:
                parse_llm_json(response.text, PROBE_SCHEMA)
            except LLMValidationError as exc:
                click.echo(f"未通過（原因分類：{exc.reason}）")
                raise click.exceptions.Exit(1) from None
            click.echo("通過")

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

    @app.cli.command("purge-unconfirmed-accounts")
    @click.option("--dry-run", is_flag=True, help="只列出逾時未完成年齡確認的帳號數量，不刪除任何資料。")
    def purge_unconfirmed_accounts(dry_run: bool) -> None:
        """刪除建立後 24 小時內沒有完成年齡確認的玩家帳號。管理員與沒有期限記錄的帳號不納入；可重複執行，結果相同。"""
        factory = current_app.extensions["db_session_factory"]
        with factory() as session:
            result = build_account_retention_service(session, clock=get_clock()).purge_unconfirmed(dry_run=dry_run)
        if dry_run:
            click.echo(f"預覽：逾時未完成年齡確認的帳號 {result.matched} 個（未刪除任何資料）。")
        else:
            click.echo(f"已刪除 {result.deleted} 個逾時未完成年齡確認的帳號。")

    @app.cli.command("execute-account-deletions")
    @click.option("--dry-run", is_flag=True, help="只列出寬限期已滿、待刪除的帳號數量，不刪除任何資料。")
    def execute_account_deletions(dry_run: bool) -> None:
        """刪除使用者申請刪除、且寬限期（7 天）已滿的帳號。剛撤銷的帳號不會被刪；可重複執行，結果相同。"""
        factory = current_app.extensions["db_session_factory"]
        with factory() as session:
            result = build_account_deletion_executor(session, clock=get_clock()).execute_due(dry_run=dry_run)
        if dry_run:
            click.echo(f"預覽：寬限期已滿、待刪除的帳號 {result.matched} 個（未刪除任何資料）。")
        else:
            click.echo(f"已刪除 {result.deleted} 個寬限期已滿的帳號。")

    @app.cli.command("check-alerts")
    @click.option("--dry-run", is_flag=True, help="只評估並顯示結果（含正式執行時會做的動作），不寄信、不建立或改變任何狀態。")
    def check_alerts(dry_run: bool) -> None:
        """評估營運告警並在狀態變化時寄信。可重複執行：狀態沒變就不會重複寄信。"""
        factory = current_app.extensions["db_session_factory"]
        with factory() as session:
            report = build_alert_service(session, config=get_config(), clock=get_clock(), mail_sender=get_mail_sender()).run(dry_run=dry_run)
        if dry_run:
            click.echo("預覽（沒有寄信，也沒有改變任何狀態）：")
        elif not report.recipients_configured:
            click.echo("沒有設定告警收件人（ALERT_EMAIL_TO），只評估並顯示結果，不寄信也不改變狀態：")
        else:
            click.echo("告警檢查結果：")
        for result in report.results:
            click.echo(f"  {describe_alert_result(result, preview=report.read_only)}")
        if report.failed:
            raise click.ClickException("以下告警失敗：" + "、".join(ALERT_NAMES[key] for key in report.failed))

    @app.cli.command("purge-expired-data")
    @click.option("--dry-run", is_flag=True, help="只列出各類符合條件的筆數，不刪除或改變任何資料。")
    def purge_expired_data(dry_run: bool) -> None:
        """清除超過保存期限的資料。各類獨立處理，某一類失敗不影響其他類別（但指令會以非零代碼結束）；可重複執行，結果相同。"""
        factory = current_app.extensions["db_session_factory"]
        with factory() as session:
            report = build_data_retention_service(session, clock=get_clock()).run(dry_run=dry_run)
        click.echo("預覽（未改變任何資料）：" if dry_run else "期限清除結果：")
        for result in report.results:
            label = RETENTION_LABELS[result.category]
            click.echo(f"  {label}：{'失敗' if result.failed else str(result.matched) + ' 筆'}")
        if report.failed:
            raise click.ClickException("以下類別清除失敗：" + "、".join(RETENTION_LABELS[c] for c in report.failed))

    @app.cli.command("replay-deletions")
    @click.option("--dry-run", is_flag=True, help="只列出將抹除與略過的筆數，不改變任何資料。")
    @click.option("--ids-file", type=click.Path(dir_okay=False), help="由日誌取出的檔案，每行「使用者編號,刪除時間（ISO 8601）」，涵蓋備份之後才發生的刪除。")
    def replay_deletions(dry_run: bool, ids_file: str | None) -> None:
        """備份還原後依刪除紀錄重新抹除復活的帳號。**必須在還原後、對外開放流量之前執行**；可重複執行，結果相同。"""
        extra = []
        if ids_file:
            try:
                with open(ids_file, encoding="utf-8-sig") as handle:   # 讀取一律 utf-8-sig（自動去除 BOM）
                    text = handle.read()
            except (OSError, UnicodeDecodeError):
                raise click.ClickException("無法讀取指定的檔案（找不到、沒有權限或不是 UTF-8 文字）。") from None
            try:
                extra = parse_replay_file(text)   # 整份先驗證：格式錯誤時在抹除任何帳號之前失敗
            except ReplayFileError as error:
                raise click.ClickException(str(error)) from None
        factory = current_app.extensions["db_session_factory"]
        with factory() as session:
            report = build_deletion_replay_service(session).run(extra, dry_run=dry_run)
        click.echo("預覽（未改變任何資料）：" if dry_run else "重新抹除結果：")
        click.echo(f"  {'將抹除' if dry_run else '已抹除'}：{report.erased} 個帳號")
        click.echo(f"  略過（編號已被還原後的新使用者重新使用）：{report.reissued} 個")
        click.echo(f"  略過（管理員）：{report.admin} 個")
        click.echo(f"  已經不存在：{report.absent} 個")

    @app.cli.command("seed-scripts")
    @click.option("--dry-run", is_flag=True, help="只列出將建立、更新或沒有變動的內建劇本，不改變任何資料。")
    def seed_scripts(dry_run: bool) -> None:
        """建立或更新內建（首發）劇本。**部署後執行**（備份、遷移、部署、種子）；可重複執行，內容沒變就不會有任何變動。"""
        factory = current_app.extensions["db_session_factory"]
        marker = leak_marker(get_config().secret_key.reveal())
        with factory() as session:
            try:
                report = build_script_seed_service(session, clock=get_clock(), forbidden_markers=(marker,)).run(dry_run=dry_run)
            except SeedDataError as error:
                raise click.ClickException("內建劇本資料不合規，沒有寫入任何資料：\n" + "\n".join(f"  {problem}" for problem in error.problems)) from None
        label = {SEED_CREATED: "建立", SEED_UPDATED: "更新（新增版本）", SEED_UNCHANGED: "沒有變動"}
        click.echo("預覽（未改變任何資料）：" if dry_run else "內建劇本：")
        click.echo(f"  建立 {report.created}、更新 {report.updated}、沒有變動 {report.unchanged}（共 {len(report.results)} 份）")
        for result in report.results:
            click.echo(f"  {result.seed_key}：{label[result.action]}")

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
