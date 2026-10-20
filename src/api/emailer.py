import re
import smtplib
import ssl
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from io import BytesIO
from PIL import Image
from collections import Counter, OrderedDict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from html import escape
from email.utils import formataddr
from urllib.parse import quote

import markdown
from pygments.formatters import HtmlFormatter

from src.runtime import config

_MD_EXTENSIONS= ["tables", "fenced_code", "nl2br", "sane_lists", "codehilite"]

_MD_EXTENSION_CONFIGS = {
    "codehilite": {
        "guess_lang": False,
        "linenums": False,
        "css_class": "highlight",
    }
}

_PYGMENTS_CSS = HtmlFormatter(style="friendly").get_style_defs(".highlight")

_EMAIL_CSS = """\
body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, sans-serif;
    font-size: 15px;
    line-height: 1.7;
    color: #1a1a1a;
    max-width: 800px;
    margin: 0 auto;
    padding: 20px;
}
h2 {
    color: #2c3e50;
    border-bottom: 2px solid #3498db;
    padding-bottom: 8px;
    margin-top: 32px;
}
h3 {
    color: #34495e;
    margin-top: 24px;
}
h3 small {
    color: #7f8c8d;
    font-weight: normal;
}
h4 { color: #555; margin-top: 18px; }
hr {
    border: none;
    border-top: 1px solid #e0e0e0;
    margin: 28px 0;
}
strong { color: #c0392b; }
table {
    border-collapse: collapse;
    width: 100%;
    margin: 12px 0;
}
th, td {
    border: 1px solid #ddd;
    padding: 8px 12px;
    text-align: left;
}
th {
    background: #f5f6fa;
    font-weight: 600;
}
tr:nth-child(even) { background: #fafafa; }
pre {
    background: #f8f8f8;
    border: 1px solid #e0e0e0;
    border-radius: 4px;
    padding: 12px 16px;
    overflow-x: auto;
    font-size: 13px;
    line-height: 1.5;
}
code {
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
    font-size: 13px;
}
p code {
    background: #f0f0f0;
    padding: 2px 5px;
    border-radius: 3px;
}
blockquote {
    border-left: 4px solid #3498db;
    margin: 12px 0;
    padding: 8px 16px;
    background: #f8f9fa;
    color: #555;
}
ul, ol { padding-left: 24px; }
li { margin-bottom: 4px; }
"""

_PDF_CSS = """\
@page { size: A4; margin: 16mm 15mm; }
body {
    max-width: none !important;
    margin: 0 !important;
    padding: 0 !important;
    font-family: "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
    font-size: 11pt;
    line-height: 1.65;
}
h1 { font-size: 20pt; margin: 0 0 8px; color: #2c3e50; }
h2 { font-size: 15pt; margin: 0 0 18px; color: #34495e; }
h3 { page-break-after: avoid; }
img { max-width: 100% !important; height: auto !important; }
pre { white-space: pre-wrap !important; word-wrap: break-word; }
table { page-break-inside: avoid; }
"""

_MIN_INLINE_HEIGHT = 13  # minimum logical height for inline formulas (px)

_IMAGE_CACHE: dict[str, tuple] = {}


def _smtp_candidates() -> list[tuple[str, int, str]]:
    """Return the configured endpoint plus a sensible TLS fallback."""
    primary = (config.SMTP_HOST, config.SMTP_PORT, config.SMTP_SECURITY)
    candidates = [primary]
    host, _port, security = primary
    if security == "ssl":
        candidates.append((host, 587, "starttls"))
    elif security == "starttls":
        candidates.append((host, 465, "ssl"))
    if host == "smtp.office365.com":
        candidates.append(("smtp-mail.outlook.com", 587, "starttls"))
    elif host == "smtp-mail.outlook.com":
        candidates.append(("smtp.office365.com", 587, "starttls"))
    if host == "mail.fudan.edu.cn" and ("mail.fudan.edu.cn", 25, "starttls") not in candidates:
        candidates.append(("mail.fudan.edu.cn", 25, "starttls"))

    # Preserve order while dropping duplicates.
    seen = set()
    out = []
    for item in candidates:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _open_smtp(host: str, port: int, security: str):
    """Open an SMTP connection using implicit TLS, STARTTLS, or plaintext."""
    context = ssl.create_default_context()
    if security == "ssl":
        return smtplib.SMTP_SSL(
            host, port, timeout=config.SMTP_TIMEOUT, context=context,
        )
    server = smtplib.SMTP(host, port, timeout=config.SMTP_TIMEOUT)
    server.ehlo()
    if security == "starttls":
        server.starttls(context=context)
        server.ehlo()
    return server


