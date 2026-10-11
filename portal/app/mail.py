"""Письма портала: учётные данные новому сотруднику (решение пользователя 21.09).

Отправка через обычный почтовый ящик по SMTP — Яндекс, Mail.ru или Gmail, все три с ПК
доступны. Настройки лежат в `.env` портала и задаются во вкладке «Почта» администрирования;
пароль ящика туда только записывается и наружу не отдаётся. Для Яндекса и Gmail нужен
не пароль от ящика, а **пароль приложения** — его выпускают в настройках безопасности почты.
"""
from __future__ import annotations

import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from . import config

TIMEOUT = 20
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Известные ящики: администратору достаточно ввести адрес, сервер подставится сам.
KNOWN_HOSTS = {"yandex.ru": "smtp.yandex.ru", "ya.ru": "smtp.yandex.ru", "mail.ru": "smtp.mail.ru",
               "bk.ru": "smtp.mail.ru", "list.ru": "smtp.mail.ru", "inbox.ru": "smtp.mail.ru",
               "gmail.com": "smtp.gmail.com"}


def portal_url() -> str:
    return config.read_env("PORTAL_PUBLIC_URL") or "https://workspace.example.ru"


def settings() -> dict:
    return {"host": config.read_env("SMTP_HOST"), "port": config.read_env("SMTP_PORT") or "465",
            "user": config.read_env("SMTP_USER"),
            "from_name": config.read_env("SMTP_FROM_NAME") or "Рабочее пространство",
            "password_set": bool(config.read_env("SMTP_PASSWORD")), "portal_url": portal_url()}


def configured() -> bool:
    s = settings()
    return bool(s["host"] and s["user"] and s["password_set"])


def guess_host(user: str) -> str:
    return KNOWN_HOSTS.get(user.rsplit("@", 1)[-1].lower(), "") if "@" in user else ""


def _send(to: str, subject: str, text: str) -> None:
    s = settings()
    msg = EmailMessage()
    msg["From"] = formataddr((s["from_name"], s["user"]))
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    port = int(s["port"] or 465)
    context = ssl.create_default_context()
    if port == 465:
        smtp = smtplib.SMTP_SSL(s["host"], port, timeout=TIMEOUT, context=context)
    else:
        smtp = smtplib.SMTP(s["host"], port, timeout=TIMEOUT)
    with smtp:
        if port != 465 and config.read_env("SMTP_STARTTLS", "1") != "0":
            smtp.starttls(context=context)
        if config.read_env("SMTP_PASSWORD"):
            smtp.login(s["user"], config.read_env("SMTP_PASSWORD"))
        smtp.send_message(msg)


def human_error(e: Exception) -> str:
    if isinstance(e, smtplib.SMTPAuthenticationError):
        # ответ сервера показываем: по нему видно, неверен пароль или в ящике выключен доступ программам
        reply = (e.smtp_error or b"").decode("utf-8", "replace").strip()
        hint = "почта не приняла ящик или пароль приложения"
        if "access rights" in reply or "not have access" in reply:
            hint = ("в ящике выключен доступ почтовым программам: Яндекс Почта → Настройки → «Почтовые программы» → "
                    "включите «С сервера imap.yandex.ru по протоколу IMAP» и «Пароли приложений»")
        return hint + (f" (ответ сервера: {reply[:200]})" if reply else "")
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return "почтовый сервер не принял адрес получателя"
    if isinstance(e, (TimeoutError, OSError)):
        return "нет связи с почтовым сервером"
    return f"письмо не ушло ({type(e).__name__})"


def _first_name(full_name: str) -> str:
    words = full_name.split()
    return words[1] if len(words) > 1 else ""


def send_credentials(to: str, full_name: str, login: str, password: str, *, reset: bool = False) -> None:
    name = _first_name(full_name)
    lead = ("Пароль к вашей учётной записи в рабочем пространстве сброшен." if reset
            else "Для вас заведена учётная запись в рабочем пространстве.")
    text = (f"Здравствуйте{', ' + name if name else ''}!\n\n{lead}\n\n"
            f"Адрес:             {portal_url()}\n"
            f"Логин:             {login}\n"
            f"Временный пароль:  {password}\n\n"
            "При первом входе портал попросит сменить пароль на свой — не короче 10 знаков,\n"
            "удобнее всего фраза из нескольких слов. Временный пароль после этого перестанет действовать.\n\n"
            "Если письмо пришло по ошибке, просто удалите его.\n")
    _send(to, "Доступ в рабочее пространство", text)


def send_test(to: str) -> None:
    _send(to, "Проверка почты рабочего пространства",
          "Это проверочное письмо. Раз оно дошло, портал умеет отправлять учётные данные сотрудникам.\n")
