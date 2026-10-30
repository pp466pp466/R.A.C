"""platform-alerts 規格：平台負責的告警定義（5xx、探測、寄信失敗、排程失敗與停擺）——門檻、窗口、管道、來源事件真實存在。"""
import json
import re

import pytest

from app.config import PROJECT_ROOT
from app.observability import alert_events, security_events
from tests.support.scripts import load_script

plan = load_script("launch_plan")

EMAIL = "oncall.person@zz-host.example"
PLAIN = {
    "BASE_URL": "https://cs.example.com", "MAIL_SMTP_HOST": "smtp.example.com", "MAIL_SMTP_PORT": "587",
    "MAIL_FROM": "service@example.com", "ALERT_EMAIL_TO": "ops@example.com",
}


def target(env="staging", emails=(EMAIL,)):
    return plan.Target(project="demo-project", region="asia-east1", env=env, sql_instance=f"cs-sql-{env}",
                       image_tag="abc1234", notify_emails=tuple(emails))


def alerts(env="staging", emails=(EMAIL,)):
    return plan.build_plan(target(env, emails), PLAIN, groups=("alerts",))


def policies(env="staging"):
    return plan.alert_definitions(target(env))


def threshold(policy, index=0):
    return policy["conditions"][index]["conditionThreshold"]


# ---- 5xx 比率 ---------------------------------------------------------------------------


def test_the_five_xx_policy_needs_both_a_ratio_above_five_percent_and_at_least_twenty_requests():
    policy = policies()["five-xx-ratio"]
    assert policy["combiner"] == "AND"
    ratio, volume = threshold(policy, 0), threshold(policy, 1)
    assert (ratio["comparison"], ratio["thresholdValue"]) == ("COMPARISON_GT", 0.05)
    assert (volume["comparison"], volume["thresholdValue"]) == ("COMPARISON_GE", 20)
    assert ratio["aggregations"][0]["alignmentPeriod"] == "300s" == volume["aggregations"][0]["alignmentPeriod"]
    assert '"5xx"' in ratio["filter"] and "response_code_class" not in ratio["denominatorFilter"]
    assert "run.googleapis.com/request_count" in ratio["denominatorFilter"] and "cs-web-staging" in ratio["denominatorFilter"]


def test_the_five_xx_source_is_the_platform_request_metric_not_the_application_database():
    text = json.dumps(policies()["five-xx-ratio"])
    assert "request_count" in text and "alert_states" not in text and "llm_call_logs" not in text


# ---- 存活與就緒 -------------------------------------------------------------------------


def test_healthz_and_readyz_are_probed_every_minute_and_alert_after_three_consecutive_failures():
    steps = {step.id: step for step in alerts()}
    for key, path in (("healthz", "/healthz"), ("readyz", "/readyz")):
        probe = steps[f"uptime-{key}"].argv
        assert f"--path={path}" in probe and "--period=1" in probe and "--protocol=https" in probe
        policy = policies()[f"uptime-{key}"]
        assert threshold(policy)["duration"] == "180s" and threshold(policy)["aggregations"][0]["alignmentPeriod"] == "60s"
        assert path in policy["displayName"]
    assert "資料庫" in policies()["uptime-readyz"]["displayName"], "就緒失敗的通知要分辨是資料庫問題"


def test_the_probes_need_no_login_and_run_from_outside_the_service():
    probe = next(step for step in alerts() if step.id == "uptime-healthz").argv
    assert probe[:4] == ("gcloud", "monitoring", "uptime", "create")
    assert not any("auth" in part.lower() or "header" in part.lower() for part in probe)


def test_the_uptime_policies_reference_the_probe_ids_that_are_known_only_at_run_time():
    text = json.dumps(policies()["uptime-healthz"])
    assert "{{uptime-healthz|last}}" in text
    step_ids = [step.id for step in alerts()]
    assert step_ids.index("uptime-healthz") < step_ids.index("policy-uptime-healthz")


# ---- 寄信失敗與停擺 ---------------------------------------------------------------------


