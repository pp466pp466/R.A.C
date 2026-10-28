"""email-token-flows 規格「信件內容與編碼」：真實的 SMTP 寄送、連線方式、逾時、標頭注入。"""
import email
import socket
import ssl
import threading
import time
from email import policy

import pytest
from aiosmtpd.controller import Controller

from app import create_app
from app.config import MailSettings, Secret, load_config
from app.services import mail_messages
from app.services.mail_sender import SMTP_TIMEOUT_SECONDS, MailHeaderError, SmtpMailSender
from tests.support.config_env import AUTH_ENV, FAKE_SMTP_PASSWORD

SUBJECT = "【智能客服】請驗證您的電子郵件信箱"
BODY = "王小明 您好：\n\n請點擊連結：http://localhost:5000/verify-email#token=abc_DEF-123\n\n這個連結將於 24 小時後失效。\n"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Sink:
    def __init__(self) -> None:
        self.envelopes = []

    async def handle_DATA(self, server, session, envelope):      # aiosmtpd 的處理常式介面
        self.envelopes.append(envelope)
        return "250 OK"


@pytest.fixture()
def sink():
    handler = Sink()
    port = free_port()
    controller = Controller(handler, hostname="127.0.0.1", port=port)
    controller.start()
    handler.port = port
    yield handler
    controller.stop()


def plain_settings(port: int, **overrides) -> MailSettings:
    values = dict(host="127.0.0.1", port=port, from_address="service@example.com", security="none")
    values.update(overrides)
    return MailSettings(**values)


# ---- 真實的 SMTP：內容與編碼 --------------------------------------------------

def test_the_message_arrives_as_utf8_traditional_chinese_with_the_right_headers(sink):
    SmtpMailSender(plain_settings(sink.port)).send(to="member@example.com", subject=SUBJECT, body=BODY)
    (envelope,) = sink.envelopes
    assert envelope.mail_from == "service@example.com" and envelope.rcpt_tos == ["member@example.com"]
    message = email.message_from_bytes(envelope.content, policy=policy.default)
    assert message["Subject"] == SUBJECT
    assert message["From"] == "service@example.com" and message["To"] == "member@example.com"
    assert message.get_content_type() == "text/plain" and message.get_content_charset() == "utf-8"
    assert message["Content-Transfer-Encoding"] == "base64"
    assert message.get_content().replace("\r\n", "\n") == BODY


def test_the_subject_is_rfc2047_encoded_on_the_wire_not_raw_utf8(sink):
    SmtpMailSender(plain_settings(sink.port)).send(to="member@example.com", subject=SUBJECT, body=BODY)
    raw_headers = sink.envelopes[0].content.split(b"\r\n\r\n", 1)[0]
    assert b"Subject: =?utf-8?" in raw_headers
    assert all(byte < 0x80 for byte in raw_headers)


def test_the_real_product_messages_round_trip_through_smtp(sink):
    link = mail_messages.verification_link("https://cs.example.com", "tok-en_1")
    content = mail_messages.verification_mail("王小明", link)
    SmtpMailSender(plain_settings(sink.port)).send(to="member@example.com", subject=content.subject, body=content.body)
    message = email.message_from_bytes(sink.envelopes[0].content, policy=policy.default)
    text = message.get_content()
    assert "王小明" in text and link in text and "24 小時" in text and "忽略這封信" in text


# ---- 連線方式（以替身驗證呼叫順序）--------------------------------------------

class FakeSmtp:
    log: list = []

    def __init__(self, host, port, **kwargs):
        self.log.append(("connect", type(self).__name__, host, port, kwargs))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.log.append(("close",))

    def ehlo(self):
        self.log.append(("ehlo",))

    def starttls(self, context=None):
        self.log.append(("starttls", context))

    def login(self, user, password):
        self.log.append(("login", user, password))

    def send_message(self, message):
        self.log.append(("send_message", message["To"]))


class FakeSmtpSsl(FakeSmtp):
    pass


@pytest.fixture()
def fake_smtp(monkeypatch):
    FakeSmtp.log = []
    monkeypatch.setattr("app.services.mail_sender.smtplib.SMTP", FakeSmtp)
    monkeypatch.setattr("app.services.mail_sender.smtplib.SMTP_SSL", FakeSmtpSsl)
    return FakeSmtp.log


def names(log):
    return [entry[0] for entry in log]


