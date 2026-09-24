import imaplib
import email
from email.header import decode_header
import re
import time
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def get_imap_host_for_email(email_addr: str, custom_host: str = "") -> str:
    """تحديد سيرفر IMAP المناسب تلقائياً حسب نوع البريد الإلكتروني"""
    if custom_host and custom_host.strip():
        return custom_host.strip()
    
    email_clean = (email_addr or "").strip().lower()
    if "@ses.yu.edu.jo" in email_clean or "@yu.edu.jo" in email_clean:
        return "outlook.office365.com"
    elif "@outlook." in email_clean or "@hotmail." in email_clean or "@live." in email_clean:
        return "outlook.office365.com"
    elif "@gmail." in email_clean:
        return "imap.gmail.com"
    elif "@yahoo." in email_clean:
        return "imap.mail.yahoo.com"
    elif "@icloud." in email_clean:
        return "imap.mail.me.com"
    else:
        return "outlook.office365.com"


def decode_str(header_val) -> str:
    """فك تشفير نصوص ترويسات البريد الإلكتروني (Subject, From)"""
    if not header_val:
        return ""
    try:
        decoded_list = decode_header(header_val)
        parts = []
        for content, encoding in decoded_list:
            if isinstance(content, bytes):
                parts.append(content.decode(encoding or "utf-8", errors="ignore"))
            else:
                parts.append(str(content))
        return " ".join(parts)
    except Exception:
        return str(header_val)


def extract_otp_from_text(text: str) -> Optional[str]:
    """
    استخراج كود التحقق (OTP) المكون من 4 إلى 8 أرقام من نص الرسالة
    أمثلة:
    - 'رمز التحقق الخاص بك هو: 447990'
    - 'رمز التحقق: 123456'
    - 'Your verification code is: 447990'
    """
    if not text:
        return None

    patterns = [
        r"رمز\s*التحقق\s*الخاص\s*بك\s*هو\s*[:：]?\s*(\d{4,8})",
        r"رمز\s*التحقق\s*[:：]?\s*(\d{4,8})",
        r"كود\s*التحقق\s*[:：]?\s*(\d{4,8})",
        r"verification\s*code\s*is\s*[:：]?\s*(\d{4,8})",
        r"OTP\s*[:：]?\s*(\d{4,8})",
        r"\b(\d{6})\b"  # 6 أرقام متتالية كبديل قياسي
    ]

    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def fetch_latest_yu_otp(
    email_user: str,
    email_password: str,
    imap_host: str = "",
    imap_port: int = 993,
    max_wait_seconds: int = 45,
    poll_interval: int = 3
) -> Tuple[Optional[str], Optional[str]]:
    """
    الاتصال بالبريد الإلكتروني عبر IMAP وانتظار وصول رسالة YU-OTP لاستخراج كود التحقق.
    ترجع: (كود_التحقق, رسالة_الخطأ_إن_وجدت)
    """
    if not email_user or not email_password:
        return None, "لم يتم تحديد البريد الإلكتروني أو كلمة مرور البريد في ملف الإعدادات (.env)."

    host = get_imap_host_for_email(email_user, imap_host)
    logger.info(f"📧 جاري مراقبة البريد ({email_user}) عبر السيرفر ({host}:{imap_port}) لاستخراج رمز التحقق...")

    start_time = time.time()
    
    while time.time() - start_time < max_wait_seconds:
        mail = None
        try:
            mail = imaplib.IMAP4_SSL(host, imap_port)
            mail.login(email_user, email_password)
            mail.select("INBOX")

            # البحث عن الرسائل
            # 1. محاولة البحث عن الرسائل غير المقروءة أولاً
            status, messages = mail.search(None, '(UNSEEN)')
            mail_ids = messages[0].split() if (status == "OK" and messages and messages[0]) else []

            # 2. إذا لم تكن هناك غير مقروءة، نأخذ آخر 5 رسائل في الصندوق
            if not mail_ids:
                status, all_messages = mail.search(None, "ALL")
                if status == "OK" and all_messages and all_messages[0]:
                    all_ids = all_messages[0].split()
                    mail_ids = all_ids[-5:]  # آخر 5 رسائل

            if mail_ids:
                # فحص من الأحدث إلى الأقدم
                for msg_id in reversed(mail_ids):
                    _, msg_data = mail.fetch(msg_id, "(RFC822)")
                    if not msg_data or not msg_data[0]:
                        continue

                    for response_part in msg_data:
                        if isinstance(response_part, tuple):
                            msg = email.message_from_bytes(response_part[1])
                            sender = decode_str(msg.get("From", ""))
                            subject = decode_str(msg.get("Subject", ""))

                            # استخراج نص الرسالة
                            body = ""
                            if msg.is_multipart():
                                for part in msg.walk():
                                    ctype = part.get_content_type()
                                    if ctype in ("text/plain", "text/html"):
                                        payload = part.get_payload(decode=True)
                                        if payload:
                                            body += payload.decode(errors="ignore") + "\n"
                            else:
                                payload = msg.get_payload(decode=True)
                                if payload:
                                    body = payload.decode(errors="ignore")

                            # التحقق إن كانت الرسالة من YU-OTP أو تخص رمز التحقق
                            is_yu_msg = (
                                "YU-OTP" in sender or
                                "رمز التحقق" in subject or
                                "نظام معلومات الطلبة" in subject or
                                "نظام معلومات الطلبة" in body or
                                "رمز التحقق الخاص بك هو" in body
                            )

                            if is_yu_msg:
                                otp = extract_otp_from_text(body) or extract_otp_from_text(subject)
                                if otp:
                                    logger.info(f"✅ تم العثور على رمز التحقق من رسالة ({sender} - {subject}): {otp}")
                                    try:
                                        mail.store(msg_id, '+FLAGS', '\\Seen')
                                        mail.logout()
                                    except Exception:
                                        pass
                                    return otp, None

            try:
                mail.logout()
            except Exception:
                pass

        except imaplib.IMAP4.error as e:
            err_msg = str(e)
            logger.warning(f"خطأ تسجيل الدخول لبريدك ({email_user}): {err_msg}")
            if "AUTHENTICATIONFAILED" in err_msg.upper() or "LOGIN" in err_msg.upper():
                return None, f"فشل تسجيل الدخول للبريد ({email_user}). إذا كان لديك تحقق بخطوتين في الإيميل، يرجى استخدام App Password."
        except Exception as e:
            logger.warning(f"ملاحظة أثناء فحص البريد: {e}")
        finally:
            if mail:
                try:
                    mail.close()
                except Exception:
                    pass

        time.sleep(poll_interval)

    return None, f"انتهت مهلة الانتظار ({max_wait_seconds} ثانية) دون وصول رسالة رمز التحقق إلى بريدك."