def _smtp_auth_hint() -> str:
    """Actionable, non-secret guidance for the common 535 cases."""
    domain = (
        config.SMTP_EMAIL.rsplit("@", 1)[-1].lower()
        if "@" in config.SMTP_EMAIL else ""
    )
    if domain in ("m.fudan.edu.cn", "fudan.edu.cn"):
        return (
            "SMTP 认证失败（535）。当前 SMTP_EMAIL 是复旦邮箱，"
            "SMTP_PASSWORD 需要填写复旦邮箱的客户端授权码/邮箱密码"
            "（不是 UIS 统一身份认证密码），并在邮箱设置中确认已开启 "
            "SMTP/客户端服务。"
        )
    if domain == "qq.com":
        return (
            "SMTP 认证失败（535）。QQ 邮箱的 SMTP_PASSWORD 必须填写"
            "16 位 SMTP 授权码，不是 QQ 登录密码。"
        )
    if (domain in (
        "outlook.com", "hotmail.com", "live.com", "msn.com",
        "outlook.jp", "office365.com", "onmicrosoft.com",
    ) or domain.endswith(".onmicrosoft.com")):
        return (
            "SMTP 认证失败（535）。Outlook 需要 Microsoft 账户的"
            "应用专用密码；如果服务器提示 basic authentication "
            "disabled，则该账户只支持 OAuth2，当前密码式 SMTP 登录"
            "无法使用。"
        )
    return (
        "SMTP 认证失败（535）。请检查 SMTP_EMAIL / SMTP_PASSWORD，"
        "并确认邮箱服务商已开启 SMTP 客户端登录。"
    )


def send_message(msg, *, raise_on_failure: bool = False) -> bool:
    """Send a MIME message using the configured SMTP endpoint.

    Tries the configured endpoint first, then a TLS fallback (465↔587).
    Authentication failures fail fast instead of retrying every endpoint,
    which avoids locking an account after a wrong password.

    Returns True on success.  On failure returns False, or raises
    RuntimeError when ``raise_on_failure`` is set (used by export scripts
    so a failed export is visible in Actions).
    """
    errors: list[str] = []
    auth_failed_hosts: set[str] = set()
    for host, port, security in _smtp_candidates():
        if host in auth_failed_hosts:
            continue
        label = f"{host}:{port} ({security})"
        for attempt in range(2):
            try:
                with _open_smtp(host, port, security) as server:
                    server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
                    server.sendmail(
                        config.SMTP_EMAIL,
                        config.RECEIVER_EMAIL,
                        msg.as_string(),
                    )
                print(f"[Emailer] Sent via {label}")
                return True
            except smtplib.SMTPAuthenticationError as exc:
                hint = _smtp_auth_hint()
                print(f"[Emailer] {label}: {hint} Server said: {exc}")
                errors.append(f"{label}: {hint}")
                auth_failed_hosts.add(host)
                # A wrong credential will not improve on another port.  A
                # different Outlook endpoint is still worth one try.
                if raise_on_failure:
                    raise RuntimeError(hint) from exc
                continue
            except Exception as exc:
                print(
                    f"[Emailer] {label} attempt {attempt + 1}/2 failed: "
                    f"{type(exc).__name__}: {exc}"
                )
                errors.append(f"{label}: {type(exc).__name__}: {exc}")
                if attempt == 0:
                    time.sleep(2 ** attempt)

    detail = "\n".join(errors)
    if raise_on_failure:
        raise RuntimeError(f"All SMTP endpoints failed:\n{detail}")
    print("[Emailer] All SMTP endpoints failed.")
    return False


def _fetch_latex_image(url: str, dpi: int = 300) -> tuple:
    """Fetch rendered LaTeX image, return (width, height, png_bytes).

    Width/height are logical display pixels (DPI-adjusted).
    Returns (None, None, None) on failure.
    """
    if url in _IMAGE_CACHE:
        return _IMAGE_CACHE[url]

    try:
        scale_factor = dpi / 96.0
        response = requests.get(url, timeout=10)
        response.raise_for_status()

        img = Image.open(BytesIO(response.content))
        logical_width = max(1, int(img.width / scale_factor))
        logical_height = max(1, int(img.height / scale_factor))

        result = (logical_width, logical_height, response.content)
        _IMAGE_CACHE[url] = result
        return result
    except Exception as e:
        print(f"[LaTeX Render] Image fetch failed: {e}")
        return None, None, None


