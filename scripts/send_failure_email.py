#!/usr/bin/env python3
"""Send a sanitized workflow-failure notification using only stdlib."""

from __future__ import annotations

import argparse
import os
import re
import smtplib
import ssl
from email.mime.text import MIMEText
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _smtp_endpoint(sender: str) -> tuple[str, int, str]:
    domain = sender.rsplit("@", 1)[-1].lower() if "@" in sender else ""
    if domain == "onmicrosoft.com" or domain.endswith(".onmicrosoft.com"):
        return "smtp.office365.com", 587, "starttls"
    endpoints = {
        "qq.com": ("smtp.qq.com", 465, "ssl"),
        "foxmail.com": ("smtp.qq.com", 465, "ssl"),
        "163.com": ("smtp.163.com", 465, "ssl"),
        "126.com": ("smtp.126.com", 465, "ssl"),
        "gmail.com": ("smtp.gmail.com", 465, "ssl"),
        "outlook.com": ("smtp.office365.com", 587, "starttls"),
        "hotmail.com": ("smtp.office365.com", 587, "starttls"),
        "live.com": ("smtp.office365.com", 587, "starttls"),
        "msn.com": ("smtp.office365.com", 587, "starttls"),
        "office365.com": ("smtp.office365.com", 587, "starttls"),
        "m.fudan.edu.cn": ("smtp.exmail.qq.com", 465, "ssl"),
        "fudan.edu.cn": ("mail.fudan.edu.cn", 465, "ssl"),
    }
    return endpoints.get(
        domain, (f"smtp.{domain}" if domain else "smtp.qq.com", 465, "ssl"),
    )


def classify_failure(stage: str, log_tail: str) -> str:
    text = f"{stage}\n{log_tail}".lower()
    if "pip._internal" in text or "cannot import name 'cli'" in text:
        return "Python pip 工具链损坏"
    if "no module named" in text or "modulenotfounderror" in text:
        return "Python 依赖安装不完整"
    if "icourse_session_cookie" in text or "session cookie" in text:
        return "iCourse 会话 Cookie 无效或已过期"
    if "totp" in text or "userandotp" in text:
        return "UIS TOTP 动态口令验证失败"
    if "secondary authentication" in text:
        return "iCourse 需要二次认证"
    if any(token in text for token in (
        "webvpn", "logintoken", "cas response", "icourse login",
    )):
        return "iCourse / WebVPN 登录异常"
    if "media" in text and any(token in text for token in ("github api", "422", "403")):
        return "GitHub media 上传或清理失败"
    if any(token in text for token in ("sqlite", "database", "decrypt")):
        return "数据库读取、合并或解密失败"
    if any(token in text for token in ("smtp", "emailer", "sendmail")):
        return "课堂笔记或错误邮件发送失败"
    if "timeout" in text or "timed out" in text:
        return "网络或服务响应超时"
    return "未分类的工作流失败"


_SECRET_PATTERNS = (
    re.compile(r"github_pat_[A-Za-z0-9_]+"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]+"),
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"AIza[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?i)(authorization:\s*(?:bearer|token)\s+)\S+"),
    re.compile(r"(?i)(cookie:\s*)\S+"),
)


def sanitize(text: str) -> str:
    for name in (
        "SMTP_PASSWORD", "STUID", "UISPSW", "DEEPSEEK_API_KEY",
        "DASHSCOPE_API_KEY", "GEMINI_API_KEY", "GUEST_PAT",
        "ICOURSE_SESSION_COOKIE", "UIS_TOTP_SECRET",
    ):
        value = os.environ.get(name, "")
        if len(value) >= 4:
            text = text.replace(value, "***")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(r"\1***" if pattern.groups else "***", text)
    return text


def _tail(path: str, max_lines: int = 120, max_chars: int = 20000) -> str:
    if not path:
        return ""
    try:
        lines = Path(path).read_text(
            encoding="utf-8", errors="replace",
        ).splitlines()
    except OSError:
        return ""
    text = "\n".join(lines[-max_lines:])
    return text[-max_chars:]


def build_report() -> tuple[str, str]:
    workflow = _env("WORKFLOW_NAME", "iCourse workflow")
    job = _env("JOB_NAME", "unknown")
    stage = _env("FAILED_STAGE", "unknown")
    run_url = _env("RUN_URL")
    commit = _env("COMMIT_SHA", "unknown")[:12]
    event = _env("EVENT_NAME", "unknown")
    alert_type = _env("ALERT_TYPE", "failure").lower()
    raw_log = _tail(_env("FAILURE_LOG"))
    category = classify_failure(stage, raw_log)
    excerpt = sanitize(raw_log) or "失败步骤没有可读取的本地日志，请查看运行链接。"
    prefix = "DEFERRED" if alert_type == "deferred" else "FAILED"
    subject = f"[FiCS][{prefix}] {workflow} - {category}"
    body = "\n".join((
        f"Run status: {alert_type}",
        f"Workflow: {workflow}",
        f"Job: {job}",
        f"Event: {event}",
        f"Failed stage: {stage}",
        f"Failure category: {category}",
        f"Commit: {commit}",
        f"Run: {run_url or 'unavailable'}",
        "",
        "Error excerpt:",
        excerpt,
        "",
        "This is an automated failure notification from GitHub Actions.",
    ))
    return subject, body


def send_mail(subject: str, body: str) -> None:
    sender = _env("SMTP_EMAIL")
    password = _env("SMTP_PASSWORD")
    receiver = _env("RECEIVER_EMAIL")
    if not sender or not password or not receiver:
        raise RuntimeError("SMTP_EMAIL, SMTP_PASSWORD and RECEIVER_EMAIL are required")

    default_host, default_port, default_security = _smtp_endpoint(sender)
    host = _env("SMTP_HOST", default_host)
    port = int(_env("SMTP_PORT", str(default_port)))
    security = _env("SMTP_SECURITY", default_security).lower()
    if security == "tls":
        security = "starttls"
    username = _env("SMTP_USERNAME", sender)
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = receiver

    context = ssl.create_default_context()
    if security == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        if security == "starttls":
            server.starttls(context=context)
    try:
        server.ehlo()
        server.login(username, password)
        server.sendmail(sender, [receiver], msg.as_string())
    finally:
        try:
            server.quit()
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    subject, body = build_report()
    if args.dry_run:
        print(subject)
        print(body)
        return
    send_mail(subject, body)
    print(f"[Failure Email] Sent: {subject}")


if __name__ == "__main__":
    main()
