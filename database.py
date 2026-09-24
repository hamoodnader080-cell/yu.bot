import sqlite3
import secrets
import string
import re
import json
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any, Tuple
from contextlib import contextmanager
from config import DATABASE_PATH, DATABASE_URL, MAX_COURSES_PER_USER

logger = logging.getLogger(__name__)

# فحص توفر مكتبة PostgreSQL
PSYCOPG2_AVAILABLE = False
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from psycopg2 import IntegrityError as PgIntegrityError
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PgIntegrityError = Exception

_USE_POSTGRES: Optional[bool] = None


def is_postgres() -> bool:
    """التحقق مما إذا كان البوت يستخدم قاعدة بيانات PostgreSQL السحابية مع تجربة الاتصال التلقائية"""
    global _USE_POSTGRES
    if _USE_POSTGRES is not None:
        return _USE_POSTGRES
    if not (DATABASE_URL and PSYCOPG2_AVAILABLE):
        _USE_POSTGRES = False
        return False
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=3)
        conn.close()
        _USE_POSTGRES = True
        return True
    except Exception as e:
        logger.warning(f"⚠️ تعذر الاتصال بـ PostgreSQL السحابية ({e}) - التحويل التلقائي لقاعدة SQLite المحلية.")
        _USE_POSTGRES = False
        return False