def _prefetch_latex_images(urls: list[str], dpi: int = 300) -> None:
    """Pre-fetch multiple LaTeX images concurrently.

    Results are stored in ``_IMAGE_CACHE`` so that subsequent calls to
    ``_fetch_latex_image`` become instant cache hits.
    """
    uncached = [u for u in urls if u not in _IMAGE_CACHE]
    if not uncached:
        return
    with ThreadPoolExecutor(max_workers=min(len(uncached), 8)) as pool:
        futures = {pool.submit(_fetch_latex_image, u, dpi): u for u in uncached}
        for future in as_completed(futures):
            future.result()  # trigger any exception logging inside _fetch_latex_image


def _md_to_html(md_text: str, cid_images: dict | None = None) -> str:
    """Convert Markdown to styled HTML, rendering LaTeX math as images.

    Processing order: extract LaTeX → markdown convert → restore as <img>.
    This prevents the markdown engine from corrupting backslash escapes.

    Args:
        md_text: Markdown source text.
        cid_images: When provided (dict), download images and embed via CID
                    references instead of external URLs.  The dict is populated
                    with {cid_name: png_bytes} entries for the caller to attach
                    to the MIME message.
    """
    latex_map: dict[str, str] = {}
    counter = 0

    def _stash(match):
        nonlocal counter
        key = f"\x00LATEX{counter}\x00"
        counter += 1
        latex_map[key] = match.group(0)
        return key

    def _stash_block(match):
        """Stash \\[...\\] as $$...$$ for uniform downstream handling."""
        nonlocal counter
        key = f"\x00LATEX{counter}\x00"
        counter += 1
        latex_map[key] = "$$" + match.group(1) + "$$"
        return key

    def _stash_inline(match):
        """Stash \\(...\\) as $...$ for uniform downstream handling."""
        nonlocal counter
        key = f"\x00LATEX{counter}\x00"
        counter += 1
        latex_map[key] = "$" + match.group(1) + "$"
        return key

    # Extract LaTeX in order: block first, then inline
    # 1) $$...$$ block formulas
    text = re.sub(r"\$\$(.+?)\$\$", _stash, md_text, flags=re.DOTALL)
    # 2) \[...\] block formulas (normalize to $$...$$)
    text = re.sub(r"\\\[(.+?)\\\]", _stash_block, text, flags=re.DOTALL)
    # 3) $...$ inline formulas (not $$)
    text = re.sub(r"(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)", _stash, text)
    # 4) \(...\) inline formulas (normalize to $...$)
    text = re.sub(r"\\\((.+?)\\\)", _stash_inline, text)

    html = markdown.markdown(
        text,
        extensions=_MD_EXTENSIONS,
        extension_configs=_MD_EXTENSION_CONFIGS,
    )

    # Build URL list and pre-fetch all LaTeX images concurrently
    # Each entry: (url, latex_content, is_block)
    latex_info: dict[str, tuple[str, str, bool]] = {}
    for key, original in latex_map.items():
        is_block = original.startswith("$$")
        latex_content = original[2:-2] if is_block else original[1:-1]
        prefix = r"\dpi{300}\bg{white}" if is_block else r"\dpi{300}\bg{white}\inline"
        url = f"https://latex.codecogs.com/png.latex?{prefix}%20{quote(latex_content)}"
        latex_info[key] = (url, latex_content, is_block)

    _prefetch_latex_images([info[0] for info in latex_info.values()])

    for key, (url, latex_content, is_block) in latex_info.items():
        w, h, img_data = _fetch_latex_image(url)

        if is_block:
            if w and h:
                src = _resolve_src(url, img_data, cid_images)
                img_tag = (
                    f'<div style="text-align:center;margin:16px 0">'
                    f'<img src="{src}" alt="{escape(latex_content)}" '
                    f'width="{w}" height="{h}" '
                    f'style="width:{w}px;height:{h}px;max-width:none;'
                    f'vertical-align:middle;border:none;display:inline-block;">'
                    f'</div>'
                )
            else:
                img_tag = (
                    f'<div style="text-align:center;margin:16px 0">'
                    f'<code>{escape(latex_content)}</code></div>'
                )
        else:
            if w and h:
                # Enforce minimum height so formulas aren't smaller than text
                if h < _MIN_INLINE_HEIGHT:
                    scale = _MIN_INLINE_HEIGHT / h
                    w = max(1, int(w * scale))
                    h = _MIN_INLINE_HEIGHT

                src = _resolve_src(url, img_data, cid_images)
                img_tag = (
                    f'<img src="{src}" alt="{escape(latex_content)}" '
                    f'width="{w}" height="{h}" '
                    f'style="width:{w}px;height:{h}px;max-width:none;'
                    f'vertical-align:-3px;border:none;margin:0 2px;">'
                )
            else:
                img_tag = f'<code>{escape(latex_content)}</code>'

        html = html.replace(key, img_tag)

    return html