def test_mail_failures_alert_at_three_in_five_minutes_and_the_source_event_is_the_real_one():
    policy = policies()["mail-send-failed"]
    assert (threshold(policy)["comparison"], threshold(policy)["thresholdValue"]) == ("COMPARISON_GE", 3)
    assert threshold(policy)["aggregations"][0]["alignmentPeriod"] == "300s"
    metric_step = next(step for step in alerts() if step.id == "log-metric-mail-send-failed")
    log_filter = next(part for part in metric_step.argv if part.startswith("--log-filter="))
    assert f'jsonPayload.event="{security_events.MAIL_SEND_FAILED}"' in log_filter and security_events.MAIL_SEND_FAILED == "mail.send_failed"


def test_the_mail_failure_event_is_really_logged_by_the_application():
    users = [path for path in (PROJECT_ROOT / "app").rglob("*.py") if "MAIL_SEND_FAILED" in path.read_text(encoding="utf-8-sig") and path.name != "security_events.py"]
    assert users, "沒有任何程式記錄 mail.send_failed，平台告警會永遠不觸發"


def test_check_alerts_has_a_stall_alert_based_on_the_heartbeat_event():
    policy = policies()["check-alerts-stalled"]
    absent = policy["conditions"][0]["conditionAbsent"]
    assert absent["duration"] == "900s" and "alert-check-completed" in absent["filter"]
    metric_step = next(step for step in alerts() if step.id == "log-metric-alert-check-completed")
    log_filter = next(part for part in metric_step.argv if part.startswith("--log-filter="))
    assert alert_events.EVENT_CHECK_COMPLETED == "alert.check_completed"
    assert f'jsonPayload.event="{alert_events.EVENT_CHECK_COMPLETED}"' in log_filter and "cs-check-alerts-staging" in log_filter


def test_the_heartbeat_event_is_really_logged_by_the_alert_service():
    text = (PROJECT_ROOT / "app" / "services" / "alert_service.py").read_text(encoding="utf-8-sig")
    assert "log_check_completed" in text


# ---- 排程工作失敗 -----------------------------------------------------------------------


def test_any_failed_scheduled_job_triggers_an_alert_naming_all_four_jobs():
    condition = threshold(policies()["job-failed"])
    assert condition["thresholdValue"] == 0 and condition["comparison"] == "COMPARISON_GT"
    assert 'metric.label."result"="failed"' in condition["filter"]
    from app.cli import SCHEDULED_COMMANDS
    for command in SCHEDULED_COMMANDS:
        assert f"cs-{command}-staging" in condition["filter"]


# ---- 通知管道只有電子郵件、收件人沒有預設值 -----------------------------------------------


def test_without_recipients_the_alert_steps_are_refused():
    with pytest.raises(plan.PlanError) as error:
        alerts(emails=())
    assert "--notify-email" in str(error.value)


@pytest.mark.parametrize("bad", ["Name <a@example.com>", "no-at-sign", "a@example.com\nb@example.com", "a b@example.com", "a@example.com,b@example.com"])
def test_invalid_recipient_addresses_are_refused_without_echoing_them(bad):
    with pytest.raises(plan.PlanError) as error:
        alerts(emails=(bad,))
    assert bad.split("\n")[0] not in str(error.value)


def test_duplicate_recipients_are_refused():
    with pytest.raises(plan.PlanError):
        alerts(emails=(EMAIL, EMAIL))


def test_the_only_channel_type_is_email_and_the_address_appears_only_in_the_channel_step():
    steps = alerts(emails=(EMAIL, "second@zz-host.example"))
    channels = [step for step in steps if step.id.startswith("create-channel-")]
    assert len(channels) == 2 and all("--type=email" in step.argv for step in channels)
    for step in steps:
        if step not in channels:
            assert EMAIL not in " ".join(step.argv)
    everything = " ".join(" ".join(step.argv) for step in steps)
    assert "webhook" not in everything.lower() and "sentry" not in everything.lower()


def test_the_policies_are_attached_to_every_channel():
    steps = alerts(emails=(EMAIL, "second@zz-host.example"))
    policy_step = next(step for step in steps if step.id == "policy-job-failed")
    assert "--notification-channels={{channel-0}},{{channel-1}}" in policy_step.argv


