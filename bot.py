import asyncio
import logging
import html
import os
import sys
import time
import threading
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional, Tuple, Dict, Any, List

# ضمان توافق محارف UTF-8 في موجه أوامر ويندوز
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import telegram.error
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters
)

import config
import database as db
from scraper import YarmoukScraper, CourseCheckResult

# إعداد السجلات (Logging)
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """معالج الأخطاء العام لمنع توقف البوت عند حدوث تعارض أو انقطاع شبكة"""
    if isinstance(context.error, telegram.error.Conflict):
        logger.warning("تنبيه: تم اكتشاف تعارض في الاتصال (Conflict) - جاري التعامل معه تلقائياً...")
        return
    logger.error(f"خطأ غير معالج: {context.error}")


# خادم فحص صحي لدعم الاستضافة السحابية (Render / Cloud)
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("OK - YU Bot Running 24/7".encode("utf-8"))

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()

    def log_message(self, format, *args):
        pass


def start_health_server():
    try:
        port = int(os.environ.get("PORT", 10000))
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        logger.info(f"🌐 تم تشغيل خادم الفحص الصحي للسحابة بنجاح على المنفذ {port}")
        server.serve_forever()
    except Exception as e:
        logger.warning(f"Health server note: {e}")


# حالات محادثة إضافة مادة (خطوتان فقط: رقم المادة -> رقم الشعبة)
WAITING_COURSE_NO, WAITING_SECTION_NO = range(2)

# كائن فاحص مواد جامعة اليرموك
scraper = YarmoukScraper()


# ==========================================
# إدارة الصلاحيات ومفاتيح التفعيل (Security)
# ==========================================

def is_owner(user_id: int) -> bool:
    """التحقق مما إذا كان المستخدم هو المالك الأساسي للبوت (Owner)"""
    return user_id == config.OWNER_ID or (bool(config.ADMIN_IDS) and user_id == config.ADMIN_IDS[0])


def is_admin(user_id: int) -> bool:
    """التحقق مما إذا كان المستخدم مالكاً أو مشرفاً معتمداً (Admin)"""
    if is_owner(user_id):
        return True
    if user_id in config.ADMIN_IDS:
        return True
    return user_id in db.get_sub_admins()


def check_user_access(user_id: int) -> Tuple[bool, str, int]:
    """التحقق من تفعيل المستخدم: (is_allowed, status_code, max_courses)"""
    if not config.REQUIRE_ACTIVATION or is_admin(user_id):
        return True, "ACTIVE", config.MAX_COURSES_PER_USER
    return db.is_user_activated(user_id)


def format_remaining_time(expires_at_val: Any) -> str:
    """حساب وتنسيق الوقت المتبقي الحي (Real-time Timer) بدون تاريخ ثابت"""
    if not expires_at_val:
        return "دائم ومفتوح ♾️ (طوال الفصل)"
    try:
        if isinstance(expires_at_val, str):
            clean_str = expires_at_val.strip()
            exp_dt = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    exp_dt = datetime.strptime(clean_str[:19], fmt)
                    break
                except ValueError:
                    continue
            if not exp_dt:
                return "دائم ومفتوح ♾️"
        elif isinstance(expires_at_val, datetime):
            exp_dt = expires_at_val
        else:
            return "دائم ومفتوح ♾️"

        now = datetime.now()
        diff = exp_dt - now
        if diff.total_seconds() <= 0:
            return "❌ منتهي الصلاحية"

        days = diff.days
        hours, remainder = divmod(diff.seconds, 3600)
        minutes, _ = divmod(remainder, 60)

        parts = []
        if days > 0:
            parts.append(f"{days} يوم")
        if hours > 0:
            parts.append(f"{hours} ساعة")
        if minutes > 0 or not parts:
            parts.append(f"{minutes} دقيقة")

        return " و ".join(parts) + " ⏳"
    except Exception:
        return "دائم ومفتوح ♾️"