def test_starttls_upgrades_the_connection_before_logging_in(fake_smtp):
    settings = MailSettings(host="smtp.example.com", port=587, from_address="service@example.com", security="starttls",
                            username="mailer", password=Secret(FAKE_SMTP_PASSWORD))
    SmtpMailSender(settings).send(to="member@example.com", subject=SUBJECT, body=BODY)
    assert names(fake_smtp) == ["connect", "ehlo", "starttls", "ehlo", "login", "send_message", "close"]
    connect = fake_smtp[0]
    assert connect[1] == "FakeSmtp" and connect[2:4] == ("smtp.example.com", 587)
    assert connect[4] == {"timeout": SMTP_TIMEOUT_SECONDS} == {"timeout": 5.0}
    context = next(e for e in fake_smtp if e[0] == "starttls")[1]
    assert isinstance(context, ssl.SSLContext) and context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert next(e for e in fake_smtp if e[0] == "login")[1:] == ("mailer", FAKE_SMTP_PASSWORD)


def test_ssl_mode_connects_over_tls_from_the_start_and_does_not_starttls(fake_smtp):
    settings = MailSettings(host="smtp.example.com", port=465, from_address="service@example.com", security="ssl",
                            username="mailer", password=Secret(FAKE_SMTP_PASSWORD))
    SmtpMailSender(settings).send(to="member@example.com", subject=SUBJECT, body=BODY)
    assert names(fake_smtp) == ["connect", "ehlo", "login", "send_message", "close"]
    connect = fake_smtp[0]
    assert connect[1] == "FakeSmtpSsl" and connect[4]["timeout"] == 5.0
    assert connect[4]["context"].verify_mode == ssl.CERT_REQUIRED


def test_unencrypted_mode_without_credentials_neither_upgrades_nor_logs_in(fake_smtp):
    SmtpMailSender(plain_settings(1025)).send(to="member@example.com", subject=SUBJECT, body=BODY)
    assert names(fake_smtp) == ["connect", "ehlo", "send_message", "close"]


# ---- 失敗：拋出例外，不卡住，不洩漏密碼 ---------------------------------------

def test_a_refused_connection_raises_quickly():
    settings = plain_settings(free_port(), username="mailer", password=Secret(FAKE_SMTP_PASSWORD))
    started = time.monotonic()
    with pytest.raises(OSError) as exc:
        SmtpMailSender(settings).send(to="member@example.com", subject=SUBJECT, body=BODY)
    assert time.monotonic() - started < SMTP_TIMEOUT_SECONDS
    assert FAKE_SMTP_PASSWORD not in str(exc.value)


def test_a_server_that_never_answers_times_out_instead_of_hanging():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    accepted = []
    threading.Thread(target=lambda: accepted.append(listener.accept()), daemon=True).start()
    try:
        sender = SmtpMailSender(plain_settings(listener.getsockname()[1]), timeout=0.5)
        started = time.monotonic()
        with pytest.raises(OSError):                  # TimeoutError 是 OSError 的子類別
            sender.send(to="member@example.com", subject=SUBJECT, body=BODY)
        assert time.monotonic() - started < 3
    finally:
        for connection, _ in accepted:
            connection.close()
        listener.close()


# ---- 標頭注入 -----------------------------------------------------------------

@pytest.mark.parametrize(
    ("to", "subject"),
    [
        ("member@example.com\r\nBcc: victim@example.com", SUBJECT),
        ("member@example.com\nBcc: victim@example.com", SUBJECT),
        ("member@example.com", "標題\r\nBcc: victim@example.com"),
        ("member@example.com", "標題\nX-Injected: 1"),
        ("", SUBJECT),
        ("member@example.com", ""),
    ],
)
def test_header_injection_is_refused_before_any_connection_is_made(sink, to, subject):
    with pytest.raises(MailHeaderError):
        SmtpMailSender(plain_settings(sink.port)).send(to=to, subject=subject, body=BODY)
    assert sink.envelopes == []


# ---- 由設定建立 ---------------------------------------------------------------

def test_create_app_builds_the_real_sender_from_the_mail_settings():
    config = load_config(environ={"APP_ENV": "testing", "EMAIL_VERIFICATION_REQUIRED": "true", **AUTH_ENV, "BASE_URL": "http://localhost:5000"})
    sender = create_app(config).extensions["mail_sender"]
    assert isinstance(sender, SmtpMailSender)
    assert sender._settings == config.mail


def test_an_injected_sender_wins_and_without_mail_settings_there_is_none():
    class Fake:
        pass

    fake = Fake()
    config = load_config(environ={"APP_ENV": "testing", "EMAIL_VERIFICATION_REQUIRED": "true", **AUTH_ENV, "BASE_URL": "http://localhost:5000"})
    assert create_app(config, mail_sender=fake).extensions["mail_sender"] is fake
    assert create_app(load_config(environ={"APP_ENV": "testing"})).extensions["mail_sender"] is None