@contextmanager
def get_db_cursor():
    """Context manager يوفر مؤشر قاعدة البيانات المناسب (PostgreSQL أو SQLite)"""
    if is_postgres():
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        try:
            yield cursor, True
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()
    else:
        conn = sqlite3.connect(DATABASE_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        try:
            yield cursor, False
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()


def _format_sql(sql: str, is_pg: bool) -> str:
    """تحويل المعاملات من ? إلى %s في حال كانت قاعدة البيانات PostgreSQL"""
    if is_pg:
        return sql.replace("?", "%s")
    return sql


def _get_scalar(row: Any) -> Any:
    """استخراج قيمة مفردة بأمان من الصف سواء كان Dict أو Row"""
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()))
def _sync_seed_data(cursor, is_pg: bool) -> None:
    """مزامنة بيانات المشتركين والمفاتيح والإعدادات تلقائياً عند التشغيل على السحابة"""
    seed_file = config.BASE_DIR / "seed_data.json"
    if not seed_file.exists():
        return
    try:
        with open(seed_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        if is_pg:
            # مسح المفاتيح غير المستخدمة القديمة وتثبيت المفاتيح الحقيقية
            cursor.execute("DELETE FROM activation_keys WHERE is_used = 0;")

            # 1. مفاتيح التفعيل
            for k in data.get("activation_keys", []):
                sql_k = """
                    INSERT INTO activation_keys (key_code, duration_days, max_courses, is_used, used_by_user_id, used_by_username, used_by_first_name, used_at, expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(key_code) DO UPDATE SET
                        is_used = EXCLUDED.is_used,
                        used_by_user_id = EXCLUDED.used_by_user_id,
                        used_by_username = EXCLUDED.used_by_username,
                        used_by_first_name = EXCLUDED.used_by_first_name,
                        used_at = EXCLUDED.used_at,
                        expires_at = EXCLUDED.expires_at
                """
                cursor.execute(sql_k, (
                    k.get("key_code"), k.get("duration_days", 0), k.get("max_courses", 10),
                    k.get("is_used", 0), k.get("used_by_user_id"), k.get("used_by_username", ""),
                    k.get("used_by_first_name", ""), k.get("used_at"), k.get("expires_at")
                ))

            # 2. المستخدمين المفعّلين
            for u in data.get("activated_users", []):
                sql_u = """
                    INSERT INTO activated_users (user_id, username, first_name, key_code, is_active, max_courses, activated_at, expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(user_id) DO UPDATE SET
                        username = EXCLUDED.username,
                        first_name = EXCLUDED.first_name,
                        key_code = EXCLUDED.key_code,
                        is_active = EXCLUDED.is_active,
                        max_courses = EXCLUDED.max_courses,
                        expires_at = EXCLUDED.expires_at
                """
                cursor.execute(sql_u, (
                    u.get("user_id"), u.get("username", ""), u.get("first_name", ""),
                    u.get("key_code", ""), u.get("is_active", 1), u.get("max_courses", 10),
                    u.get("activated_at"), u.get("expires_at")
                ))

            # 3. الشعب المراقبة
            for c in data.get("tracked_courses", []):
                sql_c = """
                    INSERT INTO tracked_courses (user_id, chat_id, course_no, course_name, section_no, capacity, registered, available_seats, last_status, is_active, notified)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(user_id, course_no, section_no) DO UPDATE SET
                        capacity = EXCLUDED.capacity,
                        registered = EXCLUDED.registered,
                        available_seats = EXCLUDED.available_seats,
                        last_status = EXCLUDED.last_status,
                        is_active = EXCLUDED.is_active
                """
                cursor.execute(sql_c, (
                    c.get("user_id"), c.get("chat_id"), c.get("course_no"),
                    c.get("course_name", ""), c.get("section_no"), c.get("capacity", 0),
                    c.get("registered", 0), c.get("available_seats", 0),
                    c.get("last_status", "UNKNOWN"), c.get("is_active", 1), c.get("notified", 0)
                ))

            # 4. إعدادات البوت والمشرفين
            for s in data.get("bot_settings", []):
                sql_s = """
                    INSERT INTO bot_settings (setting_key, setting_val, updated_at)
                    VALUES (%s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT(setting_key) DO UPDATE SET setting_val = EXCLUDED.setting_val
                """
                cursor.execute(sql_s, (s.get("setting_key"), s.get("setting_val")))
    except Exception as e:
        logger.warning(f"⚠️ Error syncing seed data: {e}")


def init_db() -> None:
    """إنشاء جداول قاعدة البيانات إذا لم تكن موجودة (يدعم SQLite و PostgreSQL)"""
    with get_db_cursor() as (cursor, is_pg):
        if is_pg:
            # جداول PostgreSQL السحابية
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tracked_courses (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    chat_id BIGINT NOT NULL,
                    course_no TEXT NOT NULL,
                    course_name TEXT,
                    section_no TEXT NOT NULL,
                    capacity INTEGER DEFAULT 0,
                    registered INTEGER DEFAULT 0,
                    available_seats INTEGER DEFAULT 0,
                    last_status TEXT DEFAULT 'UNKNOWN',
                    is_active INTEGER DEFAULT 1,
                    notified INTEGER DEFAULT 0,
                    last_alert_msg_id BIGINT DEFAULT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, course_no, section_no)
                );
            """)

            try:
                cursor.execute("ALTER TABLE tracked_courses ADD COLUMN last_alert_msg_id BIGINT DEFAULT NULL;")
            except Exception:
                pass

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS activation_keys (
                    id SERIAL PRIMARY KEY,
                    key_code TEXT UNIQUE NOT NULL,
                    duration_days INTEGER DEFAULT 0,
                    max_courses INTEGER DEFAULT 10,
                    is_used INTEGER DEFAULT 0,
                    used_by_user_id BIGINT DEFAULT NULL,
                    used_by_username TEXT DEFAULT NULL,
                    used_at TIMESTAMP DEFAULT NULL,
                    expires_at TIMESTAMP DEFAULT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            try:
                cursor.execute("ALTER TABLE activation_keys ADD COLUMN used_by_first_name TEXT DEFAULT NULL;")
            except Exception:
                pass

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS activated_users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    key_code TEXT,
                    is_active INTEGER DEFAULT 1,
                    max_courses INTEGER DEFAULT 10,
                    activated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP DEFAULT NULL
                );
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS bot_settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_val TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            _sync_seed_data(cursor, is_pg=True)
            logger.info("✅ PostgreSQL Database Initialized and Synced Successfully!")
        else:
            # جداول SQLite المحلية
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tracked_courses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    course_no TEXT NOT NULL,
                    course_name TEXT,
                    section_no TEXT NOT NULL,
                    capacity INTEGER DEFAULT 0,
                    registered INTEGER DEFAULT 0,
                    available_seats INTEGER DEFAULT 0,
                    last_status TEXT DEFAULT 'UNKNOWN',
                    is_active INTEGER DEFAULT 1,
                    notified INTEGER DEFAULT 0,
                    last_alert_msg_id INTEGER DEFAULT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, course_no, section_no)
                );
            """)

            try:
                cursor.execute("ALTER TABLE tracked_courses ADD COLUMN last_alert_msg_id INTEGER DEFAULT NULL;")
            except Exception:
                pass

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS activation_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key_code TEXT UNIQUE NOT NULL,
                    duration_days INTEGER DEFAULT 0,
                    max_courses INTEGER DEFAULT 10,
                    is_used INTEGER DEFAULT 0,
                    used_by_user_id INTEGER DEFAULT NULL,
                    used_by_username TEXT DEFAULT NULL,
                    used_by_first_name TEXT DEFAULT NULL,
                    used_at TIMESTAMP DEFAULT NULL,
                    expires_at TIMESTAMP DEFAULT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            try:
                cursor.execute("ALTER TABLE activation_keys ADD COLUMN used_by_first_name TEXT DEFAULT NULL;")
            except Exception:
                pass

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS activated_users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    key_code TEXT,
                    is_active INTEGER DEFAULT 1,
                    max_courses INTEGER DEFAULT 10,
                    activated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP DEFAULT NULL
                );
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS bot_settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_val TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            logger.info("✅ SQLite Database Initialized Successfully!")