async def send_to_log_channel(context: ContextTypes.DEFAULT_TYPE, log_text: str) -> None:
    """إرسال تقرير السجلات والمحادثات إلى القناة الخاصة بالأدمن"""
    channel_id = db.get_setting("log_channel_id") or config.LOG_CHANNEL_ID
    if not channel_id:
        return
    try:
        cid = int(channel_id) if str(channel_id).lstrip("-").isdigit() else channel_id
        await context.bot.send_message(
            chat_id=cid,
            text=log_text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.debug(f"Failed to send to log channel {channel_id}: {e}")


async def send_activation_required_message(update: Update) -> None:
    """إرسال رسالة القفل والمطالبة بكود التفعيل"""
    user = update.effective_user
    name = html.escape(user.first_name) if user and user.first_name else "عزيزنا الطالب"
    
    owner_handle, owner_url = config.get_owner_contact_info()

    locked_text = (
        f"👋 أهلاً بك يا <b>{name}</b> في <b>بوت شواغر جامعة اليرموك</b> 🎓\n\n"
        "🔒 <b>عذراً، البوت متاح بنظام الاشتراك ومفاتيح التفعيل (Activation Key)!</b>\n\n"
        "🔑 <b>لتفعيل حسابك والبدء فوراً:</b>\n"
        "أرسل كود التفعيل الخاص بك هنا مباشرة في المحادثة (مثال: <code>YU-XXXX-XXXX-XXXX</code>).\n\n"
        f"🆔 <b>الآيدي الخاص بك (ID):</b> <code>{user.id}</code> (اضغط للنسخ)\n\n"
        f"💬 <i>لشراء أو الحصول على كود تفعيل، يرجى التواصل مع: <b>{owner_handle}</b></i>"
    )

    
    keyboard = []
    if owner_url:
        keyboard.append([InlineKeyboardButton("💬 تواصل مع صاحب البوت للاشتراك", url=owner_url)])
    keyboard.append([InlineKeyboardButton("🔄 تحديث / إعادة المحاولة", callback_data="btn_main_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.answer("🔒 البوت يتطلب مفتاح تفعيل!", show_alert=True)
        try:
            await update.callback_query.edit_message_text(locked_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except Exception:
            await update.callback_query.message.reply_text(locked_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    elif update.message:
        await update.message.reply_text(locked_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)



# ==========================================
# معالجات الأوامر الرئيسية (Command Handlers)
# ==========================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """رسالة الترحيب والشاشة الرئيسية للبوت"""
    user = update.effective_user
    user_id = user.id if user else 0

    # تسجيل الحدث في قناة السجلات الخاصة
    u_name = html.escape(user.full_name) if user else "مجهول"
    u_user = f"@{user.username}" if user and user.username else "بدون يوزر"
    await send_to_log_channel(
        context,
        f"🟢 <b>مستخدم فتح البوت (/start):</b>\n"
        f"👤 <b>الاسم:</b> {u_name}\n"
        f"🔗 <b>اليوزر:</b> {u_user}\n"
        f"🆔 <b>الآيدي:</b> <code>{user_id}</code>"
    )

    # فحص إذا كان الرابط يحتوي على كود تفعيل تلقائي (/start YU-XXXX-XXXX-XXXX)
    if context.args and len(context.args) > 0:
        param = context.args[0].strip()
        if "YU-" in param.upper() or len(param) >= 10:
            await process_activation_key(update, context, user, param)
            return

    # التحقق من صلاحية التفعيل
    is_allowed, status_code, _ = check_user_access(user_id)
    if not is_allowed:
        await send_activation_required_message(update)
        return

    name = html.escape(user.first_name) if user and user.first_name else "طالبنا العزيز"
    if is_owner(user_id):
        role_badge = " 👑 (المالك)"
    elif is_admin(user_id):
        role_badge = " 🛡️ (مشرف)"
    else:
        role_badge = ""
    
    welcome_text = (
        f"👋 أهلاً بك يا <b>{name}</b>{role_badge} في <b>بوت مراقبة شواغر جامعة اليرموك</b> 🎓\n\n"
        "💡 <b>وظيفة البوت:</b>\n"
        "تزويد البوت برقم المادة ورقم الشعبة، وسيقوم بمراقبتها وفحصها باستمرار على مدار الساعة. "
        "وفور قيام أي طالب بسحب المادة أو توفر مقعد شاغر، ستصلك رسالة تنبيه عاجلة فوراً لتسجيلها! ⚡\n\n"
        "👇 <b>اختر من الخيارات التالية للبدء:</b>"
    )

    keyboard = [
        [
            InlineKeyboardButton("➕ إضافة مادة للمراقبة", callback_data="btn_add_course"),
            InlineKeyboardButton("📋 موادي المراقبة", callback_data="btn_list_courses")
        ],
        [
            InlineKeyboardButton("🔍 فحص سريع لشعبة", callback_data="btn_quick_check"),
            InlineKeyboardButton("⚙️ حالة البوت", callback_data="btn_bot_status")
        ],
        [
            InlineKeyboardButton("🌐 رابط نظام التسجيل (SIS)", url=config.YU_PORTAL_URL)
        ]
    ]

    # إضافة زر لوحة تحكم الأدمن إذا كان هو المالك
    if is_admin(user_id):
        keyboard.append([InlineKeyboardButton("👑 لوحة تحكم الأدمن والمفاتيح", callback_data="btn_admin_panel")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(welcome_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except Exception:
            await update.callback_query.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    elif update.message:
        await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """دليل استخدام البوت"""
    user_id = update.effective_user.id
    is_allowed, _, _ = check_user_access(user_id)
    if not is_allowed:
        await send_activation_required_message(update)
        return

    admin_help = ""
    if is_owner(user_id):
        admin_help = (
            "\n\n👑 <b>أوامر مالك البوت (Owner):</b>\n"
            "🔹 <code>/addadmin [User_ID]</code> - إضافة حساب كـ مشرف (Admin)\n"
            "🔹 <code>/deladmin [User_ID]</code> - إزالة مشرف من البوت\n"
            "🔹 <code>/admins</code> - عرض قائمة المشرفين\n"
            "🔹 <code>/genkey</code> - توليد مفتاح VIP جديد\n"
            "🔹 <code>/keys</code> - عرض وحذف المفاتيح\n"
            "🔹 <code>/users</code> - عرض المشتركين المفعّلين\n"
            "🔹 <code>/stats</code> - لوحة التحكم الشاملة\n"
            "🔹 <code>/setlog</code> - إعداد قناة السجلات\n"
        )
    elif is_admin(user_id):
        admin_help = (
            "\n\n🛡️ <b>أوامر المشرف (Admin):</b>\n"
            "🔹 <code>/genkey</code> - توليد مفتاح تفعيل جديد\n"
            "🔹 <code>/keys</code> - عرض المفاتيح وإدارتها\n"
            "🔹 <code>/users</code> - عرض المشتركين المفعّلين\n"
            "🔹 <code>/stats</code> - لوحة الإحصائيات\n"
            "🔹 <code>/admins</code> - عرض قائمة المشرفين\n"
        )

    help_text = (
        "📖 <b>دليل استخدام بوت شواغر اليرموك:</b>\n\n"
        "🔹 <code>/track</code> - لبدء إضافة مادة ومراقبتها خطوة بخطوة.\n"
        "🔹 <code>/list</code> - لعرض كل المواد والشعب التي تراقبها حالياً والتحكم بها.\n"
        "🔹 <code>/check [رقم_المادة] [الشعبة]</code> - فحص فوري وسريع لمرة واحدة.\n"
        "🔹 <code>/status</code> - تفاصيل وسرعة الفحص وعدد المواد المراقبة.\n"
        "🔹 <code>/myid</code> - لمعرفة رقم حسابك (ID) وتفاصيل اشتراكك.\n"
        "🔹 <code>/cancel</code> - إلغاء العملية الحالية والرجوع للقائمة الرئيسية.\n\n"
        "💡 <b>مثال على الفحص السريع:</b>\n"
        "<code>/check CS101 1</code>\n\n"
        f"⚡ <b>تنبيه:</b> البوت يفحص الشعب تلقائياً كل {config.CHECK_INTERVAL_SECONDS} ثوانٍ ويرسل لك إشعاراً صوتياً فور فتح أي مقعد."
        f"{admin_help}"
    )
    keyboard = [[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="btn_main_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(help_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """عرض حالة النظام والإحصائيات"""
    user_id = update.effective_user.id
    is_allowed, _, max_allowed = check_user_access(user_id)
    if not is_allowed:
        await send_activation_required_message(update)
        return

    user_courses = db.get_user_courses(user_id, active_only=True)
    all_active = db.get_all_active_courses()
    user_info = db.get_user_activation_details(user_id)
    
    if is_owner(user_id):
        sub_status = "👑 مالك البوت (دائم ♾️)"
    elif is_admin(user_id):
        sub_status = "🛡️ مشرف البوت (دائم ♾️)"
    elif is_allowed:
        sub_status = format_remaining_time(user_info.get("expires_at") if user_info else None)
    else:
        sub_status = "🔒 غير مفعّل"

    status_text = (
        "📊 <b>حالة نظام المراقبة:</b>\n\n"
        f"⏱️ <b>معدل تكرار الفحص:</b> كل <code>{config.CHECK_INTERVAL_SECONDS}</code> ثانية\n"
        f"🎯 <b>وضع التشغيل:</b> <code>{'تجريبي (Demo Mode)' if config.DEMO_MODE else 'حي مباشر (Live SIS)'}</code>\n"
        f"🔐 <b>حالة اشتراكك:</b> {sub_status}\n"
        f"👤 <b>موادك المراقبة حالياً:</b> <code>{len(user_courses)}/{max_allowed}</code> مادة\n"
        f"🌐 <b>إجمالي الشعب المراقبة في النظام:</b> <code>{len(all_active)}</code> شعبة\n"
        f"🛡️ <b>نظام الحماية من الحظر:</b> مُفعل تلقائياً\n"
    )
    
    keyboard = [[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="btn_main_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(status_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except Exception:
            await update.callback_query.message.reply_text(status_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    elif update.message:
        await update.message.reply_text(status_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)



# =======================================================
# محادثة إضافة مادة جديدة للمراقبة (خطوتان فقط)
# =======================================================

async def start_tracking_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """بدء محادثة إضافة المادة"""
    user_id = update.effective_user.id
    is_allowed, _, max_allowed = check_user_access(user_id)
    if not is_allowed:
        await send_activation_required_message(update)
        return ConversationHandler.END

    context.user_data.clear()
    current_count = db.get_user_course_count(user_id)

    if current_count >= max_allowed:
        msg = f"⚠️ لقد وصلت للحد الأقصى من المواد المراقبة ({max_allowed} مواد). يرجى إيقاف أو حذف مادة من قائمتك أولاً عبر أمر /list."
        if update.callback_query:
            await update.callback_query.answer(msg, show_alert=True)
        else:
            await update.message.reply_text(msg)
        return ConversationHandler.END


    prompt_text = (
        "📝 <b>الخطوة 1 من 2: إدخال رمز أو رقم المساق</b>\n\n"
        "أرسل الآن رمز المادة أو رقمها (مثال: <code>CS 111L</code> أو <code>CS101</code> أو <code>FT200</code> أو <code>101330</code>):\n\n"
        "<i>(يمكنك إرسال /cancel في أي وقت للإلغاء)</i>"
    )

    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(prompt_text, parse_mode=ParseMode.HTML)
        except Exception:
            await update.callback_query.message.reply_text(prompt_text, parse_mode=ParseMode.HTML)
    elif update.message:
        await update.message.reply_text(prompt_text, parse_mode=ParseMode.HTML)

    return WAITING_COURSE_NO


async def receive_course_no(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """استلام رقم/رمز المادة"""
    course_no = update.message.text.strip().upper()
    user = update.effective_user
    if len(course_no) < 2 or len(course_no) > 15:
        await update.message.reply_text(
            "⚠️ <b>رمز المادة غير صالح!</b>\n\n"
            "يرجى إدخال رمز صحيح مثل <code>CS 111L</code> أو <code>CS101</code> أو <code>FT200</code> أو <code>101330</code>:",
            parse_mode=ParseMode.HTML
        )
        return WAITING_COURSE_NO

    context.user_data["course_no"] = course_no

    await update.message.reply_text(
        f"✅ تم حفظ رمز المادة: <b>{html.escape(course_no)}</b>\n\n"
        "📝 <b>الخطوة 2 من 2: رقم الشعبة</b>\n"
        "أرسل الآن <b>رقم الشعبة</b> التي تريد مراقبتها (مثال: <code>1</code> أو <code>2</code> أو <code>4</code>):",
        parse_mode=ParseMode.HTML
    )
    return WAITING_SECTION_NO


async def receive_section_no(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """استلام رقم الشعبة وبدء الفحص والمراقبة فوراً"""
    section_no = update.message.text.strip()

    if not section_no.isdigit():
        await update.message.reply_text(
            "⚠️ رقم الشعبة يجب أن يكون رقماً صحيحاً (مثال: <code>1</code> أو <code>2</code> أو <code>4</code>):",
            parse_mode=ParseMode.HTML
        )
        return WAITING_SECTION_NO

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    course_no = context.user_data.get("course_no", "UNKNOWN")
    user = update.effective_user

    # حفظ أولي في قاعدة البيانات
    saved_course = db.add_tracked_course(
        user_id=user_id,
        chat_id=chat_id,
        course_no=course_no,
        course_name=course_no,
        section_no=section_no
    )

    wait_msg = await update.message.reply_text(
        f"⏳ جاري فحص حالة الشعبة <b>{html.escape(section_no)}</b> للمادة <b>{html.escape(course_no)}</b> في نظام اليرموك...",
        parse_mode=ParseMode.HTML
    )

    # إجراء فحص أولي فوري
    res: CourseCheckResult = await scraper.check_course(course_no, section_no)

    # جلب الاسم الحقيقي للمادة من جدول الجامعة
    real_name = res.course_name if res.course_name else course_no
    db.update_course_status(
        course_id=saved_course["id"],
        capacity=res.capacity,
        registered=res.registered,
        available_seats=res.available_seats,
        last_status=res.raw_status,
        notified=1 if res.is_available else 0,
        course_name=real_name
    )

    # إرسال إشعار لقناة السجلات الخاصة
    u_info = f"{html.escape(user.full_name if user else 'طالب')} (@{user.username if user and user.username else 'بدون'}) [<code>{user_id}</code>]"
    await send_to_log_channel(
        context,
        f"➕ <b>إضافة مادة للمراقبة:</b>\n"
        f"👤 <b>الطالب:</b> {u_info}\n"
        f"📚 <b>المادة:</b> {html.escape(real_name)} (<code>{html.escape(course_no)}</code>)\n"
        f"🔢 <b>الشعبة:</b> <code>{html.escape(section_no)}</code>\n"
        f"🪑 <b>حالة المقاعد:</b> {res.available_seats} شاغر ({res.raw_status})"
    )

    if res.error_message and res.raw_status in ["NOT_FOUND", "SECTION_NOT_FOUND", "ERROR"]:
        msg = (
            "⚠️ <b>تنبيه:</b>\n\n"
            f"{html.escape(res.error_message)}\n\n"
            "💡 <b>تلميح:</b> تأكد من إدخال رمز المادة ورقمها بدقة كما في جدول الجامعة (مثال: <code>CS 111L</code> أو <code>FT 200</code> أو <code>ACC 101</code>)."
        )
    elif res.is_available:
        msg = (
            "🎉 <b>خبر سار! الشعبة متاحة ويوجد مقاعد شاغرة حالياً!</b>\n\n"
            f"📚 <b>المادة:</b> {html.escape(real_name)} (<code>{html.escape(res.course_no)}</code>)\n"
            f"🔢 <b>الشعبة:</b> {html.escape(res.section_no)}\n"
            f"🪑 <b>المقاعد الشاغرة:</b> 🔥 <code>{res.available_seats}</code> مقعد شاغر الآن!\n"
            f"👨‍🏫 <b>المدرس:</b> {html.escape(res.instructor)}\n"
            f"⏰ <b>الموعد:</b> {html.escape(res.schedule_time)}\n"
            f"🏛️ <b>القاعة:</b> {html.escape(res.hall)}\n\n"
            "⚡ ادخل الآن مباشرة وسجل المادة قبل أن تمتلئ!"
        )
    else:
        msg = (
            "🔒 <b>الشعبة ممتلئة حالياً (0 مقاعد شاغرة)</b>\n\n"
            f"📚 <b>المادة:</b> {html.escape(real_name)} (<code>{html.escape(res.course_no)}</code>)\n"
            f"🔢 <b>الشعبة:</b> {html.escape(res.section_no)}\n"
            f"🪑 <b>المقاعد الشاغرة:</b> <code>0</code> (المادة ممتلئة)\n"
            f"👨‍🏫 <b>المدرس:</b> {html.escape(res.instructor)}\n"
            f"⏰ <b>الموعد:</b> {html.escape(res.schedule_time)}\n"
            f"🏛️ <b>القاعة:</b> {html.escape(res.hall)}\n\n"
            "🟢 <b>تم تفعيل المراقبة المستمرة بنجاح!</b>\n"
            f"البوت يقوم الآن بفحص الشعبة كل <code>{config.CHECK_INTERVAL_SECONDS}</code> ثوانٍ في الخلفية، وسيرسل لك إشعاراً صوتياً وتنبيه فور قيام أي طالب بسحب المادة! 🚀"
        )

    keyboard = [
        [
            InlineKeyboardButton("📋 عرض موادي المراقبة", callback_data="btn_list_courses"),
            InlineKeyboardButton("➕ إضافة مادة أخرى", callback_data="btn_add_course")
        ],
        [
            InlineKeyboardButton("🌐 فتح بوابة التسجيل SIS", url=config.YU_PORTAL_URL)
        ],
        [
            InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="btn_main_menu")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await wait_msg.edit_text(msg, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning(f"Error editing wait_msg with HTML: {e}")
        await update.message.reply_text(msg, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

    return ConversationHandler.END


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """إلغاء عملية الإضافة"""
    context.user_data.clear()
    cancel_text = "❌ تم إلغاء العملية والعودة للقائمة الرئيسية."
    keyboard = [[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="btn_main_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(cancel_text, reply_markup=reply_markup)
    return ConversationHandler.END


# ==========================================
# قائمة المواد والتحكم بها (List & Controls)
# ==========================================

async def list_courses_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """عرض قائمة المواد المراقبة للمستخدم مع أزرار التحكم"""
    user_id = update.effective_user.id
    is_allowed, _, _ = check_user_access(user_id)
    if not is_allowed:
        await send_activation_required_message(update)
        return

    courses = db.get_user_courses(user_id)

    if not courses:
        empty_text = (
            "📭 <b>لا توجد لديك أي مواد مراقبة حالياً.</b>\n\n"
            "اضغط على الزر أدناه لإضافة مادتك الأولى وبدء المراقبة:"
        )
        keyboard = [
            [InlineKeyboardButton("➕ إضافة مادة للمراقبة", callback_data="btn_add_course")],
            [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="btn_main_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        if update.callback_query:
            await update.callback_query.answer()
            try:
                await update.callback_query.edit_message_text(empty_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
            except Exception:
                await update.callback_query.message.reply_text(empty_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        elif update.message:
            await update.message.reply_text(empty_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        return

    message_text = "📋 <b>قائمة المواد والشعب المراقبة لديك:</b>\n\n"
    keyboard = []

    for idx, c in enumerate(courses, 1):
        status_icon = "🟢" if c["is_active"] else "⏸️"
        seat_status = f"({c['registered']}/{c['capacity']})" if c['capacity'] > 0 else ""
        avail_badge = f"🔥 متوفر {c['available_seats']} مقعد!" if c['available_seats'] > 0 else "ممتلئة 🔒"
        
        c_name = html.escape(c['course_name']) if c['course_name'] else html.escape(c['course_no'])
        message_text += (
            f"<b>{idx}. {c_name}</b> (<code>{html.escape(c['course_no'])}</code>)\n"
            f"   🔢 شعبة: <code>{html.escape(c['section_no'])}</code> | الحالة: {status_icon} {'مراقبة نشطة' if c['is_active'] else 'متوقفة'}\n"
            f"   🪑 المقاعد: {avail_badge} {seat_status}\n\n"
        )

        toggle_btn = InlineKeyboardButton(
            f"⏸️ إيقاف #{c['section_no']}" if c["is_active"] else f"▶️ تشغيل #{c['section_no']}",
            callback_data=f"toggle_{c['id']}"
        )
        del_btn = InlineKeyboardButton(f"🗑️ حذف {c['course_no']}", callback_data=f"del_{c['id']}")
        check_btn = InlineKeyboardButton(f"🔄 فحص", callback_data=f"check_{c['id']}")
        
        keyboard.append([check_btn, toggle_btn, del_btn])

    keyboard.append([InlineKeyboardButton("➕ إضافة مادة جديدة", callback_data="btn_add_course")])
    keyboard.append([InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="btn_main_menu")] )
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(message_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except Exception:
            await update.callback_query.message.reply_text(message_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    elif update.message:
        await update.message.reply_text(message_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def check_command_direct(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر الفحص الفوري المباشر: يدعم /check FT 200 2 أو /check FT200 2 أو /check CS101 1"""
    user_id = update.effective_user.id
    is_allowed, _, _ = check_user_access(user_id)
    if not is_allowed:
        await send_activation_required_message(update)
        return

    if not context.args or len(context.args) < 1:
        help_msg = (
            "⚠️ <b>صيغة الأمر:</b> <code>/check [رمز_المادة] [رقم_الشعبة]</code>\n\n"
            "💡 <b>أمثلة:</b>\n"
            "• <code>/check FT 200 2</code> (رمز FT، رقم 200، شعبة 2)\n"
            "• <code>/check FT200 2</code>\n"
            "• <code>/check CS101 1</code>"
        )
        await update.message.reply_text(help_msg, parse_mode=ParseMode.HTML)
        return

    if len(context.args) >= 3:
        course_no = f"{context.args[0]} {context.args[1]}".upper()
        section_no = context.args[2]
    elif len(context.args) == 2:
        course_no = context.args[0].upper()
        section_no = context.args[1]
    else:
        await update.message.reply_text("⚠️ يرجى تحديد رقم الشعبة أيضاً، مثال: <code>/check FT 200 2</code>", parse_mode=ParseMode.HTML)
        return

    wait_msg = await update.message.reply_text(
        f"⏳ جاري فحص الشعبة <b>{html.escape(section_no)}</b> للمادة <b>{html.escape(course_no)}</b>...",
        parse_mode=ParseMode.HTML
    )

    res: CourseCheckResult = await scraper.check_course(course_no, section_no)

    user = update.effective_user
    u_info = f"{html.escape(user.full_name if user else 'طالب')} (@{user.username if user and user.username else 'بدون'}) [<code>{user_id}</code>]"
    await send_to_log_channel(
        context,
        f"🔍 <b>فحص سريع لشعبة (/check):</b>\n"
        f"👤 <b>الطالب:</b> {u_info}\n"
        f"📚 <b>المادة:</b> <code>{html.escape(course_no)}</code> - شعبة <code>{html.escape(section_no)}</code>\n"
        f"🪑 <b>النتيجة:</b> {res.available_seats} مقاعد شاغرة ({res.raw_status})"
    )

    if res.error_message and res.raw_status in ["NOT_FOUND", "SECTION_NOT_FOUND", "ERROR"]:
        result_text = (
            "⚠️ <b>تنبيه:</b>\n\n"
            f"{html.escape(res.error_message)}\n\n"
            "💡 <b>مثال صحيح:</b> <code>/check CS 111L 4</code> أو <code>/check FT 200 2</code>"
        )
        keyboard = [[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="btn_main_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await wait_msg.edit_text(result_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        return

    if res.is_available:
        status_msg = f"🟢 <b>متوفر شواغر الآن ({res.available_seats} مقعد)!</b>"
    else:
        status_msg = "🔴 <b>الشعبة ممتلئة حالياً (0 مقاعد).</b>"

    result_text = (
        "📊 <b>نتيجة فحص الشعبة الحقيقية من اليرموك:</b>\n\n"
        f"📚 <b>المادة:</b> {html.escape(res.course_name)} (<code>{html.escape(res.course_no)}</code>)\n"
        f"🔢 <b>الشعبة:</b> {html.escape(res.section_no)}\n"
        f"📌 <b>الحالة:</b> {status_msg}\n"
        f"🪑 <b>المقاعد المتاحة:</b> <code>{res.available_seats}</code> مقعد شاغر\n"
        f"👨‍🏫 <b>المدرس:</b> {html.escape(res.instructor)}\n"
        f"⏰ <b>الموعد:</b> {html.escape(res.schedule_time)}\n"
        f"🏛️ <b>القاعة:</b> {html.escape(res.hall)}\n"
    )
    
    keyboard = [
        [InlineKeyboardButton("➕ مراقبة هذه الشعبة باستمرار", callback_data=f"track_quick_{course_no}_{section_no}")],
        [InlineKeyboardButton("🌐 فتح SIS", url=config.YU_PORTAL_URL)],
        [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="btn_main_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await wait_msg.edit_text(result_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


# ==========================================
# أوامر التفعيل ومعلومات الحساب (Activation)
# ==========================================

async def process_activation_key(update: Update, context: ContextTypes.DEFAULT_TYPE, user, key_input: str) -> None:
    """معالجة والتحقق من كود التفعيل وتطبيقه على حساب المستخدم"""
    user_id = user.id
    username = user.username or ""
    first_name = user.first_name or ""

    success, message, key_info = db.activate_user_with_key(user_id, username, first_name, key_input)

    u_info = f"{html.escape(first_name)} (@{username if username else 'بدون'}) [<code>{user_id}</code>]"
    if success:
        await send_to_log_channel(
            context,
            f"🔑 <b>تفعيل اشتراك ناجح:</b>\n"
            f"👤 <b>الطالب:</b> {u_info}\n"
            f"🎟️ <b>المفتاح:</b> <code>{key_info.get('key_code', key_input)}</code>"
        )
        exp_text = "دائم ومفتوح ♾️ (طوال الفصل)"
        if key_info and key_info.get("expires_at"):
            exp_text = f"ينتهي في: <code>{key_info['expires_at']}</code>"

        max_c_val = key_info.get('max_courses', 10) if key_info else 10
        max_c_text = "غير محدود ♾️ (كافة المواد)" if max_c_val >= 99 else f"{max_c_val} مواد"
        
        congrats_text = (
            "🎉🎉 <b>ألف مبروك! تم تفعيل اشتراكك بنجاح!</b> 🎉🎉\n\n"
            f"🔑 <b>كود التفعيل:</b> <code>{key_info.get('key_code', key_input)}</code>\n"
            f"👤 <b>الحساب المفعّل:</b> {html.escape(first_name)} (<code>{user_id}</code>)\n"
            f"⏳ <b>فترة الصلاحية:</b> <code>{exp_text}</code>\n"
            f"📚 <b>عدد المواد المسموحة:</b> <code>{max_c_text}</code>\n\n"
            "🚀 <b>تم فتح كافة خدمات البوت لك الآن!</b> يمكنك البدء بإضافة موادك لمراقبة المقاعد الشاغرة فوراً:"
        )
        keyboard = [
            [
                InlineKeyboardButton("➕ إضافة مادة للمراقبة", callback_data="btn_add_course"),
                InlineKeyboardButton("📋 موادي المراقبة", callback_data="btn_list_courses")
            ],
            [
                InlineKeyboardButton("🔍 فحص سريع لشعبة", callback_data="btn_quick_check"),
                InlineKeyboardButton("⚙️ حالة البوت", callback_data="btn_bot_status")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        if update.message:
            await update.message.reply_text(congrats_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        elif update.callback_query:
            await update.callback_query.message.reply_text(congrats_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    else:
        owner_handle, owner_url = config.get_owner_contact_info()
        error_msg = (
            f"{message}\n\n"
            f"💡 <i>تأكد من كتابة الكود بشكل صحيح وبنفس الحروف، أو تواصل مع: <b>{owner_handle}</b> للحصول على مفتاح جديد.</i>"
        )
        keyboard = []
        if owner_url:
            keyboard.append([InlineKeyboardButton("💬 تواصل مع صاحب البوت", url=owner_url)])
        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

        if update.message:
            await update.message.reply_text(error_msg, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        elif update.callback_query:
            await update.callback_query.message.reply_text(error_msg, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def my_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """عرض معرف المستخدم وحالة حسابه"""
    user = update.effective_user
    user_id = user.id
    is_owner_user = is_owner(user_id)
    is_admin_user = is_admin(user_id)
    is_act, status_code, max_c = db.is_user_activated(user_id)
    user_info = db.get_user_activation_details(user_id)

    if is_owner_user:
        role_str = "👑 مالك البوت (Owner)"
        time_left_str = "دائم ومفتوح ♾️ (مالك البوت)"
    elif is_admin_user:
        role_str = "🛡️ مشرف البوت (Admin)"
        time_left_str = "دائم ومفتوح ♾️ (مشرف البوت)"
    elif is_act:
        role_str = "🟢 مشترك مفعّل (VIP)"
        time_left_str = format_remaining_time(user_info.get("expires_at") if user_info else None)
    else:
        role_str = "🔒 غير مفعّل"
        time_left_str = "غير مشترك 🔒"

    max_c_text = "غير محدود ♾️" if max_c >= 99 else f"{max_c} مواد"
    msg = (
        "🆔 <b>معلومات حسابك:</b>\n\n"
        f"👤 <b>الاسم:</b> {html.escape(user.full_name)}\n"
        f"🔢 <b>معرف الحساب (User ID):</b> <code>{user_id}</code> (اضغط للنسخ)\n"
        f"🏷️ <b>اسم المستخدم:</b> @{user.username if user.username else 'لا يوجد'}\n"
        f"🛡️ <b>الرتبة / الحالة:</b> {role_str}\n"
        f"⏳ <b>الوقت المتبقي:</b> <code>{time_left_str}</code>\n"
        f"📚 <b>الحد الأقصى للمواد المراقبة:</b> <code>{max_c_text if is_act or is_admin_user else '0 مواد'}</code>\n"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def activate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر تفعيل المفتاح يدوياً: /activate YU-XXXX-XXXX-XXXX"""
    user = update.effective_user
    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "🔑 <b>طريقة التفعيل:</b>\n"
            "اكتب الأمر مع كود التفعيل بالشكل التالي:\n"
            "<code>/activate YU-XXXX-XXXX-XXXX</code>\n\n"
            "أو يمكنك إرسال الكود فقط في المحادثة مباشرة!",
            parse_mode=ParseMode.HTML
        )
        return

    key_input = context.args[0].strip()
    await process_activation_key(update, context, user, key_input)


# ==========================================
# أوامر لوحة تحكم الأدمن (Admin Commands)
# ==========================================

async def admin_genkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر الأدمن لتوليد مفتاح تفعيل جديد: /genkey [days] [max_courses] (الافتراضي: غير محدود وفل بالكامل)"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ هذا الأمر خاص بمالك البوت فقط!")
        return

    duration_days = 0
    max_courses = 999  # افتراضياً غير محدود بالكامل

    if context.args:
        try:
            if len(context.args) >= 1 and context.args[0].isdigit():
                duration_days = int(context.args[0])
            if len(context.args) >= 2 and context.args[1].isdigit():
                max_courses = int(context.args[1])
        except Exception:
            pass

    key_code = db.create_activation_key(duration_days=duration_days, max_courses=max_courses)
    dur_desc = f"{duration_days} يوم" if duration_days > 0 else "دائم وغير محدود ♾️ (طوال الفصل)"
    courses_desc = "غير محدود ♾️ (كافة المواد)" if max_courses >= 99 else f"{max_courses} مواد"

    response_text = (
        "👑 <b>تم توليد مفتاح تفعيل VIP جديد بنجاح!</b>\n\n"
        "📋 <b>كود التفعيل (اضغط عليه للنسخ):</b>\n"
        f"<code>{key_code}</code>\n\n"
        f"⏱️ <b>المدة:</b> <code>{dur_desc}</code>\n"
        f"📚 <b>عدد المواد:</b> <code>{courses_desc}</code>\n"
        "🔒 <b>الصلاحية:</b> يظل شغالاً دائماً ومحفوظاً بقاعدة البيانات حتى يقوم الطالب باستخدامه.\n\n"
        "💬 <b>رسالة جاهزة للإرسال للزبون:</b>\n"
        "➖➖➖➖➖➖➖➖➖➖\n"
        f"أهلاً بك! تم إنشاء اشتراكك المميز في بوت شواغر اليرموك 🎓\n\n"
        f"🔑 كود التفعيل الخاص بك:\n<code>{key_code}</code>\n\n"
        "✨ <b>مميزات الاشتراك:</b>\n"
        f"♾️ <b>المدة:</b> {dur_desc}\n"
        f"📚 <b>المواد:</b> {courses_desc}\n\n"
        "طريقة التفعيل: افتح البوت وأرسل هذا الكود مباشرة لتفعيل حسابك فوراً! ⚡\n"
        "➖➖➖➖➖➖➖➖➖➖"
    )
    keyboard = [
        [InlineKeyboardButton("🗑️ حذف هذا المفتاح فوراً", callback_data=f"delkey_code_{key_code}")],
        [
            InlineKeyboardButton("🔑 توليد مفتاح آخر", callback_data="btn_admin_genkey"),
            InlineKeyboardButton("📋 عرض كل المفاتيح", callback_data="keys_p_1_all")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.message:
        await update.message.reply_text(response_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    elif update.callback_query:
        await update.callback_query.message.reply_text(response_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def admin_genkeys_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """توليد عدة مفاتيح بالجملة: /genkeys <count> [days]"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ هذا الأمر خاص بمالك البوت فقط!")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("⚠️ يرجى تحديد العدد، مثال: <code>/genkeys 5</code> أو <code>/genkeys 5 30</code>", parse_mode=ParseMode.HTML)
        return

    count = min(int(context.args[0]), 20)
    days = int(context.args[1]) if len(context.args) > 1 and context.args[1].isdigit() else 0

    keys = db.create_bulk_activation_keys(count=count, duration_days=days, max_courses=999)
    dur_desc = f"{days} يوم" if days > 0 else "دائم ♾️"

    text = f"👑 <b>تم توليد {len(keys)} مفاتيح VIP جديدة ({dur_desc} - مواد غير محدودة):</b>\n\n"
    for i, k in enumerate(keys, 1):
        text += f"{i}. <code>{k}</code>\n"

    keyboard = [
        [InlineKeyboardButton("📋 الانتقال لإدارة المفاتيح", callback_data="keys_p_1_all")]
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


def build_admin_keys_view(page: int = 1, filter_status: str = "all") -> Tuple[str, InlineKeyboardMarkup]:
    """بناء واجهة قائمة المفاتيح التفاعلية للأدمن مع خيارات الحذف السريع والتقليب والفلترة"""
    db_filter = None if filter_status == "all" else filter_status
    per_page = 4
    keys, total_count, total_pages = db.get_all_keys_paginated(filter_status=db_filter, page=page, per_page=per_page)
    stats = db.get_system_stats()

    filter_title = "الكل 📋"
    if filter_status == "unused":
        filter_title = "المتاحة فقط 🟢"
    elif filter_status == "used":
        filter_title = "المستخدمة فقط 🔴"

    text = (
        "👑 <b>لوحة إدارة ومراقبة المفاتيح:</b>\n\n"
        f"📊 <b>إحصائيات سريعة:</b>\n"
        f"🟢 متاحة للبيع: <code>{stats['unused_keys']}</code> | "
        f"🔴 مستخدمة: <code>{stats['used_keys']}</code> | "
        f"👥 مفعّلين: <code>{stats['active_users']}</code>\n"
        f"📄 <b>الصفحة:</b> <code>{page}</code> من <code>{total_pages}</code> (عرض: <b>{filter_title}</b>)\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
    )

    delete_buttons = []

    if not keys:
        text += "<i>📭 لا توجد مفاتيح في هذا القسم حالياً.</i>\n\n"
    else:
        for idx, k in enumerate(keys, 1):
            is_used = bool(k["is_used"])
            status_icon = "🔴" if is_used else "🟢"
            dur_text = f"{k['duration_days']} يوم" if k.get("duration_days") and k["duration_days"] > 0 else "دائم ♾️"
            max_c = k.get("max_courses", 10)
            max_c_text = "غير محدود ♾️" if max_c >= 99 else f"{max_c} مواد"
            created_date = str(k.get("created_at", ""))[:16]

            text += f"{status_icon} <b>كود:</b> <code>{k['key_code']}</code>\n"
            text += f"   ⏱️ <b>المدة:</b> {dur_text} | 📚 <b>المواد:</b> {max_c_text}\n"

            if is_used:
                u_name = f"@{k['used_by_username']}" if k.get("used_by_username") else (k.get("user_first_name") or "مستخدم")
                time_left = format_remaining_time(k.get("expires_at"))
                text += f"   👤 <b>المستخدم:</b> <b>{html.escape(u_name)}</b> (<code>{k.get('used_by_user_id')}</code>)\n"
                text += f"   ⏳ <b>الوقت المتبقي:</b> <code>{time_left}</code>\n"
            else:
                dur_label = f"{k['duration_days']} يوم" if k.get("duration_days") and k["duration_days"] > 0 else "دائم ♾️"
                text += "   ⏳ <b>الحالة:</b> <i>جاهز ومتاح للاستخدام</i>\n"
                text += f"   ⏱️ <b>مدة الاشتراك:</b> <code>{dur_label} (تبدأ عند التفعيل)</code>\n"

            text += "───────────────────\n"

            # الزر الخاص بحذف هذا المفتاح بكبسة واحدة
            short_code = k['key_code']
            btn_label = f"🗑️ حذف ({short_code})"
            delete_buttons.append([InlineKeyboardButton(btn_label, callback_data=f"delkey_id_{k['id']}_{page}_{filter_status}")])

    # أزرار الفلترة
    all_label = "• الكل 📋 •" if filter_status == "all" else "الكل 📋"
    unused_label = f"• المتاحة ({stats['unused_keys']}) 🟢 •" if filter_status == "unused" else f"المتاحة ({stats['unused_keys']}) 🟢"
    used_label = f"• المستخدمة ({stats['used_keys']}) 🔴 •" if filter_status == "used" else f"المستخدمة ({stats['used_keys']}) 🔴"

    filter_row = [
        InlineKeyboardButton(unused_label, callback_data="keys_p_1_unused"),
        InlineKeyboardButton(used_label, callback_data="keys_p_1_used"),
        InlineKeyboardButton(all_label, callback_data="keys_p_1_all"),
    ]

    # أزرار التقليب
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"keys_p_{page-1}_{filter_status}"))
    nav_row.append(InlineKeyboardButton(f"🔄 {page}/{total_pages}", callback_data=f"keys_p_{page}_{filter_status}"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("التالي ➡️", callback_data=f"keys_p_{page+1}_{filter_status}"))

    # أزرار التحكم
    action_row = [
        InlineKeyboardButton("🔑 توليد مفتاح جديد", callback_data="btn_admin_genkey"),
        InlineKeyboardButton("👑 لوحة الأدمن", callback_data="btn_admin_panel"),
    ]

    keyboard = delete_buttons + [filter_row]
    if nav_row:
        keyboard.append(nav_row)
    keyboard.append(action_row)

    return text, InlineKeyboardMarkup(keyboard)


async def admin_keys_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """عرض قائمة المفاتيح التفاعلية للأدمن"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ هذا الأمر خاص بمالك البوت فقط!")
        return

    filter_type = context.args[0].lower() if context.args else "all"
    if filter_type not in ["unused", "used", "all"]:
        filter_type = "all"

    text, reply_markup = build_admin_keys_view(page=1, filter_status=filter_type)
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except Exception:
            await update.callback_query.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    elif update.message:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def admin_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """عرض قائمة المستخدمين المفعّلين"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ هذا الأمر خاص بمالك البوت فقط!")
        return

    users = db.get_all_activated_users()
    if not users:
        await update.message.reply_text("📭 لا يوجد أي مستخدمين مفعّلين حالياً.", parse_mode=ParseMode.HTML)
        return

    text = f"👥 <b>قائمة المستخدمين المفعّلين ({len(users)}):</b>\n\n"
    for idx, u in enumerate(users[:30], 1):
        status_icon = "🟢" if u["is_active"] else "🔴"
        u_name = f"@{u['username']}" if u["username"] else (u["first_name"] or "مستخدم")
        time_left = format_remaining_time(u.get("expires_at"))
        text += f"{idx}. {status_icon} <b>{html.escape(u_name)}</b> (<code>{u['user_id']}</code>)\n   🔑 <code>{u['key_code']}</code> | ⏳ <b>المتبقي:</b> <code>{time_left}</code>\n\n"

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def admin_revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """إلغاء تفعيل مستخدم: /revoke <user_id>"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ هذا الأمر خاص بمالك البوت فقط!")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("⚠️ يرجى كتابة الـ User ID الخاص بالمستخدم، مثال: <code>/revoke 123456789</code>", parse_mode=ParseMode.HTML)
        return

    target_id = int(context.args[0])
    success = db.revoke_user_activation(target_id)
    if success:
        await update.message.reply_text(f"✅ تم إلغاء تفعيل المستخدم <code>{target_id}</code> وإيقاف مراقبته بنجاح!", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"⚠️ لم يتم العثور على مستخدم مفعّل برقم <code>{target_id}</code>.", parse_mode=ParseMode.HTML)


async def admin_delkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """حذف مفتاح تفعيل من النظام: /delkey <key_code>"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ هذا الأمر خاص بمالك البوت فقط!")
        return

    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "⚠️ <b>طريقة حذف مفتاح:</b>\n"
            "اكتب الأمر مع كود المفتاح بالشكل التالي:\n"
            "<code>/delkey YU-XXXX-XXXX-XXXX</code>",
            parse_mode=ParseMode.HTML
        )
        return

    target_key = context.args[0].strip().upper()
    success = db.delete_key(target_key)
    if success:
        await update.message.reply_text(f"🗑️ تم حذف المفتاح <code>{target_key}</code> من النظام بنجاح!", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"⚠️ لم يتم العثور على المفتاح <code>{target_key}</code> في قاعدة البيانات.", parse_mode=ParseMode.HTML)



async def get_user_display_info(bot, user_id: int) -> Tuple[str, str]:
    """جلب الاسم الحقيقي واليوزر للمستخدم من تيليجرام أو قاعدة البيانات"""
    name = ""
    username = ""

    # 1. فحص قاعدة البيانات إذا كان المستخدم مسجلاً
    user_info = db.get_user_activation_details(user_id)
    if user_info:
        name = user_info.get("first_name", "")
        username = user_info.get("username", "")

    # 2. محاولة جلبه من تيليجرام API مباشرة
    try:
        chat = await bot.get_chat(user_id)
        if chat:
            tg_name = chat.full_name or chat.first_name
            if tg_name:
                name = tg_name
            if chat.username:
                username = chat.username
    except Exception:
        pass

    if not name:
        name = f"مستخدم {user_id}"

    return name, username


def build_admin_admins_view(caller_user_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    """بناء واجهة عرض وإدارة المشرفين (Admins) مع الأسماء واليوزرات"""
    admins_data = db.get_sub_admins_detailed()
    owner_id = config.OWNER_ID

    text = (
        "🛡️ <b>لوحة مسؤولي ومشرفي البوت (Admins):</b>\n\n"
        f"👑 <b>المالك الأساسي (Owner):</b>\n"
        f"• <code>{owner_id}</code> (صلاحيات كاملة ⚡)\n\n"
    )

    if admins_data:
        text += f"🛡️ <b>المشرفين المعتمدين ({len(admins_data)}):</b>\n"
        for idx, adm in enumerate(admins_data, 1):
            adm_id = adm.get("user_id", "")
            adm_name = adm.get("name") or f"مشرف {adm_id}"
            user_part = f" (@{adm['username']})" if adm.get("username") else ""
            text += f"{idx}. 👤 <b>{html.escape(adm_name)}</b>{user_part}\n   🆔 <code>{adm_id}</code>\n\n"
        text += (
            "✨ <b>صلاحيات المشرف:</b>\n"
            "• الدخول للوحة التحكم (<code>/admin</code> أو <code>/stats</code>)\n"
            "• توليد مفاتيح التفعيل وحذفها (<code>/genkey</code> و <code>/keys</code>)\n"
            "• فحص قائمة المشتركين (<code>/users</code>)\n"
            "• إلغاء تفعيل اشتراك مستخدم (<code>/revoke</code>)\n"
            "• استخدام كافة مميزات البوت مجاناً.\n"
        )
    else:
        text += (
            "📭 <i>لا يوجد أي مشرفين إضافيين حالياً (المالك فقط).</i>\n\n"
            "➕ <b>لإضافة حساب كـ مشرف (Admin):</b>\n"
            "أرسل الأمر مع الآيدي بالشكل التالي:\n"
            "<code>/addadmin 123456789</code>\n\n"
            "💡 <i>المشرف يستطيع توليد المفاتيح والتحكم بالمشتركين دون الوصول لإعدادات القناة أو إدارة المشرفين.</i>\n"
        )

    keyboard = []
    if is_owner(caller_user_id):
        for adm in admins_data:
            adm_id = adm.get("user_id")
            adm_name = adm.get("name") or str(adm_id)
            keyboard.append([
                InlineKeyboardButton(f"🗑️ إزالة المشرف: {adm_name}", callback_data=f"deladmin_id_{adm_id}")
            ])
        keyboard.append([
            InlineKeyboardButton("➕ إضافة مشرف جديد (/addadmin)", callback_data="btn_addadmin_info")
        ])

    keyboard.append([
        InlineKeyboardButton("🔙 لوحة التحكم", callback_data="btn_admin_panel"),
        InlineKeyboardButton("🏠 الرئيسية", callback_data="btn_main_menu")
    ])

    return text, InlineKeyboardMarkup(keyboard)


async def admin_admins_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """عرض قائمة المشرفين والمالك: /admins"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ هذا القسم خاص بالمشرفين ومالك البوت فقط!")
        return

    # تحديث تلقائي لأسماء المشرفين إن لم تكن مسجلة
    for adm in db.get_sub_admins_detailed():
        if not adm.get("username") or adm.get("name", "").startswith("مشرف "):
            name, username = await get_user_display_info(context.bot, adm["user_id"])
            if name != f"مستخدم {adm['user_id']}":
                db.add_sub_admin(adm["user_id"], name=name, username=username)

    text, reply_markup = build_admin_admins_view(user_id)
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except Exception:
            await update.callback_query.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    elif update.message:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def admin_addadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """إضافة مشرف جديد (خاص بالمالك): /addadmin <user_id>"""
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("⛔ هذا الأمر خاص بمالك البوت الأساسي (Owner) فقط!")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "⚠️ <b>طريقة إضافة مشرف:</b>\n"
            "اكتب الأمر مع الآيدي الخاص بالشخص:\n"
            "<code>/addadmin 123456789</code>\n\n"
            "💡 <i>(يمكن للشخص معرفة الآيدي الخاص به عن طريق إرسال /myid للبوت).</i>",
            parse_mode=ParseMode.HTML
        )
        return

    target_id = int(context.args[0])
    if target_id == config.OWNER_ID:
        await update.message.reply_text("👑 هذا الحساب هو مالك البوت الأساسي بالفعل!", parse_mode=ParseMode.HTML)
        return

    # جلب الاسم واليوزر من تيليجرام
    name, username = await get_user_display_info(context.bot, target_id)
    db.add_sub_admin(target_id, name=name, username=username)
    
    await send_to_log_channel(
        context,
        f"🛡️ <b>تعيين مشرف جديد (Admin):</b>\n"
        f"👤 <b>الاسم:</b> {html.escape(name)}\n"
        f"🔗 <b>اليوزر:</b> @{username if username else 'بدون'}\n"
        f"🆔 <b>الآيدي:</b> <code>{target_id}</code>\n"
        f"👑 <b>بواسطة المالك:</b> <code>{user_id}</code>"
    )

    user_tag = f"<b>{html.escape(name)}</b>" + (f" (@{html.escape(username)})" if username else "")

    keyboard = [
        [InlineKeyboardButton(f"🗑️ إزالة المشرف ({name}) فوراً", callback_data=f"deladmin_id_{target_id}")],
        [
            InlineKeyboardButton("🛡️ عرض قائمة المشرفين", callback_data="btn_admin_manage_admins"),
            InlineKeyboardButton("👑 لوحة تحكم الأدمن", callback_data="btn_admin_panel")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"✅ <b>تم تعيين {user_tag} كـ مشرف (Admin) في البوت بنجاح! 🛡️</b>\n\n"
        f"🔢 <b>معرف الحساب:</b> <code>{target_id}</code>\n\n"
        "✨ <b>الصلاحيات الممنوحة له:</b>\n"
        "• الدخول إلى لوحة التحكم (<code>/admin</code>)\n"
        "• توليد مفاتيح تفعيل جديدة (<code>/genkey</code>)\n"
        "• عرض وحذف المفاتيح (<code>/keys</code>)\n"
        "• عرض قائمة المشتركين (<code>/users</code>)\n"
        "• استخدام البوت بكافة ميزاته مجاناً.\n\n"
        "🔒 <i>(لا يستطيع تغيير إعدادات السجلات أو إضافة مشرفين آخرين).</i>",
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML
    )


async def admin_deladmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """إزالة مشرف (خاص بالمالك): /deladmin <user_id>"""
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("⛔ هذا الأمر خاص بمالك البوت الأساسي (Owner) فقط!")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "⚠️ <b>طريقة إزالة مشرف:</b>\n"
            "اكتب الأمر مع الآيدي الخاص بالمشرف:\n"
            "<code>/deladmin 123456789</code>",
            parse_mode=ParseMode.HTML
        )
        return

    target_id = int(context.args[0])
    success = db.remove_sub_admin(target_id)
    if success:
        await send_to_log_channel(
            context,
            f"🗑️ <b>إزالة مشرف (Admin):</b>\n"
            f"👤 <b>الآيدي:</b> <code>{target_id}</code>\n"
            f"👑 <b>بواسطة المالك:</b> <code>{user_id}</code>"
        )
        await update.message.reply_text(f"🗑️ تم إزالة المشرف <code>{target_id}</code> من النظام بنجاح!", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"⚠️ لم يتم العثور على مشرف بالآيدي <code>{target_id}</code> في قائمة المشرفين.", parse_mode=ParseMode.HTML)


async def admin_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """عرض إحصائيات النظام الشاملة للأدمن والمالك"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ هذا الأمر خاص بمسؤولي البوت فقط!")
        return

    stats = db.get_system_stats()
    log_ch = db.get_setting("log_channel_id") or config.LOG_CHANNEL_ID or "غير معينة"
    sub_admins_count = len(db.get_sub_admins())

    is_owner_user = is_owner(user_id)
    panel_title = "👑 <b>لوحة تحكم مالك البوت (Owner):</b>" if is_owner_user else "🛡️ <b>لوحة تحكم مشرف البوت (Admin):</b>"

    text = (
        f"{panel_title}\n\n"
        f"👥 <b>المستخدمين المشتركين:</b> <code>{stats['active_users']}</code> مستخدم\n"
        f"🟢 <b>المفاتيح المتاحة للبيع:</b> <code>{stats['unused_keys']}</code> مفتاح\n"
        f"🔴 <b>المفاتيح المستخدمة:</b> <code>{stats['used_keys']}</code> مفتاح\n"
        f"🔑 <b>إجمالي المفاتيح:</b> <code>{stats['total_keys']}</code> مفتاح\n"
        f"📚 <b>إجمالي الشعب المراقبة حالياً:</b> <code>{stats['active_courses']}</code> شعبة\n"
    )
    if is_owner_user:
        text += (
            f"🛡️ <b>عدد المشرفين (Admins):</b> <code>{sub_admins_count}</code> مشرف\n"
            f"📢 <b>قناة السجلات الخاصة:</b> <code>{log_ch}</code>\n"
        )
    text += f"⏱️ <b>معدل الفحص الدوري:</b> كل <code>{config.CHECK_INTERVAL_SECONDS}</code> ثوانٍ\n"

    keyboard = [
        [
            InlineKeyboardButton("🔑 توليد مفتاح جديد", callback_data="btn_admin_genkey"),
            InlineKeyboardButton("📋 عرض المفاتيح", callback_data="btn_admin_keys")
        ],
        [
            InlineKeyboardButton("👥 قائمة المشتركين", callback_data="btn_admin_users"),
            InlineKeyboardButton("🛡️ المشرفين (Admins)", callback_data="btn_admin_manage_admins")
        ]
    ]

    if is_owner_user:
        keyboard.append([
            InlineKeyboardButton("📢 إعداد قناة السجلات", callback_data="btn_setlog_menu"),
            InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="btn_main_menu")
        ])
    else:
        keyboard.append([
            InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="btn_main_menu")
        ])

    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except Exception:
            await update.callback_query.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    elif update.message:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def admin_setlog_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر تعيين قناة السجلات الخاصة: /setlog [channel_id] أو إرسال الأمر مباشرة في القناة"""
    user_id = update.effective_user.id if update.effective_user else 0
    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type

    # إذا تم إرسال الأمر داخل قناة أو قروب
    if chat_type in ["channel", "group", "supergroup"]:
        db.set_setting("log_channel_id", str(chat_id))
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"✅ <b>تم تعيين هذه القناة كـ قناة سجلات خاصة بالبوت بنجاح!</b>\n\n"
                    f"🆔 معرف القناة (Log Channel ID): <code>{chat_id}</code>\n"
                    "⚡ سيقوم البوت بإرسال كافة محادثات وسجلات المستخدمين وتنبيهات الشواغر هنا فورياً."
                ),
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"Error sending confirmation in channel: {e}")
        return

    if not is_owner(user_id):
        await update.message.reply_text("⛔ هذا الأمر خاص بمالك البوت الأساسي (Owner) فقط!")
        return

    if context.args and len(context.args) > 0:
        target_ch = context.args[0].strip()
        if target_ch.lower() == "me":
            target_ch = str(user_id)
        db.set_setting("log_channel_id", target_ch)
        await update.message.reply_text(
            f"✅ <b>تم تعيين وجهة السجلات بنجاح!</b>\n\n"
            f"🆔 المعرف: <code>{target_ch}</code>\n"
            "⚡ سيقوم البوت بإرسال كافة محادثات الطلاب والأنشطة والتنبيهات فورياً إلى هذه الوجهة.",
            parse_mode=ParseMode.HTML
        )
    else:
        current_ch = db.get_setting("log_channel_id") or config.LOG_CHANNEL_ID or "غير محددة بعد"
        
        keyboard = [
            [
                InlineKeyboardButton(
                    "📢 إضافة البوت لقناتك كمشرف (بضغطة واحدة)",
                    url="https://t.me/yu_c0urses_bot?startchannel=botadmin&admin=post_messages+edit_messages+delete_messages"
                )
            ],
            [
                InlineKeyboardButton(
                    "👥 إضافة البوت لقروبك كمشرف (بضغطة واحدة)",
                    url="https://t.me/yu_c0urses_bot?startgroup=botadmin&admin=post_messages+edit_messages+delete_messages"
                )
            ],
            [
                InlineKeyboardButton("📥 استلام السجلات هنا في الخاص مباشرة", callback_data="btn_setlog_me")
            ],
            [
                InlineKeyboardButton("🗑️ إلغاء ربط السجلات", callback_data="btn_unsetlog")
            ],
            [
                InlineKeyboardButton("🔙 لوحة التحكم", callback_data="btn_admin_panel")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "📋 <b>لوحة إعداد قناة/مجموعة السجلات (Log Channel):</b>\n\n"
            f"📌 <b>الجهة المربوطة حالياً:</b> <code>{current_ch}</code>\n\n"
            "⚡ <b>اختر الطريقة الأنسب لك لاستلام سجلات ومحادثات الطلاب:</b>\n"
            "1️⃣ <b>زر القناة أعلاه:</b> سيقوم بإضافة البوت لقناتك فوراً مع كافة الصلاحيات بدون أخطاء.\n"
            "2️⃣ <b>زر القروب أعلاه:</b> لإنشاء قروب خاص بك والبوت بضغطة زر.\n"
            "3️⃣ <b>في الخاص مباشرة:</b> إذا أردت أن تصلك محادثات وسجلات الطلاب هنا في المحادثة الخاصة مع البوت مباشرة!",
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )


async def admin_unsetlog_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """إلغاء ربط قناة السجلات: /unsetlog"""
    user_id = update.effective_user.id if update.effective_user else 0
    if not is_owner(user_id):
        await update.message.reply_text("⛔ هذا الأمر خاص بمالك البوت الأساسي (Owner) فقط!")
        return

    db.set_setting("log_channel_id", "")
    await update.message.reply_text("🗑️ تم إلغاء ربط قناة السجلات بنجاح.", parse_mode=ParseMode.HTML)


async def handle_general_text_and_activation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """معالجة الرسائل النصية المباشرة وتسجيلها في قناة السجلات"""
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    user = update.effective_user
    user_id = user.id

    # إرسال نسخة من محادثة ورسالة الطالب لقناة السجلات الخاصة بالأدمن
    u_info = f"{html.escape(user.full_name if user else 'طالب')} (@{user.username if user and user.username else 'بدون'}) [<code>{user_id}</code>]"
    await send_to_log_channel(
        context,
        f"💬 <b>رسالة نصية واردة:</b>\n"
        f"👤 <b>المرسل:</b> {u_info}\n"
        f"📝 <b>الرسالة:</b> <i>{html.escape(text)}</i>"
    )

    # إذا كان المستخدم مفعلاً بالفعل، لا داعي لمعالجة التفعيل
    is_act, _, _ = check_user_access(user_id)
    if is_act:
        return

    # المستخدم غير مفعّل، نفحص إذا كان النص المدخل هو كود تفعيل
    await process_activation_key(update, context, user, text)


async def handle_general_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """إعادة توجيه أي وسائط (صور، فويس، ملفات) يرسلها الطلاب إلى قناة السجلات"""
    if not update.message:
        return
    user = update.effective_user
    user_id = user.id if user else 0
    u_info = f"{html.escape(user.full_name if user else 'طالب')} (@{user.username if user and user.username else 'بدون'}) [<code>{user_id}</code>]"
    
    channel_id = db.get_setting("log_channel_id") or config.LOG_CHANNEL_ID
    if not channel_id or str(update.effective_chat.id) == str(channel_id):
        return
    
    try:
        cid = int(channel_id) if str(channel_id).lstrip("-").isdigit() else channel_id
        await send_to_log_channel(context, f"📎 <b>وسائط/ملف وارد من طالب:</b>\n👤 <b>المرسل:</b> {u_info}")
        await context.bot.forward_message(
            chat_id=cid,
            from_chat_id=update.effective_chat.id,
            message_id=update.message.message_id
        )
    except Exception as e:
        logger.error(f"Error forwarding media to log channel: {e}")


# ==========================================
# معالج ضغطات الأزرار (Callback Query Handler)
# ==========================================

async def callback_query_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """توجيه التفاعلات مع الأزرار"""
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id

    is_allowed, _, _ = check_user_access(user_id)
    if not is_allowed and not data.startswith("btn_activate"):
        await send_activation_required_message(update)
        return

    if data == "btn_main_menu":
        await start_command(update, context)

    elif data == "btn_add_course":
        await start_tracking_conversation(update, context)

    elif data == "btn_list_courses":
        await list_courses_handler(update, context)

    elif data == "btn_bot_status":
        await status_command(update, context)

    elif data == "btn_admin_panel" or data == "btn_admin_stats":
        if is_admin(user_id):
            await admin_stats_command(update, context)
        else:
            await query.answer("⛔ هذا القسم خاص بالمالك فقط!", show_alert=True)

    elif data == "btn_admin_genkey":
        if is_admin(user_id):
            key_code = db.create_activation_key(duration_days=0, max_courses=999)
            await query.answer("✅ تم توليد مفتاح VIP غير محدود بنجاح!")
            dur_desc = "دائم وغير محدود ♾️ (طوال الفصل)"
            courses_desc = "غير محدود ♾️ (كافة المواد)"
            resp_msg = (
                "👑 <b>تم توليد مفتاح تفعيل VIP جديد بنجاح!</b>\n\n"
                "📋 <b>كود التفعيل (اضغط عليه للنسخ):</b>\n"
                f"<code>{key_code}</code>\n\n"
                f"⏱️ <b>المدة:</b> <code>{dur_desc}</code>\n"
                f"📚 <b>عدد المواد:</b> <code>{courses_desc}</code>\n"
                "🔒 <b>الصلاحية:</b> يظل شغالاً دائماً ومحفوظاً بقاعدة البيانات حتى يقوم الطالب باستخدامه.\n\n"
                "💬 <b>رسالة جاهزة للإرسال للزبون:</b>\n"
                "➖➖➖➖➖➖➖➖➖➖\n"
                f"أهلاً بك! تم إنشاء اشتراكك المميز في بوت شواغر اليرموك 🎓\n\n"
                f"🔑 كود التفعيل الخاص بك:\n<code>{key_code}</code>\n\n"
                "✨ <b>مميزات الاشتراك:</b>\n"
                f"♾️ <b>المدة:</b> {dur_desc}\n"
                f"📚 <b>المواد:</b> {courses_desc}\n\n"
                "طريقة التفعيل: افتح البوت وأرسل هذا الكود مباشرة لتفعيل حسابك فوراً! ⚡\n"
                "➖➖➖➖➖➖➖➖➖➖"
            )
            keyboard = [
                [InlineKeyboardButton("🗑️ حذف هذا المفتاح فوراً", callback_data=f"delkey_code_{key_code}")],
                [
                    InlineKeyboardButton("🔑 توليد مفتاح آخر", callback_data="btn_admin_genkey"),
                    InlineKeyboardButton("📋 عرض كل المفاتيح", callback_data="keys_p_1_all")
                ]
            ]
            await query.message.reply_text(resp_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        else:
            await query.answer("⛔ هذا القسم خاص بالمالك فقط!", show_alert=True)

    elif data == "btn_admin_keys" or data.startswith("keys_p_"):
        if is_admin(user_id):
            await query.answer()
            if data == "btn_admin_keys":
                page, filter_status = 1, "all"
            else:
                parts = data.split("_")
                page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
                filter_status = parts[3] if len(parts) > 3 else "all"
            text, reply_markup = build_admin_keys_view(page=page, filter_status=filter_status)
            try:
                await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
            except Exception:
                await query.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        else:
            await query.answer("⛔ هذا القسم خاص بالمالك فقط!", show_alert=True)

    elif data.startswith("delkey_id_"):
        if not is_admin(user_id):
            await query.answer("⛔ خاص بمالك البوت فقط!", show_alert=True)
            return
        parts = data.split("_")
        key_id = int(parts[2])
        page = int(parts[3])
        filter_status = parts[4]

        key_obj = db.get_key_by_id(key_id)
        key_code_str = key_obj.get("key_code", "") if key_obj else str(key_id)
        success = db.delete_key_by_id(key_id)
        if success:
            await query.answer(f"🗑️ تم حذف المفتاح {key_code_str} بنجاح!", show_alert=True)
        else:
            await query.answer("⚠️ لم يتم العثور على المفتاح!", show_alert=True)

        text, reply_markup = build_admin_keys_view(page=page, filter_status=filter_status)
        try:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    elif data.startswith("delkey_code_"):
        if not is_admin(user_id):
            await query.answer("⛔ خاص بمالك البوت فقط!", show_alert=True)
            return
        key_code_target = data.replace("delkey_code_", "").strip()
        success = db.delete_key(key_code_target)
        if success:
            await query.answer(f"🗑️ تم حذف المفتاح {key_code_target} بنجاح!", show_alert=True)
            try:
                await query.edit_message_text(f"🗑️ <b>تم حذف المفتاح بنجاح:</b>\n<code>{key_code_target}</code>", parse_mode=ParseMode.HTML)
            except Exception:
                pass
        else:
            await query.answer("⚠️ المفتاح محذوف بالفعل أو غير موجود!", show_alert=True)

    elif data == "btn_admin_users":
        if is_admin(user_id):
            await admin_users_command(update, context)
        else:
            await query.answer("⛔ هذا القسم خاص بمسؤولي البوت فقط!", show_alert=True)

    elif data == "btn_admin_manage_admins":
        if is_admin(user_id):
            await admin_admins_command(update, context)
        else:
            await query.answer("⛔ هذا القسم خاص بالمالك فقط!", show_alert=True)

    elif data.startswith("deladmin_id_"):
        if not is_owner(user_id):
            await query.answer("⛔ هذا الإجراء خاص بمالك البوت الأساسي فقط!", show_alert=True)
            return
        target_adm_id = int(data.replace("deladmin_id_", "").strip())
        
        # جلب الاسم قبل الحذف لعرضه للمالك
        admins_data = db.get_sub_admins_detailed()
        target_obj = next((x for x in admins_data if int(x.get("user_id", 0)) == target_adm_id), None)
        target_name = target_obj.get("name", str(target_adm_id)) if target_obj else str(target_adm_id)
        
        success = db.remove_sub_admin(target_adm_id)
        if success:
            await query.answer(f"🗑️ تم إزالة المشرف {target_name} بنجاح!", show_alert=True)
            await send_to_log_channel(
                context,
                f"🗑️ <b>إزالة مشرف (Admin):</b>\n👤 <b>الاسم:</b> {html.escape(target_name)}\n🔢 <b>الآيدي:</b> <code>{target_adm_id}</code>\n👑 <b>بواسطة المالك:</b> <code>{user_id}</code>"
            )
            
            # إذا كان الضغط من داخل رسالة التعيين المباشرة
            if query.message and "تم تعيين" in query.message.text:
                del_text = (
                    f"🗑️ <b>تمت إزالة المشرف {html.escape(target_name)} (<code>{target_adm_id}</code>) من قائمة المشرفين بنجاح!</b>\n\n"
                    "⚡ يمكنك إعادة تعيينه مشرفاً في أي وقت بضغطة زر واحدة أدناه:"
                )
                readd_kb = [
                    [InlineKeyboardButton(f"➕ إعادة تعيين {target_name} كمشرف", callback_data=f"readdadmin_id_{target_adm_id}")],
                    [InlineKeyboardButton("🛡️ عرض قائمة المشرفين", callback_data="btn_admin_manage_admins")],
                    [InlineKeyboardButton("👑 لوحة تحكم الأدمن", callback_data="btn_admin_panel")]
                ]
                try:
                    await query.edit_message_text(del_text, reply_markup=InlineKeyboardMarkup(readd_kb), parse_mode=ParseMode.HTML)
                    return
                except Exception:
                    pass
        else:
            await query.answer("⚠️ المشرف غير موجود أو تمت إزالته بالفعل!", show_alert=True)

        text, reply_markup = build_admin_admins_view(user_id)
        try:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    elif data.startswith("readdadmin_id_"):
        if not is_owner(user_id):
            await query.answer("⛔ خاص بمالك البوت الأساسي فقط!", show_alert=True)
            return
        target_adm_id = int(data.replace("readdadmin_id_", "").strip())
        name, username = await get_user_display_info(context.bot, target_adm_id)
        db.add_sub_admin(target_adm_id, name=name, username=username)
        await query.answer(f"✅ تم إعادة تعيين {name} كمشرف بنجاح!", show_alert=True)

        user_tag = f"<b>{html.escape(name)}</b>" + (f" (@{html.escape(username)})" if username else "")
        resp_text = (
            f"✅ <b>تم تعيين {user_tag} كـ مشرف (Admin) في البوت بنجاح! 🛡️</b>\n\n"
            f"🔢 <b>معرف الحساب:</b> <code>{target_adm_id}</code>\n\n"
            "✨ <b>الصلاحيات الممنوحة له:</b>\n"
            "• الدخول إلى لوحة التحكم (<code>/admin</code>)\n"
            "• توليد مفاتيح تفعيل جديدة (<code>/genkey</code>)\n"
            "• عرض وحذف المفاتيح (<code>/keys</code>)\n"
            "• عرض قائمة المشتركين (<code>/users</code>)\n"
            "• استخدام البوت بكافة ميزاته مجاناً.\n\n"
            "🔒 <i>(لا يستطيع تغيير إعدادات السجلات أو إضافة مشرفين آخرين).</i>"
        )
        keyboard = [
            [InlineKeyboardButton(f"🗑️ إزالة المشرف ({name}) فوراً", callback_data=f"deladmin_id_{target_adm_id}")],
            [
                InlineKeyboardButton("🛡️ عرض قائمة المشرفين", callback_data="btn_admin_manage_admins"),
                InlineKeyboardButton("👑 لوحة تحكم الأدمن", callback_data="btn_admin_panel")
            ]
        ]
        try:
            await query.edit_message_text(resp_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        except Exception:
            pass

    elif data == "btn_addadmin_info":
        if not is_owner(user_id):
            await query.answer("⛔ خاص بمالك البوت فقط!", show_alert=True)
            return
        await query.answer()
        info_text = (
            "➕ <b>طريقة إضافة حساب كـ مشرف (Admin):</b>\n\n"
            "1️⃣ اطلب من الحساب فتح البوت وإرسال أمر /myid لمعرفة الآيدي الخاص به.\n"
            "2️⃣ أرسل الأمر التالي في المحادثة:\n"
            "<code>/addadmin [User_ID]</code>\n\n"
            "💡 <b>مثال:</b> <code>/addadmin 7566322988</code>\n\n"
            "⚡ فور إضافته، سيتمكن الحساب من فتح لوحة التحكم وتوليد المفاتيح ورؤية المشتركين فوراً!"
        )
        keyboard = [
            [InlineKeyboardButton("🛡️ عرض المشرفين", callback_data="btn_admin_manage_admins")],
            [InlineKeyboardButton("👑 لوحة التحكم", callback_data="btn_admin_panel")]
        ]
        await query.message.reply_text(info_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

    elif data == "btn_setlog_menu":
        if not is_owner(user_id):
            await query.answer("⛔ خاص بمالك البوت فقط!", show_alert=True)
            return
        await admin_setlog_command(update, context)

    elif data == "btn_setlog_me":
        if is_owner(user_id):
            db.set_setting("log_channel_id", str(user_id))
            await query.answer("✅ تم التعيين بنجاح!")
            await query.message.reply_text(
                f"✅ <b>تم تفعيل استلام السجلات في محادثتك الخاصة مباشرة!</b>\n\n"
                f"🆔 المعرف (Your ID): <code>{user_id}</code>\n"
                "⚡ ستصلك الآن كافة محادثات وسجلات الطلاب وتنبيهات الشواغر هنا في الخاص.",
                parse_mode=ParseMode.HTML
            )
        else:
            await query.answer("⛔ خاص بمالك البوت الأساسي فقط!", show_alert=True)

    elif data == "btn_unsetlog":
        if is_owner(user_id):
            db.set_setting("log_channel_id", "")
            await query.answer("🗑️ تم إلغاء الربط!")
            await query.message.reply_text("🗑️ تم إلغاء ربط قناة/وجهة السجلات بنجاح.", parse_mode=ParseMode.HTML)
        else:
            await query.answer("⛔ خاص بمالك البوت الأساسي فقط!", show_alert=True)

    elif data == "btn_quick_check":
        await query.answer()
        quick_check_text = (
            "🔍 <b>الفحص السريع لشعبة:</b>\n\n"
            "أرسل أمر الفحص في المحادثة بالشكل التالي:\n"
            "<code>/check CS101 1</code>\n"
            "أو\n"
            "<code>/check FT 200 2</code>\n\n"
            "⚡ سيقوم البوت بفحص الشعبة فوراً وعرض تفاصيل المدرس والقاعة وعدد المقاعد."
        )
        keyboard = [
            [InlineKeyboardButton("➕ إضافة مادة للمراقبة", callback_data="btn_add_course")],
            [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="btn_main_menu")]
        ]
        await query.edit_message_text(quick_check_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

    elif data.startswith("toggle_"):
        course_id = int(data.split("_")[1])
        course = db.get_course_by_id(course_id, user_id)
        if course:
            user = update.effective_user
            u_info = f"{html.escape(user.full_name if user else 'طالب')} (@{user.username if user and user.username else 'بدون'}) [<code>{user_id}</code>]"
            if course["is_active"]:
                db.stop_tracking_course(course_id, user_id)
                await query.answer("⏸️ تم إيقاف مراقبة الشعبة مؤقتاً")
                await send_to_log_channel(
                    context,
                    f"⏸️ <b>إيقاف مراقبة مؤقت:</b>\n"
                    f"👤 <b>الطالب:</b> {u_info}\n"
                    f"📚 <b>المادة:</b> {course['course_no']} - شعبة {course['section_no']}"
                )
            else:
                db.update_course_status(course_id, course["capacity"], course["registered"], course["available_seats"], course["last_status"], notified=0)
                db.add_tracked_course(user_id, update.effective_chat.id, course["course_no"], course.get("course_name", course["course_no"]), course["section_no"])
                await query.answer("▶️ تم استئناف المراقبة بنجاح!")
                await send_to_log_channel(
                    context,
                    f"▶️ <b>استئناف مراقبة:</b>\n"
                    f"👤 <b>الطالب:</b> {u_info}\n"
                    f"📚 <b>المادة:</b> {course['course_no']} - شعبة {course['section_no']}"
                )
            await list_courses_handler(update, context)

    elif data.startswith("del_"):
        course_id = int(data.split("_")[1])
        course = db.get_course_by_id(course_id, user_id)
        if course:
            user = update.effective_user
            u_info = f"{html.escape(user.full_name if user else 'طالب')} (@{user.username if user and user.username else 'بدون'}) [<code>{user_id}</code>]"
            await send_to_log_channel(
                context,
                f"🗑️ <b>حذف مادة من المراقبة:</b>\n"
                f"👤 <b>الطالب:</b> {u_info}\n"
                f"📚 <b>المادة:</b> {course['course_no']} - شعبة {course['section_no']}"
            )
        db.delete_course(course_id, user_id)
        await query.answer("🗑️ تم حذف المادة من قائمة المراقبة")
        await list_courses_handler(update, context)
        await query.answer("🗑️ تم حذف المادة من قائمة المراقبة")
        await list_courses_handler(update, context)

    elif data.startswith("check_"):
        course_id = int(data.split("_")[1])
        course = db.get_course_by_id(course_id, user_id)
        if course:
            await query.answer("🔄 جاري الفحص...")
            res = await scraper.check_course(course["course_no"], course["section_no"], course["course_name"])
            db.update_course_status(course_id, res.capacity, res.registered, res.available_seats, res.raw_status)
            alert_text = f"المسجلين: {res.registered}/{res.capacity} | الشواغر: {res.available_seats}"
            await query.answer(alert_text, show_alert=True)
            await list_courses_handler(update, context)

    elif data.startswith("track_quick_"):
        parts = data.split("_")
        course_no = parts[2]
        section_no = parts[3]
        db.add_tracked_course(user_id, update.effective_chat.id, course_no, course_no, section_no)
        await query.answer("🟢 تم تفعيل مراقبة الشعبة بنجاح!", show_alert=True)
        await list_courses_handler(update, context)


# ====================================================================
# دورة الفحص التلقائي بالخلفية (Background Repeating Scanner Job)
# ====================================================================

async def background_course_scanner(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    وظيفة تعمل باستمرار في الخلفية كل X ثانية:
    تفحص كافة المواد المراقبة لجميع الطلاب، وترسل تنبيهاً فورياً عند توفر مقعد!
    """
    active_courses = db.get_all_active_courses()
    if not active_courses:
        return

    for c in active_courses:
        try:
            res: CourseCheckResult = await scraper.check_course(
                c["course_no"],
                c["section_no"],
                c["course_name"]
            )

            # الحالة 1: توفر مقاعد شاغرة لأول مرة وإشعار المستخدم
            if res.is_available and c["notified"] == 0:
                logger.info(f"🔥 شاغر متوفر في المادة {c['course_no']} شعبة {c['section_no']} للمستخدم {c['user_id']}")
                
                alert_text = (
                    "🚨🚨 <b>تنبيه عاجل: توفر مقعد شاغر!</b> 🚨🚨\n\n"
                    "يا بطل، تم توفر مقعد الآن في مادتك المراقبة:\n\n"
                    f"📚 <b>المادة:</b> {html.escape(res.course_name)} (<code>{html.escape(res.course_no)}</code>)\n"
                    f"🔢 <b>الشعبة:</b> {html.escape(res.section_no)}\n"
                    f"🪑 <b>المقاعد المتاحة الآن:</b> 🔥 <code>{res.available_seats}</code> مقعد شاغر! (المسجلين: {res.registered}/{res.capacity})\n"
                    f"👨‍🏫 <b>المدرس:</b> {html.escape(res.instructor)}\n"
                    f"⏰ <b>الموعد:</b> {html.escape(res.schedule_time)}\n"
                    f"🏛️ <b>القاعة:</b> {html.escape(res.hall)}\n\n"
                    "⚡ <b>سارع فوراً بالدخول إلى نظام التسجيل (SIS) وسجل المادة قبل أن يأخذها طالب آخر!</b>"
                )

                keyboard = [
                    [InlineKeyboardButton("🌐 الدخول السريع لنظام SIS", url=config.YU_PORTAL_URL)],
                    [InlineKeyboardButton("⏹️ إيقاف مراقبة هذه المادة", callback_data=f"toggle_{c['id']}")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)

                sent_msg = await context.bot.send_message(
                    chat_id=c["chat_id"],
                    text=alert_text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML
                )

                await send_to_log_channel(
                    context,
                    f"🚨 <b>تنبيه مقاعد شاغرة:</b>\n"
                    f"📚 <b>المادة:</b> {html.escape(res.course_name)} (<code>{html.escape(res.course_no)}</code>)\n"
                    f"🔢 <b>الشعبة:</b> {html.escape(res.section_no)}\n"
                    f"🪑 <b>المقاعد الشاغرة:</b> 🔥 <code>{res.available_seats}</code> مقعد!\n"
                    f"👤 <b>المستلم (User ID):</b> <code>{c['user_id']}</code>"
                )

                db.update_course_status(
                    c["id"],
                    res.capacity,
                    res.registered,
                    res.available_seats,
                    res.raw_status,
                    notified=1,
                    course_name=res.course_name or c.get("course_name"),
                    last_alert_msg_id=sent_msg.message_id
                )

            # الحالة 2: الشعبة لا تزال مفتوحة ولكن عدد المقاعد تغير (تحديث فوري Real-time)
            elif res.is_available and c["notified"] == 1:
                if res.available_seats != c.get("available_seats") or res.registered != c.get("registered"):
                    logger.info(f"🔄 تحديث حي لعدد المقاعد للمادة {c['course_no']} شعبة {c['section_no']}: {c.get('available_seats')} -> {res.available_seats}")
                    msg_id = c.get("last_alert_msg_id")
                    if msg_id:
                        try:
                            updated_alert_text = (
                                "🚨🚨 <b>تنبيه عاجل: توفر مقعد شاغر! (تحديث فوري ⚡)</b> 🚨🚨\n\n"
                                "يا بطل، المقاعد المتاحة الآن في مادتك المراقبة:\n\n"
                                f"📚 <b>المادة:</b> {html.escape(res.course_name)} (<code>{html.escape(res.course_no)}</code>)\n"
                                f"🔢 <b>الشعبة:</b> {html.escape(res.section_no)}\n"
                                f"🪑 <b>المقاعد المتاحة الآن:</b> 🔥 <code>{res.available_seats}</code> مقعد شاغر! (المسجلين: {res.registered}/{res.capacity})\n"
                                f"👨‍🏫 <b>المدرس:</b> {html.escape(res.instructor)}\n"
                                f"⏰ <b>الموعد:</b> {html.escape(res.schedule_time)}\n"
                                f"🏛️ <b>القاعة:</b> {html.escape(res.hall)}\n\n"
                                "⚡ <b>سارع فوراً بالدخول إلى نظام التسجيل (SIS) وسجل المادة قبل أن يأخذها طالب آخر!</b>"
                            )
                            keyboard = [
                                [InlineKeyboardButton("🌐 الدخول السريع لنظام SIS", url=config.YU_PORTAL_URL)],
                                [InlineKeyboardButton("⏹️ إيقاف مراقبة هذه المادة", callback_data=f"toggle_{c['id']}")]
                            ]
                            await context.bot.edit_message_text(
                                chat_id=c["chat_id"],
                                message_id=msg_id,
                                text=updated_alert_text,
                                reply_markup=InlineKeyboardMarkup(keyboard),
                                parse_mode=ParseMode.HTML
                            )
                        except Exception as e:
                            logger.debug(f"Could not edit real-time alert message: {e}")

                    db.update_course_status(
                        c["id"],
                        res.capacity,
                        res.registered,
                        res.available_seats,
                        res.raw_status,
                        notified=1,
                        course_name=res.course_name or c.get("course_name")
                    )

            # الحالة 3: كانت الشعبة مفتوحة وتم تنبيه الطالب، والآن امتلأت (تحديث رسالة التنبيه + إشعار بالامتلاء)
            elif not res.is_available and c["notified"] == 1:
                logger.info(f"❌ الشعبة {c['course_no']} شعبة {c['section_no']} أصبحت ممتلئة بعد أن كانت متاحة (للمستخدم {c['user_id']})")
                
                # تحديث رسالة التنبيه السابقة في المحادثة مباشرة إن وجدت
                msg_id = c.get("last_alert_msg_id")
                if msg_id:
                    try:
                        closed_alert_text = (
                            "🔒 <b>انتهت المقاعد! امتلأت هذه الشعبة بالكامل 🏃‍♂️💨</b>\n\n"
                            f"📚 <b>المادة:</b> {html.escape(res.course_name)} (<code>{html.escape(res.course_no)}</code>)\n"
                            f"🔢 <b>الشعبة:</b> {html.escape(res.section_no)}\n"
                            f"🔒 <b>المقاعد المتاحة:</b> <code>0</code> (المادة ممتلئة)\n\n"
                            "🔄 <b>المراقبة مستمرة بدون توقف 🚀</b>\n"
                            f"البوت مكمل فحص للشعبة كل <code>{config.CHECK_INTERVAL_SECONDS}</code> ثوانٍ بالخلفية، وأول ما يتوفر مقعد جديد رح نرجع نبعثلك تنبيه فوراً! ⚡"
                        )
                        keyboard = [
                            [InlineKeyboardButton("🌐 فتح بوابة التسجيل SIS", url=config.YU_PORTAL_URL)],
                            [InlineKeyboardButton("⏹️ إيقاف مراقبة هذه المادة", callback_data=f"toggle_{c['id']}")]
                        ]
                        await context.bot.edit_message_text(
                            chat_id=c["chat_id"],
                            message_id=msg_id,
                            text=closed_alert_text,
                            reply_markup=InlineKeyboardMarkup(keyboard),
                            parse_mode=ParseMode.HTML
                        )
                    except Exception as e:
                        logger.debug(f"Could not edit message on close: {e}")

                missed_text = (
                    "❌ <b>راحت عليك! في حد سبقك وسجل الشعبة 🏃‍♂️💨</b>\n\n"
                    f"📚 <b>المادة:</b> {html.escape(res.course_name)} (<code>{html.escape(res.course_no)}</code>)\n"
                    f"🔢 <b>الشعبة:</b> {html.escape(res.section_no)}\n"
                    f"🔒 <b>الحالة:</b> الشعبة رجعت فل وممتلئة حالياً (0 مقاعد شاغرة).\n\n"
                    "🔄 <b>لا تقلق! المراقبة مستمرة بدون توقف 🚀</b>\n"
                    f"البوت مكمل فحص للشعبة كل <code>{config.CHECK_INTERVAL_SECONDS}</code> ثوانٍ بالخلفية، وأول ما طالب يسحبها أو يتوفر أي مقعد شاغر من جديد رح نرجع نبعثلك مسج فوراً! ⚡"
                )

                keyboard = [
                    [InlineKeyboardButton("🌐 فتح بوابة التسجيل SIS", url=config.YU_PORTAL_URL)],
                    [InlineKeyboardButton("⏹️ إيقاف مراقبة هذه المادة", callback_data=f"toggle_{c['id']}")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)

                await context.bot.send_message(
                    chat_id=c["chat_id"],
                    text=missed_text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML
                )

                await send_to_log_channel(
                    context,
                    f"🔒 <b>امتلاء شعبة ممتلئة مجدداً:</b>\n"
                    f"📚 <b>المادة:</b> {html.escape(res.course_name)} (<code>{html.escape(res.course_no)}</code>)\n"
                    f"🔢 <b>الشعبة:</b> {html.escape(res.section_no)}\n"
                    f"👤 <b>المستخدم (User ID):</b> <code>{c['user_id']}</code>"
                )

                db.update_course_status(
                    c["id"],
                    res.capacity,
                    res.registered,
                    res.available_seats,
                    res.raw_status,
                    notified=0,
                    course_name=res.course_name or c.get("course_name"),
                    last_alert_msg_id=None
                )

            # الحالة 4: تحديث البيانات العادية في حالة عدم توفر مقاعد
            else:
                db.update_course_status(
                    c["id"],
                    res.capacity,
                    res.registered,
                    res.available_seats,
                    res.raw_status,
                    course_name=res.course_name or c.get("course_name")
                )

            await asyncio.sleep(0.1)

        except Exception as e:
            logger.error(f"خطأ أثناء فحص المادة {c.get('course_no')}: {e}")


# ==========================================
# تشغيل وتهيئة البوت (Main Entrypoint)
# ==========================================

def main() -> None:
    """تهيئة وتشغيل البوت"""
    # تشغيل خادم الفحص الصحي فوراً لدعم منصات السحابة (Render / Koyeb) في ثريد منفصل
    threading.Thread(target=start_health_server, daemon=True).start()

    # تهيئة قاعدة البيانات
    db.init_db()

    if not config.BOT_TOKEN or config.BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        print("\n" + "=" * 60)
        print("⚠️ تنبيه: يرجى وضع التوكن الخاص ببوت التيليجرام في ملف .env أولاً!")
        print("=" * 60 + "\n")
        return

    # بناء تطبيق التيليجرام مع تفعيل الـ JobQueue
    application = Application.builder().token(config.BOT_TOKEN).build()

    # محادثة إضافة مادة للمراقبة (خطوتان فقط: رقم المادة -> رقم الشعبة)
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("track", start_tracking_conversation),
            CallbackQueryHandler(start_tracking_conversation, pattern="^btn_add_course$")
        ],
        states={
            WAITING_COURSE_NO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_course_no)
            ],
            WAITING_SECTION_NO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_section_no)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conversation),
            CommandHandler("start", start_command),
            CommandHandler("list", list_courses_handler),
            CommandHandler("help", help_command)
        ],
        allow_reentry=True,
        per_message=False
    )

    # تسجيل المعالجات (Handlers)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("list", list_courses_handler))
    application.add_handler(CommandHandler("check", check_command_direct))
    application.add_handler(CommandHandler("myid", my_id_command))
    application.add_handler(CommandHandler("activate", activate_command))
    
    # أوامر الأدمن والمشرفين
    application.add_handler(CommandHandler("genkey", admin_genkey_command))
    application.add_handler(CommandHandler("genkeys", admin_genkeys_command))
    application.add_handler(CommandHandler("keys", admin_keys_command))
    application.add_handler(CommandHandler("users", admin_users_command))
    application.add_handler(CommandHandler("revoke", admin_revoke_command))
    application.add_handler(CommandHandler("delkey", admin_delkey_command))
    application.add_handler(CommandHandler("stats", admin_stats_command))
    application.add_handler(CommandHandler("admin", admin_stats_command))
    application.add_handler(CommandHandler("admins", admin_admins_command))
    application.add_handler(CommandHandler("addadmin", admin_addadmin_command))
    application.add_handler(CommandHandler("deladmin", admin_deladmin_command))
    application.add_handler(CommandHandler("setlog", admin_setlog_command, filters=filters.UpdateType.MESSAGES | filters.UpdateType.CHANNEL_POSTS))
    application.add_handler(CommandHandler("unsetlog", admin_unsetlog_command, filters=filters.UpdateType.MESSAGES | filters.UpdateType.CHANNEL_POSTS))


    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(callback_query_router))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_general_text_and_activation))
    application.add_handler(MessageHandler(filters.PHOTO | filters.VOICE | filters.AUDIO | filters.Document.ALL | filters.Sticker.ALL, handle_general_media))

    # جدولة دورة الفحص الدوري في الخلفية
    job_queue = application.job_queue
    job_queue.run_repeating(
        background_course_scanner,
        interval=config.CHECK_INTERVAL_SECONDS,
        first=5
    )

    # تسجيل معالج الأخطاء العام
    application.add_error_handler(global_error_handler)

    print("=" * 60)
    print("🚀 تم تشغيل بوت مراقبة شواغر جامعة اليرموك بنجاح!")
    print(f"⏱️ الفحص الدوري يعمل كل {config.CHECK_INTERVAL_SECONDS} ثوانٍ.")
    print(f"🎯 وضع التشغيل: {'تجريبي (Demo Mode)' if config.DEMO_MODE else 'حي مباشر (Live SIS)'}")
    print(f"🔐 نظام مفاتيح التفعيل: {'مُفعل 🔒' if config.REQUIRE_ACTIVATION else 'معطل'}")
    print(f"👑 معرفات الأدمن: {config.ADMIN_IDS if config.ADMIN_IDS else 'لم يتم تعيين ADMIN_ID في .env'}")
    print("=" * 60)

    # بدء استقبال التحديثات مع حماية من التعارض أثناء التبديل على السحابة
    while True:
        try:
            application.run_polling(drop_pending_updates=True, close_loop=False)
            break
        except telegram.error.Conflict:
            logger.warning("⏳ جاري انتظار إغلاق الجلسة القديمة للبوت على السيرفر (8 ثوانٍ)...")
            time.sleep(8)
        except Exception as e:
            logger.error(f"تنبيه Polling: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()


