"""
أداة اختبار تسجيل الدخول لنظام SIS والتحقق التلقائي من رمز OTP عبر البريد الإلكتروني 🎓
"""
import sys
import os

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import logging
from config import (
    STUDENT_ID,
    STUDENT_PASSWORD,
    EMAIL_USER,
    EMAIL_PASSWORD,
    EMAIL_HOST,
    EMAIL_PORT
)
from email_otp import fetch_latest_yu_otp
from scraper import YarmoukScraper

# إعداد السجلات لعرض التفاصيل بوضوح
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("TestOTP")

def run_test():
    print("\n" + "="*60)
    print("🎓 فحص إعدادات تسجيل الدخول والربط التلقائي مع البريد (YU-OTP)")
    print("="*60 + "\n")

    print(f"📌 الرقم الجامعي (STUDENT_ID): {STUDENT_ID or '❌ غير محدد'}")
    print(f"📌 كلمة سر المنصة (STUDENT_PASSWORD): {'******' if STUDENT_PASSWORD else '❌ غير محددة'}")
    print(f"📌 البريد الإلكتروني (EMAIL_USER): {EMAIL_USER or '❌ غير محدد'}")
    print(f"📌 كلمة سر البريد (EMAIL_PASSWORD): {'******' if EMAIL_PASSWORD else '❌ غير محددة'}")
    print(f"📌 سيرفر البريد (EMAIL_HOST): {EMAIL_HOST or 'تلقائي (outlook.office365.com / imap.gmail.com)'}")
    print("-" * 60)

    if not STUDENT_ID or not STUDENT_PASSWORD:
        print("❌ تنبيه: يجب تعبئة STUDENT_ID و STUDENT_PASSWORD في ملف .env أولاً.")
        return

    if not EMAIL_USER or not EMAIL_PASSWORD:
        print("❌ تنبيه: يجب تعبئة EMAIL_USER و EMAIL_PASSWORD في ملف .env لقراءة كود التحقق تلقائياً.")
        return

    print("\n🚀 الخطوة 1: بدء تسجيل الدخول في نظام SIS واستدعاء الكود تلقائياً...")
    scraper = YarmoukScraper()
    success = scraper.auto_relogin()

    if success:
        print("\n" + "🎉"*20)
        print("✅ نجح تسجيل الدخول التلقائي بالكامل وتم استخراج الجلسة والتوكنز بنجاح!")
        print(f"🔑 معرف الجلسة النشط (Session ID): {scraper.session_id}")
        print("="*60 + "\n")
    else:
        print("\n❌ فشل تسجيل الدخول. يرجى مراجعة رسائل السجل أعلاه للتأكد من صحة البيانات.")

if __name__ == "__main__":
    run_test()
