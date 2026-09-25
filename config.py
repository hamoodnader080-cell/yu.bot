import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# محاولة تحميل dotenv إذا كانت مثبتة، أو قراءة ملف .env يدوياً
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


BOT_TOKEN = os.getenv("BOT_TOKEN", "8806949560:AAFRb33VN6nKSalRnyE2maqgkCAwqq1WO60").strip()
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "10"))
DEMO_MODE = os.getenv("DEMO_MODE", "False").lower() in ("true", "1", "yes", "y")

STUDENT_ID = os.getenv("STUDENT_ID", "2024827015").strip()
STUDENT_PASSWORD = os.getenv("STUDENT_PASSWORD", "m1234123").strip()
SIS_SESSION_ID = os.getenv("SIS_SESSION_ID", "111648752755341").strip()
SIS_SESSION_COOKIE = os.getenv("SIS_SESSION_COOKIE", "").strip()
YU_TERM_ID = os.getenv("YU_TERM_ID", "8593").strip()

# إعدادات البريد الإلكتروني لقراءة رمز التحقق OTP
EMAIL_USER = os.getenv("EMAIL_USER", "majoodnader05@gmail.com").strip()
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "jktgfzmxwdknlyfn").strip()
EMAIL_HOST = os.getenv("EMAIL_HOST", "imap.gmail.com").strip()
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "993"))


DATABASE_PATH = os.getenv("DATABASE_PATH", str(BASE_DIR / "yu_tracker.db"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

MAX_COURSES_PER_USER = int(os.getenv("MAX_COURSES_PER_USER", "10"))
YU_PORTAL_URL = os.getenv("YU_PORTAL_URL", "https://sis.yu.edu.jo")


# إعدادات المالك (Owner) والأدمن (Admin) ومفاتيح التفعيل
OWNER_ID_RAW = os.getenv("OWNER_ID", "7566322988").strip()
OWNER_ID = int(OWNER_ID_RAW) if OWNER_ID_RAW.isdigit() else 7566322988

ADMIN_ID_RAW = os.getenv("ADMIN_ID", "7566322988").strip()
ADMIN_IDS = [int(x.strip()) for x in ADMIN_ID_RAW.split(",") if x.strip().isdigit()]
if OWNER_ID not in ADMIN_IDS:
    ADMIN_IDS.append(OWNER_ID)

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "mhmdnader5").strip().lstrip("@")
ADMIN_PHONE = os.getenv("ADMIN_PHONE", "962778356084").strip().lstrip("+").replace(" ", "")
REQUIRE_ACTIVATION = os.getenv("REQUIRE_ACTIVATION", "True").lower() in ("true", "1", "yes", "y")
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID", "-1004400753235").strip()



def get_owner_contact_info():
    """إرجاع (اسم_العرض, رابط_المحادثة_المباشر)"""
    if ADMIN_USERNAME:
        return f"@{ADMIN_USERNAME}", f"https://t.me/{ADMIN_USERNAME}"
    elif ADMIN_PHONE:
        return f"+{ADMIN_PHONE}", f"https://t.me/+{ADMIN_PHONE}"
    else:
        return "مالك البوت", "https://t.me/+962778356084"



# إعدادات الحماية من الحظر
REQUEST_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