def add_tracked_course(
    user_id: int,
    chat_id: int,
    course_no: str,
    course_name: str,
    section_no: str
) -> Dict[str, Any]:
    """إضافة مادة وشعبة جديدة للمراقبة أو إعادة تفعيلها"""
    course_no = course_no.strip().upper()
    section_no = section_no.strip()
    course_name = course_name.strip() if course_name else "غير محدد"

    sql_insert = """
        INSERT INTO tracked_courses (
            user_id, chat_id, course_no, course_name, section_no, 
            is_active, notified, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id, course_no, section_no) 
        DO UPDATE SET 
            is_active = 1,
            notified = 0,
            course_name = excluded.course_name,
            updated_at = CURRENT_TIMESTAMP
    """
    sql_select = "SELECT * FROM tracked_courses WHERE user_id = ? AND course_no = ? AND section_no = ?"

    with get_db_cursor() as (cursor, is_pg):
        cursor.execute(_format_sql(sql_insert, is_pg), (user_id, chat_id, course_no, course_name, section_no))
        cursor.execute(_format_sql(sql_select, is_pg), (user_id, course_no, section_no))
        row = cursor.fetchone()
        return dict(row) if row else {}


def get_user_courses(user_id: int, active_only: bool = False) -> List[Dict[str, Any]]:
    """جلب قائمة المواد الخاصة بمستخدم معين"""
    with get_db_cursor() as (cursor, is_pg):
        if active_only:
            sql = "SELECT * FROM tracked_courses WHERE user_id = ? AND is_active = 1 ORDER BY id DESC"
        else:
            sql = "SELECT * FROM tracked_courses WHERE user_id = ? ORDER BY is_active DESC, id DESC"
        cursor.execute(_format_sql(sql, is_pg), (user_id,))
        rows = cursor.fetchall()
        return [dict(r) for r in rows]


def get_all_active_courses() -> List[Dict[str, Any]]:
    """جلب جميع المواد المراقبة لجميع المستخدمين لتنفيذ الفحص الدوري"""
    with get_db_cursor() as (cursor, is_pg):
        sql = "SELECT * FROM tracked_courses WHERE is_active = 1"
        cursor.execute(sql)
        rows = cursor.fetchall()
        return [dict(r) for r in rows]


def update_course_status(
    course_id: int,
    capacity: int,
    registered: int,
    available_seats: int,
    last_status: str,
    notified: Optional[int] = None,
    course_name: Optional[str] = None,
    last_alert_msg_id: Optional[int] = None
) -> None:
    """تحديث نتائج الفحص الأخيرة لمادة معينة واسمها الحقيقي ومعرف رسالة التنبيه الحية"""
    with get_db_cursor() as (cursor, is_pg):
        set_clauses = [
            "capacity = ?",
            "registered = ?",
            "available_seats = ?",
            "last_status = ?",
            "updated_at = CURRENT_TIMESTAMP"
        ]
        params = [capacity, registered, available_seats, last_status]

        if notified is not None:
            set_clauses.append("notified = ?")
            params.append(notified)

        if course_name:
            set_clauses.append("course_name = ?")
            params.append(course_name)

        if last_alert_msg_id is not None:
            set_clauses.append("last_alert_msg_id = ?")
            params.append(last_alert_msg_id)

        params.append(course_id)
        sql = f"UPDATE tracked_courses SET {', '.join(set_clauses)} WHERE id = ?"
        cursor.execute(_format_sql(sql, is_pg), tuple(params))


def stop_tracking_course(course_id: int, user_id: int) -> bool:
    """إيقاف مراقبة مادة محددة"""
    sql = "UPDATE tracked_courses SET is_active = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?"
    with get_db_cursor() as (cursor, is_pg):
        cursor.execute(_format_sql(sql, is_pg), (course_id, user_id))
        return cursor.rowcount > 0


def delete_course(course_id: int, user_id: int) -> bool:
    """حذف مادة نهائياً من قائمة المراقبة"""
    sql = "DELETE FROM tracked_courses WHERE id = ? AND user_id = ?"
    with get_db_cursor() as (cursor, is_pg):
        cursor.execute(_format_sql(sql, is_pg), (course_id, user_id))
        return cursor.rowcount > 0


