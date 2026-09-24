import sqlite3
import secrets
import string
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


def is_postgres() -> bool:
    """التحقق مما إذا كان البوت يستخدم قاعدة بيانات PostgreSQL السحابية"""
    return bool(DATABASE_URL and PSYCOPG2_AVAILABLE)


@contextmanager
def get_db_cursor():
    """Context manager يوفر مؤشر قاعدة البيانات المناسب (PostgreSQL أو SQLite)"""
    if is_postgres():
        conn = psycopg2.connect(DATABASE_URL)
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
    return row[0]


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
            logger.info("✅ PostgreSQL Database Initialized Successfully!")
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
                    used_at TIMESTAMP DEFAULT NULL,
                    expires_at TIMESTAMP DEFAULT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

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
    - يتحقق من وجود المفتاح
    - يتأكد أنه غير مستخدم من حساب آخر
    - يربط المفتاح بحساب هذا المستخدم فقط
    """
    key_code = raw_key.strip().upper()
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
        if key_data["is_used"] == 1:
            if key_data.get("used_by_user_id") == user_id:
                return True, "✅ حسابك مفعّل مسبقاً بهذا المفتاح!", key_data
            else:
                return False, "❌ هذا المفتاح تم استخدامه وتفعيله مسبقاً لحساب آخر وغير صالح!", None

        # حساب تاريخ انتهاء الصلاحية
        expires_at_val = None
        expires_at_str = None
        if key_data.get("duration_days") and key_data["duration_days"] > 0:
            exp_date = datetime.now() + timedelta(days=key_data["duration_days"])
            expires_at_val = exp_date if is_pg else exp_date.strftime("%Y-%m-%d %H:%M:%S")
            expires_at_str = exp_date.strftime("%Y-%m-%d %H:%M:%S")

        # تحديث حالة المفتاح ليصبح مستخدماً ومربوطاً بـ user_id
        sql_update_key = """
            UPDATE activation_keys 
            SET is_used = 1, used_by_user_id = ?, used_by_username = ?, 
                used_at = CURRENT_TIMESTAMP, expires_at = ?
            WHERE id = ?
        """
        cursor.execute(_format_sql(sql_update_key, is_pg), (user_id, username_clean, expires_at_val, key_data["id"]))

        # إضافة أو تحديث المستخدم في جدول المستخدمين المفعّلين
        max_c = key_data.get("max_courses") or MAX_COURSES_PER_USER
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

        return True, "🎉 تم تفعيل اشتراكك بنجاح! يمكنك الآن استخدام البوت ومراقبة المقاعد بحرية.", {
            "key_code": key_code,
            "duration_days": key_data.get("duration_days", 0),
            "expires_at": expires_at_str,
            "max_courses": max_c
        }


def is_user_activated(user_id: int) -> Tuple[bool, str, int]:
    """
    التحقق من حالة تفعيل المستخدم:
    يرجع: (is_active, status_description, max_courses)
    """
    with get_db_cursor() as (cursor, is_pg):
        sql = "SELECT * FROM activated_users WHERE user_id = ?"
        cursor.execute(_format_sql(sql, is_pg), (user_id,))
        user_row = cursor.fetchone()

        if not user_row:
            return False, "NOT_ACTIVATED", 0

        user_data = dict(user_row)
        if user_data.get("is_active") != 1:
            return False, "DEACTIVATED", 0

        # فحص انتهاء المدة
        raw_exp = user_data.get("expires_at")
        if raw_exp:
            try:
                if isinstance(raw_exp, datetime):
                    exp_date = raw_exp
                else:
                    exp_clean = str(raw_exp).split(".")[0]
                    exp_date = datetime.strptime(exp_clean, "%Y-%m-%d %H:%M:%S")

                if datetime.now() > exp_date:
                    # انتهى الاشتراك
                    sql_expire = "UPDATE activated_users SET is_active = 0 WHERE user_id = ?"
                    cursor.execute(_format_sql(sql_expire, is_pg), (user_id,))
                    return False, "EXPIRED", 0
            except Exception as e:
                logger.error(f"Error checking user expiration: {e}")

        max_c = user_data.get("max_courses") or MAX_COURSES_PER_USER
        return True, "ACTIVE", max_c


def get_user_activation_details(user_id: int) -> Optional[Dict[str, Any]]:
    """جلب تفاصيل تفعيل مستخدم محدد"""
    sql = "SELECT * FROM activated_users WHERE user_id = ?"
    with get_db_cursor() as (cursor, is_pg):
        cursor.execute(_format_sql(sql, is_pg), (user_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_all_keys(filter_status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    """
    جلب المفاتيح مع إمكانية الفلترة:
    filter_status: 'unused', 'used', or None (all)
    """
    with get_db_cursor() as (cursor, is_pg):
        if filter_status == "unused":
            sql = "SELECT * FROM activation_keys WHERE is_used = 0 ORDER BY id DESC LIMIT ?"
        elif filter_status == "used":
            sql = "SELECT * FROM activation_keys WHERE is_used = 1 ORDER BY id DESC LIMIT ?"
        else:
            sql = "SELECT * FROM activation_keys ORDER BY id DESC LIMIT ?"
        cursor.execute(_format_sql(sql, is_pg), (limit,))
        rows = cursor.fetchall()
        return [dict(r) for r in rows]


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


def delete_key(key_code: str) -> bool:
    """حذف مفتاح من النظام"""
    sql = "DELETE FROM activation_keys WHERE UPPER(key_code) = ?"
    with get_db_cursor() as (cursor, is_pg):
        cursor.execute(_format_sql(sql, is_pg), (key_code.strip().upper(),))
        return cursor.rowcount > 0


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