def _resolve_src(url: str, img_data: bytes | None,
                 cid_images: dict | None) -> str:
    """Return a CID reference if embedding, otherwise the original URL."""
    if cid_images is not None and img_data:
        cid = f"latex-{uuid.uuid4().hex[:12]}"
        cid_images[cid] = img_data
        return f"cid:{cid}"
    return url


def _safe_filename(text: str, limit: int = 80) -> str:
    cleaned = "".join(
        c if c.isalnum() or c in " _-" else "_" for c in str(text)
    )
    cleaned = cleaned.strip().replace(" ", "_")
    return (cleaned[:limit] or "lecture")


def _build_lecture_pdf_html(item: dict) -> str:
    course = escape(item.get("course_title", ""))
    title = escape(item.get("sub_title", ""))
    date = escape(item.get("date", ""))
    teacher = escape(item.get("teacher", ""))
    summary = _md_to_html(item.get("summary", ""), cid_images=None)
    teacher_html = f"<p>任课教师：{teacher}</p>" if teacher else ""
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<style>{_EMAIL_CSS}\n{_PYGMENTS_CSS}\n{_PDF_CSS}</style>"
        "</head><body>"
        f"<h1>{course}</h1><h2>{title} <small>({date})</small></h2>"
        f"{teacher_html}<hr>{summary}</body></html>"
    )


def _render_lecture_pdf(
    item: dict, *, neutral_header: bool = False,
) -> tuple[bytes, str]:
    try:
        import weasyprint  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "weasyprint is required for PDF summary emails"
        ) from exc
    if neutral_header:
        header_item = {
            **item,
            "course_title": "课程笔记",
            "sub_title": item.get("date", ""),
        }
        html = _build_lecture_pdf_html(header_item)
    else:
        html = _build_lecture_pdf_html(item)
    pdf_bytes = weasyprint.HTML(string=html).write_pdf()
    if neutral_header:
        filename = f"notes_{_safe_filename(item.get('date', 'lecture'))}.pdf"
    else:
        filename = (
            f"{_safe_filename(item.get('course_title', 'course'))}_"
            f"{_safe_filename(item.get('sub_title', 'lecture'))}.pdf"
        )
    return pdf_bytes, filename