def get_course_by_id(course_id: int, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """جلب مادة بواسطة المعرف ID"""
    with get_db_cursor() as (cursor, is_pg):
        if user_id:
            sql = "SELECT * FROM tracked_courses WHERE id = ? AND user_id = ?"
            params = (course_id, user_id)
        else:
            sql = "SELECT * FROM tracked_courses WHERE id = ?"
            params = (course_id,)
        cursor.execute(_format_sql(sql, is_pg), params)
        row = cursor.fetchone()
        return dict(row) if row else None


def get_user_course_count(user_id: int) -> int:
    """حساب عدد المواد المراقبة حالياً للمستخدم"""
    sql = "SELECT COUNT(*) FROM tracked_courses WHERE user_id = ? AND is_active = 1"
    with get_db_cursor() as (cursor, is_pg):
        cursor.execute(_format_sql(sql, is_pg), (user_id,))
        row = cursor.fetchone()
        return int(_get_scalar(row) or 0)


def is_already_tracking(user_id: int, course_no: str, section_no: str) -> bool:
    """التحقق مما إذا كانت الشعبة مراقبة مسبقاً من قبل المستخدم"""
    sql = """
        SELECT 1 FROM tracked_courses 
        WHERE user_id = ? AND UPPER(course_no) = UPPER(?) AND section_no = ? AND is_active = 1
    """
    with get_db_cursor() as (cursor, is_pg):
        cursor.execute(_format_sql(sql, is_pg), (user_id, course_no.strip(), section_no.strip()))
        return cursor.fetchone() is not None


# ========================================================
# دوال إدارة التفعيل ومفاتيح الاشتراكات (Activation Keys)
# ========================================================

def _generate_random_key_code(prefix: str = "YU") -> str:
    """توليد كود تفعيل مميز مثل YU-8A9F-7D21-B45C"""
    chars = string.ascii_uppercase + string.digits
    # استبعاد الأحرف المتشابهة لتسهيل القراءة (0, O, I, 1)
    clean_chars = "".join([c for c in chars if c not in ("O", "0", "I", "1")])
    part1 = "".join(secrets.choice(clean_chars) for _ in range(4))
    part2 = "".join(secrets.choice(clean_chars) for _ in range(4))
    part3 = "".join(secrets.choice(clean_chars) for _ in range(4))
    return f"{prefix}-{part1}-{part2}-{part3}"


def create_activation_key(
    duration_days: int = 0,
    max_courses: int = 10,
    prefix: str = "YU"
) -> str:
    """توليد مفتاح تفعيل جديد وتخزينه بقاعدة البيانات"""
    sql = "INSERT INTO activation_keys (key_code, duration_days, max_courses, is_used) VALUES (?, ?, ?, 0)"
    while True:
        key_code = _generate_random_key_code(prefix)
        try:
            with get_db_cursor() as (cursor, is_pg):
                cursor.execute(_format_sql(sql, is_pg), (key_code, duration_days, max_courses))
                return key_code
        except (sqlite3.IntegrityError, PgIntegrityError):
            continue


def create_bulk_activation_keys(
    count: int,
    duration_days: int = 0,
    max_courses: int = 10,
    prefix: str = "YU"
) -> List[str]:
    """توليد عدة مفاتيح دفعة واحدة"""
    keys = []
    for _ in range(count):
        keys.append(create_activation_key(duration_days, max_courses, prefix))
    return keys


def activate_user_with_key(
    user_id: int,
    username: Optional[str],
    first_name: Optional[str],
    raw_key: str
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """
    تفعيل مستخدم باستخدام مفتاح:
    - يستخرج الكود بذكاء عبر Regex حتى لو تم لصق رسالة كاملة
    - يتحقق من وجود المفتاح
    - يتأكد أنه غير مستخدم من حساب آخر
    - يربط المفتاح بحساب هذا المستخدم بشكل دائم لا ينحذف إطلاقاً
    """
    cleaned_input = raw_key.strip().upper()
    match = re.search(r'YU-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}', cleaned_input, re.IGNORECASE)
    key_code = match.group(0).upper() if match else cleaned_input
    
    username_clean = username or ""
    first_name_clean = first_name or ""

    with get_db_cursor() as (cursor, is_pg):
        # البحث عن المفتاح
        sql_find = "SELECT * FROM activation_keys WHERE UPPER(key_code) = ?"
        cursor.execute(_format_sql(sql_find, is_pg), (key_code,))
        key_row = cursor.fetchone()

        if not key_row:
            return False, "❌ كود التفعيل غير صالح! تأكد من كتابة الكود بدقة كما وصلك.", None

        key_data = dict(key_row)

        # إذا كان المفتاح مستخدماً مسبقاً
        if key_data.get("is_used") == 1:
            used_uid = key_data.get("used_by_user_id")
            if used_uid and int(used_uid) == int(user_id):
                # إعادة تثبيت وتأكيد التفعيل الدائم للحساب نفسه
                sql_reup = """
                    INSERT INTO activated_users (
                        user_id, username, first_name, key_code, is_active, 
                        max_courses, activated_at, expires_at
                    )
                    VALUES (?, ?, ?, ?, 1, ?, CURRENT_TIMESTAMP, NULL)
                    ON CONFLICT(user_id) DO UPDATE SET 
                        is_active = 1,
                        key_code = excluded.key_code,
                        max_courses = excluded.max_courses
                """
                max_c = key_data.get("max_courses") or 999
                cursor.execute(_format_sql(sql_reup, is_pg), (user_id, username_clean, first_name_clean, key_code, max_c))
                return True, "✅ حسابك مفعّل مسبقاً بهذا المفتاح واشتراكك نشط ودائم! ♾️", key_data
            else:
                return False, "❌ هذا المفتاح تم استخدامه وتفعيله مسبقاً لحساب آخر وغير صالح!", None

        # حساب مدة المفتاح (دائم مفتوح افتراضياً)
        expires_at_val = None
        expires_at_str = "دائم ومفتوح ♾️ (طوال الفصل)"
        if key_data.get("duration_days") and int(key_data["duration_days"]) > 0:
            exp_date = datetime.now() + timedelta(days=int(key_data["duration_days"]))
            expires_at_val = exp_date if is_pg else exp_date.strftime("%Y-%m-%d %H:%M:%S")
            expires_at_str = exp_date.strftime("%Y-%m-%d %H:%M:%S")

        # تحديث حالة المفتاح ليصبح مستخدماً ومربوطاً بـ user_id
        sql_update_key = """
            UPDATE activation_keys 
            SET is_used = 1, used_by_user_id = ?, used_by_username = ?, used_by_first_name = ?,
                used_at = CURRENT_TIMESTAMP, expires_at = ?
            WHERE id = ?
        """
        cursor.execute(_format_sql(sql_update_key, is_pg), (user_id, username_clean, first_name_clean, expires_at_val, key_data["id"]))

        # إضافة أو تحديث المستخدم في جدول المستخدمين المفعّلين بشكل دائم
        max_c = key_data.get("max_courses") or 999
        sql_upsert_user = """
            INSERT INTO activated_users (
                user_id, username, first_name, key_code, is_active, 
                max_courses, activated_at, expires_at
            )
            VALUES (?, ?, ?, ?, 1, ?, CURRENT_TIMESTAMP, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                key_code = excluded.key_code,
                username = excluded.username,
                first_name = excluded.first_name,
                is_active = 1,
                max_courses = excluded.max_courses,
                activated_at = CURRENT_TIMESTAMP,
                expires_at = excluded.expires_at
        """
        cursor.execute(_format_sql(sql_upsert_user, is_pg), (user_id, username_clean, first_name_clean, key_code, max_c, expires_at_val))

        return True, "🎉 تم تفعيل اشتراكك بنجاح! تم حفظ تفعيل حسابك بشكل دائم ولا ينحذف إطلاقاً. يمكنك الآن استخدام البوت ومراقبة المقاعد بحرية.", {
            "key_code": key_code,
            "duration_days": key_data.get("duration_days", 0),
            "expires_at": expires_at_str,
            "max_courses": max_c
        }


def is_user_activated(user_id: int) -> Tuple[bool, str, int]:
    """
    التحقق من حالة تفعيل المستخدم:
    يرجع: (is_active, status_description, max_courses)
    الاشتراكات دائمة لجميع الطلاب طوال الفصل الدراسي ولا تنتهي تلقائياً.
    """
    with get_db_cursor() as (cursor, is_pg):
        sql = "SELECT * FROM activated_users WHERE user_id = ?"
        cursor.execute(_format_sql(sql, is_pg), (user_id,))
        user_row = cursor.fetchone()

        if user_row:
            user_data = dict(user_row)
            if user_data.get("is_active") == 1:
                max_c = user_data.get("max_courses") or 999
                return True, "ACTIVE", max_c

        # فحص ذاتي إضافي في جدول المفاتيح (إذا كان الحساب قد استخدم كود تفعيل مسبقاً)
        sql_key = "SELECT * FROM activation_keys WHERE used_by_user_id = ? ORDER BY id DESC LIMIT 1"
        cursor.execute(_format_sql(sql_key, is_pg), (user_id,))
        key_row = cursor.fetchone()
        if key_row:
            k_data = dict(key_row)
            max_c = k_data.get("max_courses") or 999
            # استعادة وتثبيت التفعيل تلقائياً لمنع أي فقدان للتفعيل
            sql_fix = """
                INSERT INTO activated_users (user_id, username, first_name, key_code, is_active, max_courses, activated_at, expires_at)
                VALUES (?, ?, ?, ?, 1, ?, CURRENT_TIMESTAMP, NULL)
                ON CONFLICT(user_id) DO UPDATE SET is_active = 1, max_courses = excluded.max_courses
            """
            cursor.execute(_format_sql(sql_fix, is_pg), (
                user_id,
                k_data.get("used_by_username", ""),
                k_data.get("used_by_first_name", ""),
                k_data.get("key_code", ""),
                max_c
            ))
            return True, "ACTIVE", max_c

        return False, "NOT_ACTIVATED", 0


def sync_user_profile(user_id: int, username: Optional[str] = None, first_name: Optional[str] = None) -> None:
    """تحديث الاسم الشخصي واسم المستخدم في قاعدة البيانات عند أي تفاعل"""
    if not user_id:
        return
    u_clean = username.strip() if username else ""
    f_clean = first_name.strip() if first_name else ""
    if not u_clean and not f_clean:
        return
    try:
        with get_db_cursor() as (cursor, is_pg):
            if f_clean and u_clean:
                sql_u = "UPDATE activated_users SET username = ?, first_name = ? WHERE user_id = ?"
                cursor.execute(_format_sql(sql_u, is_pg), (u_clean, f_clean, user_id))
                sql_k = "UPDATE activation_keys SET used_by_username = ?, used_by_first_name = ? WHERE used_by_user_id = ?"
                cursor.execute(_format_sql(sql_k, is_pg), (u_clean, f_clean, user_id))
            elif f_clean:
                sql_u = "UPDATE activated_users SET first_name = ? WHERE user_id = ?"
                cursor.execute(_format_sql(sql_u, is_pg), (f_clean, user_id))
                sql_k = "UPDATE activation_keys SET used_by_first_name = ? WHERE used_by_user_id = ?"
                cursor.execute(_format_sql(sql_k, is_pg), (f_clean, user_id))
            elif u_clean:
                sql_u = "UPDATE activated_users SET username = ? WHERE user_id = ?"
                cursor.execute(_format_sql(sql_u, is_pg), (u_clean, user_id))
                sql_k = "UPDATE activation_keys SET used_by_username = ? WHERE used_by_user_id = ?"
                cursor.execute(_format_sql(sql_k, is_pg), (u_clean, user_id))
    except Exception:
        pass


def get_user_activation_details(user_id: int) -> Optional[Dict[str, Any]]:
    """جلب تفاصيل تفعيل مستخدم محدد"""
    sql = "SELECT * FROM activated_users WHERE user_id = ?"
    with get_db_cursor() as (cursor, is_pg):
        cursor.execute(_format_sql(sql, is_pg), (user_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_all_keys(filter_status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    """
    جلب المفاتيح مع بيانات المستخدم المرتبط (إن وُجد) مع إمكانية الفلترة:
    filter_status: 'unused', 'used', or None (all)
    """
    where_clause = ""
    if filter_status == "unused":
        where_clause = "WHERE k.is_used = 0"
    elif filter_status == "used":
        where_clause = "WHERE k.is_used = 1"

    sql = f"""
        SELECT 
            k.id, k.key_code, k.duration_days, k.max_courses, 
            k.is_used, k.used_by_user_id, k.used_by_username, 
            k.used_at, k.expires_at, k.created_at,
            COALESCE(NULLIF(u.first_name, ''), NULLIF(k.used_by_first_name, '')) as user_first_name,
            u.is_active as user_is_active
        FROM activation_keys k
        LEFT JOIN activated_users u ON k.used_by_user_id = u.user_id
        {where_clause}
        ORDER BY k.id DESC
        LIMIT ?
    """
    with get_db_cursor() as (cursor, is_pg):
        cursor.execute(_format_sql(sql, is_pg), (limit,))
        rows = cursor.fetchall()
        return [dict(r) for r in rows]


def get_all_keys_paginated(
    filter_status: Optional[str] = None,
    page: int = 1,
    per_page: int = 5
) -> Tuple[List[Dict[str, Any]], int, int]:
    """
    جلب المفاتيح بنظام الصفحات (Pagination) للأدمن:
    يرجع: (قائمة المفاتيح, إجمالي العدد, إجمالي الصفحات)
    """
    page = max(1, page)
    offset = (page - 1) * per_page

    where_clause = ""
    if filter_status == "unused":
        where_clause = "WHERE k.is_used = 0"
    elif filter_status == "used":
        where_clause = "WHERE k.is_used = 1"

    count_sql = f"SELECT COUNT(*) FROM activation_keys k {where_clause}"

    data_sql = f"""
        SELECT 
            k.id, k.key_code, k.duration_days, k.max_courses, 
            k.is_used, k.used_by_user_id, k.used_by_username, 
            k.used_at, k.expires_at, k.created_at,
            COALESCE(NULLIF(u.first_name, ''), NULLIF(k.used_by_first_name, '')) as user_first_name,
            u.is_active as user_is_active
        FROM activation_keys k
        LEFT JOIN activated_users u ON k.used_by_user_id = u.user_id
        {where_clause}
        ORDER BY k.id DESC
        LIMIT ? OFFSET ?
    """

    with get_db_cursor() as (cursor, is_pg):
        cursor.execute(_format_sql(count_sql, is_pg))
        total_count = int(_get_scalar(cursor.fetchone()) or 0)

        total_pages = max(1, (total_count + per_page - 1) // per_page)
        if page > total_pages:
            page = total_pages
            offset = (page - 1) * per_page

        cursor.execute(_format_sql(data_sql, is_pg), (per_page, offset))
        rows = cursor.fetchall()
        keys = [dict(r) for r in rows]

        return keys, total_count, total_pages


def get_key_by_id(key_id: int) -> Optional[Dict[str, Any]]:
    """جلب تفاصيل مفتاح عبر رقمه التعريفي ID"""
    sql = """
        SELECT 
            k.*, 
            COALESCE(NULLIF(u.first_name, ''), NULLIF(k.used_by_first_name, '')) as user_first_name,
            u.is_active as user_is_active
        FROM activation_keys k
        LEFT JOIN activated_users u ON k.used_by_user_id = u.user_id
        WHERE k.id = ?
    """
    with get_db_cursor() as (cursor, is_pg):
        cursor.execute(_format_sql(sql, is_pg), (key_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_key_by_code(key_code: str) -> Optional[Dict[str, Any]]:
    """جلب تفاصيل مفتاح عبر كود المفتاح"""
    sql = """
        SELECT 
            k.*, 
            COALESCE(NULLIF(u.first_name, ''), NULLIF(k.used_by_first_name, '')) as user_first_name,
            u.is_active as user_is_active
        FROM activation_keys k
        LEFT JOIN activated_users u ON k.used_by_user_id = u.user_id
        WHERE UPPER(k.key_code) = ?
    """
    with get_db_cursor() as (cursor, is_pg):
        cursor.execute(_format_sql(sql, is_pg), (key_code.strip().upper(),))
        row = cursor.fetchone()
        return dict(row) if row else None


def delete_key_by_id(key_id: int) -> bool:
    """حذف مفتاح من النظام عبر الـ ID"""
    sql = "DELETE FROM activation_keys WHERE id = ?"
    with get_db_cursor() as (cursor, is_pg):
        cursor.execute(_format_sql(sql, is_pg), (key_id,))
        return cursor.rowcount > 0


def delete_key(key_code: str) -> bool:
    """حذف مفتاح من النظام عبر الكود"""
    sql = "DELETE FROM activation_keys WHERE UPPER(key_code) = ?"
    with get_db_cursor() as (cursor, is_pg):
        cursor.execute(_format_sql(sql, is_pg), (key_code.strip().upper(),))
        return cursor.rowcount > 0


def get_all_activated_users() -> List[Dict[str, Any]]:
    """جلب قائمة جميع المستخدمين المفعّلين"""
    sql = "SELECT * FROM activated_users ORDER BY activated_at DESC"
    with get_db_cursor() as (cursor, is_pg):
        cursor.execute(sql)
        rows = cursor.fetchall()
        return [dict(r) for r in rows]


def revoke_user_activation(user_id: int) -> bool:
    """إلغاء تفعيل اشتراك مستخدم"""
    sql1 = "UPDATE activated_users SET is_active = 0 WHERE user_id = ?"
    sql2 = "UPDATE tracked_courses SET is_active = 0 WHERE user_id = ?"
    with get_db_cursor() as (cursor, is_pg):
        cursor.execute(_format_sql(sql1, is_pg), (user_id,))
        rc = cursor.rowcount
        cursor.execute(_format_sql(sql2, is_pg), (user_id,))
        return rc > 0


def get_system_stats() -> Dict[str, Any]:
    """إحصائيات عامة عن الاشتراكات والمواد المراقبة"""
    with get_db_cursor() as (cursor, is_pg):
        cursor.execute("SELECT COUNT(*) FROM activated_users WHERE is_active = 1")
        active_users_count = int(_get_scalar(cursor.fetchone()) or 0)

        cursor.execute("SELECT COUNT(*) FROM activation_keys WHERE is_used = 0")
        unused_keys_count = int(_get_scalar(cursor.fetchone()) or 0)

        cursor.execute("SELECT COUNT(*) FROM activation_keys WHERE is_used = 1")
        used_keys_count = int(_get_scalar(cursor.fetchone()) or 0)

        cursor.execute("SELECT COUNT(*) FROM tracked_courses WHERE is_active = 1")
        active_courses_count = int(_get_scalar(cursor.fetchone()) or 0)

        return {
            "active_users": active_users_count,
            "unused_keys": unused_keys_count,
            "used_keys": used_keys_count,
            "total_keys": unused_keys_count + used_keys_count,
            "active_courses": active_courses_count
        }


def get_setting(key: str, default: str = "") -> str:
    """جلب قيمة إعداد معين من قاعدة البيانات"""
    sql = "SELECT setting_val FROM bot_settings WHERE setting_key = ?"
    with get_db_cursor() as (cursor, is_pg):
        cursor.execute(_format_sql(sql, is_pg), (key,))
        row = cursor.fetchone()
        val = _get_scalar(row)
        return str(val) if val is not None else default


def set_setting(key: str, value: str) -> None:
    """تعيين أو تحديث قيمة إعداد في قاعدة البيانات"""
    sql = """
        INSERT INTO bot_settings (setting_key, setting_val, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(setting_key)
        DO UPDATE SET setting_val = excluded.setting_val, updated_at = CURRENT_TIMESTAMP
    """
    with get_db_cursor() as (cursor, is_pg):
        cursor.execute(_format_sql(sql, is_pg), (key, value))


def get_sub_admins_detailed() -> List[Dict[str, Any]]:
    """جلب قائمة الأدمنز الإضافيين مع تفاصيل أسمائهم ويوزراتهم"""
    raw = get_setting("sub_admins_data", "")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    # في حال وجود بيانات قديمة في sub_admins
    simple_ids = get_sub_admins()
    return [{"user_id": uid, "name": f"مشرف {uid}", "username": ""} for uid in simple_ids]


def get_sub_admins() -> List[int]:
    """جلب قائمة معرفات الأدمنز الإضافيين من قاعدة البيانات"""
    raw_data = get_setting("sub_admins_data", "")
    if raw_data:
        try:
            data = json.loads(raw_data)
            return [int(item["user_id"]) for item in data if "user_id" in item]
        except Exception:
            pass
    raw = get_setting("sub_admins", "")
    if not raw:
        return []
    return [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]


def add_sub_admin(user_id: int, name: str = "", username: str = "") -> bool:
    """إضافة أدمن جديد للنظام مع حفظ اسمه ويوزره"""
    admins_data = get_sub_admins_detailed()
    existing = next((item for item in admins_data if int(item.get("user_id", 0)) == user_id), None)
    if existing:
        if name:
            existing["name"] = name
        if username:
            existing["username"] = username
    else:
        admins_data.append({
            "user_id": user_id,
            "name": name or f"مشرف {user_id}",
            "username": username or "",
            "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
    set_setting("sub_admins_data", json.dumps(admins_data, ensure_ascii=False))
    uids = [str(item["user_id"]) for item in admins_data]
    set_setting("sub_admins", ",".join(uids))
    return True


def remove_sub_admin(user_id: int) -> bool:
    """إزالة أدمن من النظام"""
    admins_data = get_sub_admins_detailed()
    new_data = [item for item in admins_data if int(item.get("user_id", 0)) != user_id]
    if len(new_data) == len(admins_data):
        # محاولة فحص sub_admins العادية
        simple_ids = get_sub_admins()
        if user_id in simple_ids:
            simple_ids = [x for x in simple_ids if x != user_id]
            set_setting("sub_admins", ",".join(str(x) for x in simple_ids))
            return True
        return False
    set_setting("sub_admins_data", json.dumps(new_data, ensure_ascii=False))
    uids = [str(item["user_id"]) for item in new_data]
    set_setting("sub_admins", ",".join(uids))
    return True