def test_no_default_recipient_exists_anywhere_in_the_plan_module():
    source = (PROJECT_ROOT / "scripts" / "launch_plan.py").read_text(encoding="utf-8-sig")
    assert not re.search(r"[\w.]+@[\w-]+\.[\w.]+", source.replace("ops@example.com", "")), "計畫模組不得有預設的收件人地址"


# ---- 定義本身 ---------------------------------------------------------------------------


def test_every_policy_body_is_valid_json_once_the_run_time_ids_are_filled_in():
    for step in alerts():
        if not step.id.startswith("policy-"):
            continue
        body = next(part for part in step.argv if part.startswith("--policy=")).removeprefix("--policy=")
        parsed = json.loads(re.sub(r"\{\{[^}]+\}\}", "123", body))
        assert parsed["displayName"].startswith("staging：") and parsed["conditions"]


def test_staging_and_production_alerts_share_no_resource_name():
    def names(env):
        found = set()
        for step in alerts(env):
            found.update(re.findall(r"cs-[a-z0-9-]+", " ".join(step.argv)))
        return found
    assert names("staging").isdisjoint(names("production"))


def test_existing_policies_are_updated_in_place_not_duplicated():
    policy_step = next(step for step in alerts() if step.id == "policy-five-xx-ratio")
    assert policy_step.exists_nonempty and policy_step.update_argv[2:5] == ("monitoring", "policies", "update")
    assert policy_step.update_argv[5] == "{{policy-five-xx-ratio}}"


# ---- 文件與定義一致 ---------------------------------------------------------------------


def doc_rows() -> dict[str, str]:
    text = (PROJECT_ROOT / "docs" / "營運與部署-operations.md").read_text(encoding="utf-8-sig")
    section = text.split("**平台負責的告警**", 1)[1].split("`check-alerts` 的**非零結束代碼**", 1)[0]
    rows = {}
    for line in section.split("\n"):
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) == 4 and cells[0] not in ("告警", "---"):
            rows[cells[0]] = line
    return rows


def numbers(row: str) -> set[int]:
    return {int(number) for number in re.findall(r"\d+", row)}


def test_the_operations_doc_table_states_the_same_thresholds_and_windows_as_the_definitions():
    rows = doc_rows()
    assert set(rows) == {"5xx 比率", "服務存活", "服務就緒", "寄信失敗", "排程工作失敗", "告警檢查停擺"}
    five_xx = numbers(rows["5xx 比率"])
    assert f"超過 {int(plan.FIVE_XX_RATIO * 100)}%" in rows["5xx 比率"]
    assert plan.FIVE_XX_MIN_REQUESTS in five_xx and plan.FIVE_XX_WINDOW_SECONDS in five_xx
    for key in ("服務存活", "服務就緒"):
        assert plan.UPTIME_CONSECUTIVE_FAILURES in numbers(rows[key]) and plan.UPTIME_PERIOD_MINUTES * 60 * plan.UPTIME_CONSECUTIVE_FAILURES in numbers(rows[key])
        assert f"{plan.UPTIME_PERIOD_MINUTES} 分鐘" in rows["服務存活"]
    assert plan.MAIL_FAILED_THRESHOLD in numbers(rows["寄信失敗"]) and plan.MAIL_FAILED_WINDOW_SECONDS in numbers(rows["寄信失敗"])
    assert f"{plan.CHECK_ALERTS_STALL_SECONDS // 60} 分鐘" in rows["告警檢查停擺"] and plan.CHECK_ALERTS_STALL_SECONDS in numbers(rows["告警檢查停擺"])
    assert security_events.MAIL_SEND_FAILED in rows["寄信失敗"] and alert_events.EVENT_CHECK_COMPLETED in rows["告警檢查停擺"]


def test_the_doc_states_that_the_channel_is_email_only_and_recipients_have_no_default():
    text = (PROJECT_ROOT / "docs" / "營運與部署-operations.md").read_text(encoding="utf-8-sig")
    assert "只有電子郵件" in text and "--notify-email" in text and "沒有預設值" in text