class Emailer:
    """Send course summary emails via QQ SMTP SSL."""

    def __init__(self):
        self.host = config.SMTP_HOST
        self.port = config.SMTP_PORT
        self.sender = config.SMTP_EMAIL
        self.password = config.SMTP_PASSWORD
        self.receiver = config.RECEIVER_EMAIL

    def _build_pdf_message(
        self, items: list[dict], subject: str,
        attachments: list[tuple[bytes, str]],
        *, neutral: bool = False,
    ):
        plain_lines = [
            "课程笔记 PDF 已随邮件发送。"
            if neutral else f"课程：{items[0]['course_title']}",
            f"本邮件包含 {len(attachments)} 份课程笔记 PDF：",
        ]
        for item in items:
            plain_lines.append(
                f"- {item.get('date', '')}"
                if neutral else
                f"- {item['sub_title']} ({item.get('date', '')})"
            )
        plain = "\n".join(plain_lines)

        if neutral:
            list_items = "\n".join(
                f"<li>课程笔记 <small>({escape(item.get('date', ''))})</small></li>"
                for item in items
            )
        else:
            list_items = "\n".join(
                f"<li>{escape(item['sub_title'])} "
                f"<small>({escape(item.get('date', ''))})</small></li>"
                for item in items
            )
        html = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<style>{_EMAIL_CSS}</style></head><body>"
            f"<h2>{'课程笔记' if neutral else escape(items[0]['course_title'])}</h2>"
            f"<p>本邮件包含 {len(attachments)} 份课程笔记 PDF：</p>"
            f"<ul>{list_items}</ul>"
            "<p style='color:#777;font-size:12px'>"
            "每节课对应一个独立 PDF 附件。</p></body></html>"
        )

        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = formataddr(("iCourse Subscriber", self.sender))
        msg["To"] = self.receiver
        msg_alt = MIMEMultipart("alternative")
        msg_alt.attach(MIMEText(plain, "plain", "utf-8"))
        msg_alt.attach(MIMEText(html, "html", "utf-8"))
        msg.attach(msg_alt)

        for pdf_bytes, filename in attachments:
            part = MIMEApplication(pdf_bytes, _subtype="pdf")
            part.add_header(
                "Content-Disposition", "attachment",
                filename=("utf-8", "", filename),
            )
            msg.attach(part)
        return msg

    def send(
        self, items: list[dict],
        on_batch_sent=None,
        subject_prefix: str = "",
        neutral_metadata: bool = False,
    ) -> bool:
        """Render one PDF per lecture and send them in small batches.

        Outlook rejects a single message with more than 1000 MIME parts.
        Each email carries only a few PDF attachments, so the HTML body no
        longer embeds thousands of LaTeX images.
        """
        if not items:
            return True

        courses: OrderedDict[str, list[dict]] = OrderedDict()
        for item in items:
            courses.setdefault(item["course_title"], []).append(item)

        batches: list[tuple[str, list[dict]]] = []
        for course_title, lectures in courses.items():
            size = max(1, config.EMAIL_PDF_MAX_ATTACHMENTS)
            for start in range(0, len(lectures), size):
                batches.append(
                    (course_title, lectures[start:start + size])
                )

        total_by_course = Counter(course for course, _ in batches)
        seen = Counter()
        all_ok = True
        for course_title, batch in batches:
            seen[course_title] += 1
            index = seen[course_title]
            total = total_by_course[course_title]
            attachments: list[tuple[bytes, str]] = []
            sent_items: list[dict] = []
            for pdf_index, lec in enumerate(batch, start=1):
                try:
                    print(
                        f"[Emailer] Rendering PDF: {course_title} / "
                        f"{lec.get('sub_title', '')}",
                        flush=True,
                    )
                    pdf_bytes, filename = _render_lecture_pdf(
                        lec, neutral_header=neutral_metadata,
                    )
                    if neutral_metadata:
                        filename = (
                            f"notes_{_safe_filename(lec.get('date', 'lecture'))}"
                            f"_{pdf_index:02d}.pdf"
                        )
                    attachments.append((pdf_bytes, filename))
                    sent_items.append(lec)
                except Exception as exc:
                    all_ok = False
                    print(
                        f"[Emailer] PDF failed for "
                        f"{lec.get('sub_title', '')}: "
                        f"{type(exc).__name__}: {exc}",
                        flush=True,
                    )

            if not attachments:
                continue
            if neutral_metadata:
                dates = sorted({
                    item.get("date", "") for item in sent_items
                    if item.get("date")
                })
                date_label = (
                    dates[0] if len(dates) == 1
                    else f"{dates[0]} 至 {dates[-1]}"
                ) if dates else "课程笔记"
                subject = (
                    f"[课程笔记] {date_label} "
                    f"({len(attachments)} 份 PDF)"
                    + (f" [{index}/{total}]" if total > 1 else "")
                )
            else:
                subject = (
                    f"[FiCS] {course_title} ({len(attachments)} 节课 PDF)"
                    + (f" [{index}/{total}]" if total > 1 else "")
                )
            if any(item.get("is_update") for item in sent_items):
                subject += "（含 PPT 识别·更新）"
            subject = (subject_prefix or "") + subject

            msg = self._build_pdf_message(
                sent_items, subject, attachments,
                neutral=neutral_metadata,
            )
            if not send_message(msg):
                print(f"[Emailer] Batch failed: {subject}")
                return False
            print(
                f"[Emailer] Sent: {subject} "
                f"({len(attachments)} PDF, batch {index}/{total})"
            )
            if on_batch_sent:
                try:
                    on_batch_sent(sent_items)
                except Exception as exc:
                    print(f"[Emailer] on_batch_sent failed: {exc}")
                    all_ok = False
        return all_ok
