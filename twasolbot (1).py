
import logging
import sqlite3
import asyncio
import traceback # استيراد traceback لمعلومات الأخطاء المفصلة
from functools import wraps
from telegram import (
    Update,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,

    LinkPreviewOptions # لاستيراد خيارات معاينة الرابط
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
    Application # لاستيراد Application في post_init
)
from telegram.error import Forbidden, BadRequest # لاستيراد الأخطاء بشكل صريح

# --- تهيئة التسجيل ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
# ضبط مستوى تسجيل مكتبة httpx لتقليل الإسهاب (اختياري)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- إعدادات البوت والتحكم في التزامن ---
TOKEN = "7674638009:AAFxmo8-IB6LJYcVf4erKjCUS4AnZNoP1Gs" # !!! استبدل هذا بالتوكن الفعلي الخاص بك !!!
DB_FILE = 'bot.db' # اسم ملف قاعدة البيانات
processing_semaphore = asyncio.Semaphore(10) # تحديد عدد العمليات المتزامنة

# --- مزخرف للحد من التزامن ---
def limit_concurrency(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        async with processing_semaphore:
            return await func(*args, **kwargs)
    return wrapper

# --- دوال قاعدة البيانات ---
def init_db():
    """تهيئة قاعدة البيانات وإنشاء الجداول إذا لم تكن موجودة."""
    logger.info(f"Initializing database schema in '{DB_FILE}'...")
    # استخدام اتصال واحد للتهيئة
    try:
        with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
            cur = conn.cursor()
            # تعديل جدول blocked_users ليستخدم display_name
            cur.execute("""
                CREATE TABLE IF NOT EXISTS blocked_users (
                    user_id INTEGER PRIMARY KEY,
                    display_name TEXT NOT NULL
                )""")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )""")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS msg_map (
                    forwarded_msg_id INTEGER PRIMARY KEY,
                    original_chat_id INTEGER NOT NULL,
                    display_name TEXT NOT NULL
                )""")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    display_name TEXT NOT NULL
                )""")
            conn.commit()
            logger.info("Database schema initialized/verified successfully.")
    except sqlite3.Error as e:
        logger.error(f"Database initialization failed: {e}", exc_info=True)
        raise # إعادة رفع الخطأ لإيقاف البوت إذا فشلت التهيئة

def db_execute(query, params=(), fetch_one=False, fetch_all=False, commit=False):
    """دالة مساعدة لتنفيذ استعلامات SQL بأمان للخيوط."""
    try:
        with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
            cur = conn.cursor()
            cur.execute(query, params)
            if commit:
                conn.commit()
                return cur.rowcount # إرجاع عدد الصفوف المتأثرة في عمليات الكتابة
            if fetch_one:
                return cur.fetchone()
            if fetch_all:
                return cur.fetchall()
            return None # للحالات التي لا تتطلب إرجاع بيانات أو commit
    except sqlite3.Error as e:
        logger.error(f"Database error executing query '{query[:50]}...': {e}", exc_info=True)
        # يمكنك اختيارياً رفع الخطأ هنا أو إرجاع قيمة تشير للفشل
        # raise e
        return None # أو إرجاع None للإشارة إلى فشل

# --- دوال مساعدة ---
def get_display_name(user):
    """الحصول على أفضل اسم عرض للمستخدم."""
    if not user:
        return "مستخدم غير معروف"
    name = (user.first_name or '') + (' ' + user.last_name if user.last_name else '')
    name = name.strip()
    return name or user.username or f"مستخدم {user.id}" # استخدام المعرف كآخر خيار

async def update_user_info(user):
    """تحديث معلومات المستخدم في قاعدة البيانات (غير متزامن)."""
    if not user:
        return
    display_name = get_display_name(user)
    await asyncio.to_thread(
        db_execute,
        """
        INSERT OR REPLACE INTO users (user_id, username, first_name, last_name, display_name)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user.id, user.username, user.first_name, user.last_name, display_name),
        commit=True
    )

def load_blocked_users():
    """تحميل قائمة معرفات المستخدمين المحظورين."""
    result = db_execute("SELECT user_id FROM blocked_users", fetch_all=True)
    return {row[0] for row in result} if result is not None else set()

@limit_concurrency
async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة جميع الرسائل الخاصة (ليس فقط النصوص) من المستخدمين وإعادة توجيهها إلى المجموعة."""
    if not update.message or not update.message.from_user:
        return  # تجاهل التحديثات غير الصالحة

    user = update.message.from_user
    await update_user_info(user)  # تحديث معلومات المستخدم

    blocked_user_ids = await asyncio.to_thread(load_blocked_users)
    if user.id in blocked_user_ids:
        logger.info(f"المستخدم {user.id} ({get_display_name(user)}) محظور، يتم تجاهل رسالته.")
        return  # تجاهل الرسالة بصمت

    # جلب معرف المجموعة المستهدفة من إعدادات البوت
    group_id_row = await asyncio.to_thread(
        db_execute, "SELECT value FROM bot_settings WHERE key = 'group_id'", fetch_one=True
    )
    group_id = group_id_row[0] if group_id_row else None

    if not group_id:
        logger.warning("لم يتم تعيين معرف المجموعة في bot_settings.")
        await update.message.reply_text("عذراً، البوت قيد الصيانة حالياً. يرجى المحاولة لاحقاً.")
        return

    try:
        # إعادة توجيه الرسالة إلى المجموعة (سيتم إعادة توجيه جميع أنواع الرسائل)
        forwarded_msg = await context.bot.forward_message(
            chat_id=int(group_id),
            from_chat_id=update.message.chat_id,
            message_id=update.message.message_id
        )

        # تسجيل الربط بين الرسالة المعاد توجيهها والمستخدم الأصلي
        await asyncio.to_thread(
            db_execute,
            """
            INSERT OR REPLACE INTO msg_map (forwarded_msg_id, original_chat_id, display_name)
            VALUES (?, ?, ?)
            """,
            (forwarded_msg.message_id, user.id, get_display_name(user)),
            commit=True
        )

        # إرسال رسالة تأكيد للمستخدم
        await update.message.reply_text(
            "يرجى الانتظار، سيصلك الرد على رسالتك قريباً ⏳\n"
            "أتمنى لك يوماً سعيداً ومميزاً 😊\n"
            "Good day! 💚"
        )
        logger.info(f"تم إعادة توجيه رسالة المستخدم {user.id} ({get_display_name(user)}) إلى المجموعة {group_id}. msg_id: {forwarded_msg.message_id}")

    except Forbidden as e:
        logger.error(f"خطأ Forbidden أثناء إعادة توجيه الرسالة من المستخدم {user.id} إلى المجموعة {group_id}: {e}", exc_info=True)
        await update.message.reply_text("❌ حدث خطأ أثناء محاولة إرسال رسالتك. قد تكون هناك مشكلة في صلاحيات البوت في المجموعة.")
    except Exception as e:
        logger.error(f"خطأ أثناء إعادة توجيه رسالة المستخدم {user.id} ({get_display_name(user)}) إلى المجموعة {group_id}: {e}", exc_info=True)
        await update.message.reply_text("❌ عذراً، حدث خطأ غير متوقع أثناء إعادة توجيه رسالتك. يرجى المحاولة مرة أخرى.")

@limit_concurrency
async def handle_group_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة الردود من المشرفين في المجموعة."""
    if (not update.message or
        not update.message.reply_to_message or
        update.message.from_user.is_bot):
        # تجاهل الرسائل التي ليست ردوداً أو من بوتات أخرى
        return

    replied_msg = update.message.reply_to_message
    forwarded_msg_id = replied_msg.message_id
    admin_reply_msg_id = update.message.message_id # معرف رد المشرف الفعلي
    admin_user = update.message.from_user # المشرف الذي قام بالرد

    # البحث عن المستخدم الأصلي المرتبط بالرسالة التي تم الرد عليها
    mapping = await asyncio.to_thread(
        db_execute,
        "SELECT original_chat_id, display_name FROM msg_map WHERE forwarded_msg_id = ?",
        (forwarded_msg_id,),
        fetch_one=True
    )

    if not mapping:
        # لم يتم العثور على ربط (قد تكون رسالة قديمة، أو رد على رسالة غير مرتبطة بالبوت)
        logger.debug(f"No mapping found for replied message {forwarded_msg_id} in group {update.message.chat.id}. Ignoring reply from {admin_user.id}.")
        return

    original_user_id, original_display_name = mapping

    try:
        # نسخ رسالة المشرف إلى المستخدم الأصلي (أفضل من الإرسال للحفاظ على التنسيق والوسائط)
        await context.bot.copy_message(
            chat_id=original_user_id,
            from_chat_id=update.message.chat.id,
            message_id=admin_reply_msg_id,
            # لا نستخدم reply_to_message_id هنا لأنه يشير لرسالة في المجموعة
            allow_sending_without_reply=True
        )
        logger.info(f"Relayed reply from admin {admin_user.id} ({get_display_name(admin_user)}) (msg_id={admin_reply_msg_id}) to user {original_user_id} ({original_display_name})")

        # إرسال تأكيد للمشرف (اختياري)
        # await update.message.reply_text(f"✅ تم إرسال ردك إلى {original_display_name}.", quote=True)

    except Forbidden:
        # المستخدم حظر البوت
        logger.warning(f"Failed to send reply to user {original_user_id} ({original_display_name}): Bot was blocked.")
        await update.message.reply_text(
            f"⚠️ فشل إرسال الرد إلى {original_display_name} (`{original_user_id}`).\n"
            f"يبدو أن المستخدم قد قام بحظر البوت. تم تسجيله كمحظور.",
            parse_mode="Markdown"
        )
        # حظر المستخدم في قاعدة البيانات تلقائياً
        await asyncio.to_thread(
            db_execute,
            "INSERT OR REPLACE INTO blocked_users (user_id, display_name) VALUES (?, ?)",
            (original_user_id, original_display_name),
            commit=True
        )
        logger.info(f"User {original_user_id} ({original_display_name}) automatically blocked due to Forbidden error.")

    except BadRequest as e:
         logger.error(f"BadRequest sending reply to user {original_user_id} ({original_display_name}): {e}", exc_info=True)
         await update.message.reply_text(f"❌ حدث خطأ (BadRequest) أثناء محاولة إرسال الرد إلى المستخدم {original_display_name}. التفاصيل: {e}")
    except Exception as e:
        logger.error(f"Unexpected error sending reply to user {original_user_id} ({original_display_name}): {e}", exc_info=True)
        await update.message.reply_text(f"❌ حدث خطأ غير متوقع أثناء محاولة إرسال الرد إلى المستخدم {original_display_name}.")


# --- أوامر البوت ---
@limit_concurrency
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج الأمر /start في الدردشات الخاصة."""
    user = update.effective_user
    if user:
        await update_user_info(user) # تحديث معلومات المستخدم عند البدء
        await update.message.reply_text(
            f"هلو يا {user.first_name} القمر 🌝❤️\n"
            "انا اسمي تواصل🤖 روبوت لطيييف عم اشتغل لوصل رسائلك للأدمن"
           
        )
    else:
         logger.warning("Received /start command with no effective_user.")


@limit_concurrency
async def setgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج الأمر /setgroup لتعيين مجموعة الإدارة."""
    if not update.message or update.message.chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("⚠️ هذا الأمر مخصص للاستخدام داخل المجموعات فقط.")
        return

    # اختياري: التحقق من أن المستخدم هو مشرف في المجموعة
    # (يتطلب صلاحيات إضافية للبوت وقد لا يكون ضرورياً إذا كنت تثق بالمستخدمين)
    # try:
    #     chat_admins = await context.bot.get_chat_administrators(update.message.chat_id)
    #     admin_ids = {admin.user.id for admin in chat_admins}
    #     if update.message.from_user.id not in admin_ids:
    #         await update.message.reply_text("⚠️ يجب أن تكون مشرفاً في هذه المجموعة لاستخدام هذا الأمر.")
    #         return
    # except Exception as e:
    #     logger.warning(f"Could not verify admin status for setgroup command user {update.message.from_user.id} in chat {update.message.chat.id}: {e}")
    #     # يمكن المتابعة بحذر أو إيقاف التنفيذ

    group_id = update.message.chat.id
    group_title = update.message.chat.title or f"Group {group_id}"

    result = await asyncio.to_thread(
        db_execute,
        "INSERT OR REPLACE INTO bot_settings (key, value) VALUES ('group_id', ?)",
        (str(group_id),),
        commit=True
    )

    if result is not None: # نجح التنفيذ (حتى لو لم يؤثر على صفوف)
        logger.info(f"Group ID set to {group_id} ('{group_title}') by user {update.message.from_user.id}")
        await update.message.reply_text(f"✅ تم بنجاح تعيين هذه المجموعة ('{group_title}') كوجهة للرسائل.")
    else:
        logger.error(f"Failed to set group ID {group_id} in database.")
        await update.message.reply_text("❌ حدث خطأ أثناء محاولة حفظ معرف المجموعة في قاعدة البيانات.")


@limit_concurrency
async def block(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج الأمر /block لحظر مستخدم."""
    if not update.message or update.message.chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("⚠️ هذا الأمر مخصص للاستخدام داخل المجموعات فقط.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("⚠️ يرجى استخدام هذا الأمر بالرد على رسالة المستخدم الذي ترغب في حظره.")
        return

    admin_user = update.message.from_user
    replied_message = update.message.reply_to_message
    msg_id = replied_message.message_id
    user_to_block_id = None
    user_to_block_display_name = "مستخدم غير معروف"

    # محاولة تحديد المستخدم من قاعدة البيانات أولاً (msg_map)
    mapping = await asyncio.to_thread(
        db_execute,
        "SELECT original_chat_id, display_name FROM msg_map WHERE forwarded_msg_id = ?",
        (msg_id,),
        fetch_one=True
    )
    if mapping:
        user_to_block_id, user_to_block_display_name = mapping
        logger.info(f"Identified user {user_to_block_id} ('{user_to_block_display_name}') via msg_map for blocking.")
    # إذا لم يكن في msg_map، حاول من معلومات إعادة التوجيه
    elif replied_message.forward_from:
        fwd_user = replied_message.forward_from
        user_to_block_id = fwd_user.id
        # محاولة الحصول على الاسم الأحدث من جدول users كأفضلية
        user_data = await asyncio.to_thread(db_execute,"SELECT display_name FROM users WHERE user_id = ?", (user_to_block_id,), fetch_one=True)
        user_to_block_display_name = user_data[0] if user_data else get_display_name(fwd_user)
        logger.info(f"Identified user {user_to_block_id} ('{user_to_block_display_name}') via forward_from for blocking.")
    else:
        logger.warning(f"Could not identify user to block from reply to message {msg_id} in chat {update.message.chat.id}.")
        await update.message.reply_text("❌ لم أتمكن من تحديد المستخدم الأصلي من هذه الرسالة. هل هي رسالة تم إعادة توجيهها بواسطة البوت؟")
        return

    if not user_to_block_id:
        logger.error(f"Failed to extract user_id for blocking from message {msg_id}.")
        await update.message.reply_text("❌ حدث خطأ غير متوقع أثناء محاولة تحديد معرف المستخدم.")
        return

    # تنفيذ الحظر في قاعدة البيانات
    result = await asyncio.to_thread(
        db_execute,
        "INSERT OR REPLACE INTO blocked_users (user_id, display_name) VALUES (?, ?)",
        (user_to_block_id, user_to_block_display_name),
        commit=True
    )

    if result is not None:
        logger.info(f"User {user_to_block_id} ('{user_to_block_display_name}') blocked by admin {admin_user.id} in chat {update.message.chat.id}.")
        await update.message.reply_text(
            f"🚫 تم حظر المستخدم **{user_to_block_display_name}** (`{user_to_block_id}`) بنجاح.",
            parse_mode="Markdown"
        )
    else:
         logger.error(f"Failed to block user {user_to_block_id} in database.")
         await update.message.reply_text("❌ حدث خطأ أثناء محاولة حظر المستخدم في قاعدة البيانات.")


@limit_concurrency
async def unblock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج الأمر /unblock لفك حظر مستخدم."""
    if not update.message or update.message.chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("⚠️ هذا الأمر مخصص للاستخدام داخل المجموعات فقط.")
        return

    admin_user = update.message.from_user
    user_id_to_unblock = None
    display_name_guess = "المستخدم المحدد" # اسم افتراضي لرسائل الخطأ/النجاح

    # الحالة 1: فك الحظر بالمعرف (/unblock 12345)
    if context.args and len(context.args) == 1 and context.args[0].isdigit():
        user_id_to_unblock = int(context.args[0])
        logger.info(f"Attempting unblock by ID: {user_id_to_unblock} requested by admin {admin_user.id}.")
        # محاولة الحصول على الاسم من جدول المحظورين (إذا كان موجوداً)
        blocked_user_info = await asyncio.to_thread(db_execute, "SELECT display_name FROM blocked_users WHERE user_id = ?", (user_id_to_unblock,), fetch_one=True)
        if blocked_user_info:
            display_name_guess = blocked_user_info[0]
        else:
            # محاولة الحصول عليه من جدول users
             user_info = await asyncio.to_thread(db_execute, "SELECT display_name FROM users WHERE user_id = ?", (user_id_to_unblock,), fetch_one=True)
             if user_info:
                 display_name_guess = user_info[0]


    # الحالة 2: فك الحظر بالرد على رسالة
    elif update.message.reply_to_message:
        replied_message = update.message.reply_to_message
        msg_id = replied_message.message_id
        logger.info(f"Attempting unblock by reply to msg_id: {msg_id} requested by admin {admin_user.id}.")

        # البحث في msg_map أولاً
        mapping = await asyncio.to_thread(db_execute,"SELECT original_chat_id, display_name FROM msg_map WHERE forwarded_msg_id = ?", (msg_id,), fetch_one=True)
        if mapping:
            user_id_to_unblock, display_name_guess = mapping
            logger.info(f"Found user {user_id_to_unblock} ('{display_name_guess}') via msg_map for unblocking.")
        # إذا لم يكن في msg_map، حاول forward_from
        elif replied_message.forward_from:
            fwd_user = replied_message.forward_from
            user_id_to_unblock = fwd_user.id
            # حاول الحصول على الاسم الأحدث من جدول users أو blocked_users
            blocked_info = await asyncio.to_thread(db_execute,"SELECT display_name FROM blocked_users WHERE user_id = ?", (user_id_to_unblock,), fetch_one=True)
            if blocked_info:
                display_name_guess = blocked_info[0]
            else:
                user_info = await asyncio.to_thread(db_execute,"SELECT display_name FROM users WHERE user_id = ?", (user_id_to_unblock,), fetch_one=True)
                display_name_guess = user_info[0] if user_info else get_display_name(fwd_user)
            logger.info(f"Found user {user_id_to_unblock} ('{display_name_guess}') via forward_from for unblocking.")
        else:
            logger.warning(f"Could not identify user to unblock from reply to message {msg_id}.")
            # لا نرجع خطأ هنا، سنصل للتحقق من user_id_to_unblock لاحقاً

    else:
        # لم يتم تقديم معرف أو رد
        await update.message.reply_text("⚠️ يرجى استخدام هذا الأمر بالرد على رسالة المستخدم أو بإضافة معرف المستخدم بعد الأمر.\nمثال: `/unblock 123456789`")
        return

    # التحقق من العثور على معرف
    if not user_id_to_unblock:
        await update.message.reply_text("❌ لم أتمكن من تحديد معرف المستخدم لفك الحظر من الرسالة التي تم الرد عليها.")
        return

    # --- هنا يتم حل المشكلة: نتأكد من وجود العمود قبل استخدامه ---
    # الآن نستطيع بأمان محاولة الحذف والتحقق من النتيجة

    # تنفيذ فك الحظر في قاعدة البيانات
    deleted_rows = await asyncio.to_thread(
        db_execute,
        "DELETE FROM blocked_users WHERE user_id = ?",
        (user_id_to_unblock,),
        commit=True
    )

    if deleted_rows is not None: # تم تنفيذ الاستعلام بنجاح
        if deleted_rows > 0:
            # تم فك الحظر بنجاح
            logger.info(f"User {user_id_to_unblock} ('{display_name_guess}') unblocked by admin {admin_user.id} in chat {update.message.chat.id}.")
            await update.message.reply_text(
                f"✅ تم فك حظر **{display_name_guess}** (`{user_id_to_unblock}`) بنجاح.",
                parse_mode="Markdown"
            )
        else:
            # المستخدم لم يكن محظوراً أصلاً
            logger.info(f"Attempted to unblock user {user_id_to_unblock} ('{display_name_guess}'), but they were not found in blocked list.")
            await update.message.reply_text(
                f"ℹ️ المستخدم **{display_name_guess}** (`{user_id_to_unblock}`) لم يكن محظوراً في الأساس.",
                 parse_mode="Markdown"
            )
    else:
        # حدث خطأ في قاعدة البيانات أثناء الحذف
         logger.error(f"Failed to unblock user {user_id_to_unblock} in database.")
         await update.message.reply_text("❌ حدث خطأ أثناء محاولة فك حظر المستخدم في قاعدة البيانات.")


@limit_concurrency
async def showblocked(update: Update, context: ContextTypes.DEFAULT_TYPE):
     """معالج الأمر /showblocked لعرض قائمة المحظورين."""
     if not update.message or update.message.chat.type not in ['group', 'supergroup']:
         await update.message.reply_text("⚠️ هذا الأمر مخصص للاستخدام داخل المجموعات فقط.")
         return

     # جلب قائمة المحظورين من قاعدة البيانات
     blocked_list = await asyncio.to_thread(
         db_execute,
         "SELECT user_id, display_name FROM blocked_users ORDER BY display_name", # قراءة display_name
         fetch_all=True
     )

     if blocked_list is None: # خطأ في قاعدة البيانات
          await update.message.reply_text("❌ حدث خطأ أثناء محاولة جلب قائمة المحظورين.")
          return

     if not blocked_list:
         await update.message.reply_text("✅ لا يوجد أي مستخدمين محظورين حالياً.")
         return

     text = "🚫 **قائمة المستخدمين المحظورين حالياً:**\n\n"
     for user_id, display_name in blocked_list:
         # استخدام Markdown العادي لتجنب مشاكل التهريب المعقدة
         text += f"• {display_name} (`{user_id}`)\n"

     # إرسال الرسالة (قد تحتاج لتقسيمها إذا كانت القائمة طويلة جداً)
     try:
        await update.message.reply_text(text, parse_mode="Markdown")
     except BadRequest as e:
         # قد يحدث خطأ إذا كان النص طويلاً جداً أو التنسيق غير صالح
         logger.error(f"Error sending blocked list: {e}")
         await update.message.reply_text("❌ حدث خطأ أثناء عرض القائمة (قد تكون طويلة جداً).")
     except Exception as e:
        logger.error(f"Unexpected error during showblocked: {e}", exc_info=True)
        await update.message.reply_text("❌ حدث خطأ غير متوقع أثناء عرض القائمة.")


@limit_concurrency
async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج الأمر /info لعرض معلومات المستخدم مع زرَي التواصل والحظر أو إلغاء الحظر بحسب حالة الحظر."""
    if not update.message or update.message.chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("⚠️ هذا الأمر مخصص للاستخدام داخل المجموعات فقط.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("⚠️ يرجى استخدام هذا الأمر بالرد على رسالة المستخدم الذي ترغب في معرفة معلوماته.")
        return

    replied_message = update.message.reply_to_message
    msg_id = replied_message.message_id
    user_id = None
    display_name = "مستخدم غير معروف"
    username = None

    # استخراج معلومات المستخدم من الرسالة أو من قاعدة البيانات
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn_local:
        cur = conn_local.cursor()
        cur.execute("SELECT original_chat_id, display_name FROM msg_map WHERE forwarded_msg_id = ?", (msg_id,))
        result_map = cur.fetchone()
        if result_map:
            user_id, display_name = result_map
            cur.execute("SELECT username FROM users WHERE user_id = ?", (user_id,))
            user_data = cur.fetchone()
            if user_data:
                username = user_data[0]
        elif replied_message.forward_from:
            fwd_user = replied_message.forward_from
            user_id = fwd_user.id
            username = fwd_user.username
            cur.execute("SELECT display_name FROM users WHERE user_id = ?", (user_id,))
            name_result = cur.fetchone()
            display_name = name_result[0] if name_result else get_display_name(fwd_user)
        else:
            await update.message.reply_text("❌ لم أتمكن من تحديد المستخدم الأصلي من هذه الرسالة.")
            return

        if not user_id:
            await update.message.reply_text("❌ حدث خطأ غير متوقع أثناء محاولة تحديد معرف المستخدم.")
            return

        # التحقق من حالة الحظر
        cur.execute("SELECT user_id FROM blocked_users WHERE user_id = ?", (user_id,))
        blocked_result = cur.fetchone()
        blocked_status = "محظور 🚫" if blocked_result else "غير محظور ✅"

    # تجهيز نص المعلومات
    info_text = f"📌 **معلومات المستخدم:**\n\n"
    info_text += f"👤 **الاسم:** {display_name}\n"
    info_text += f"🆔 **المعرف (ID):** `{user_id}`\n"
    info_text += f"🔒 **الحالة:** {blocked_status}\n"
    if username:
        info_text += f"📧 **اسم المستخدم:** @{username}\n"
        user_link = f"https://t.me/{username}"
    else:
        info_text += f"📧 **اسم المستخدم:** (لا يوجد)\n"
        user_link = f"tg://openmessage?user_id={user_id}"

    # تحديد الزر بحسب حالة الحظر
    if blocked_result:
        block_button = InlineKeyboardButton("✅ إلغاء الحظر", callback_data=f"unblock_{user_id}")
    else:
        block_button = InlineKeyboardButton("🚫 حظر", callback_data=f"block_{user_id}")

    keyboard = [
        [InlineKeyboardButton("✉️ تواصل", url=user_link), block_button]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    link_options = LinkPreviewOptions(is_disabled=True)

    try:
        await update.message.reply_text(info_text, parse_mode="Markdown", reply_markup=reply_markup, link_preview_options=link_options)
        logger.info(f"تم إرسال معلومات المستخدم (ID: {user_id}) مع الأزرار.")
    except Exception as e:
        logger.error(f"خطأ أثناء إرسال معلومات المستخدم: {e}", exc_info=True)
        await update.message.reply_text("❌ حدث خطأ أثناء عرض المعلومات.")


@limit_concurrency
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج ضغط الأزرار لتحديث حالة الحظر في الرسالة."""
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    # تأكيد استلام الضغط لتفادي تجمّد الأزرار
    await query.answer()

    parts = data.split("_", 1)
    if len(parts) != 2:
        await query.answer("بيانات غير صحيحة.", show_alert=True)
        return
    action, user_id_str = parts
    try:
        user_id = int(user_id_str)
    except ValueError:
        await query.answer("خطأ في المعرف.", show_alert=True)
        return

    # تنفيذ العملية المطلوبة بناءً على الزر المضغوط
    if action == "block":
        blocked = await asyncio.to_thread(db_execute, "SELECT user_id FROM blocked_users WHERE user_id = ?", (user_id,), fetch_one=True)
        if blocked:
            await query.answer("المستخدم محظور مسبقاً.")
        else:
            user_info = await asyncio.to_thread(db_execute, "SELECT display_name FROM users WHERE user_id = ?", (user_id,), fetch_one=True)
            display_name = user_info[0] if user_info else "مستخدم غير معروف"
            result = await asyncio.to_thread(db_execute, "INSERT OR REPLACE INTO blocked_users (user_id, display_name) VALUES (?, ?)", (user_id, display_name), commit=True)
            if result is not None:
                await query.answer("تم حظر المستخدم.")
            else:
                await query.answer("حدث خطأ أثناء حظر المستخدم.", show_alert=True)
    elif action == "unblock":
        deleted_rows = await asyncio.to_thread(db_execute, "DELETE FROM blocked_users WHERE user_id = ?", (user_id,), commit=True)
        if deleted_rows is not None and deleted_rows > 0:
            await query.answer("تم فك حظر المستخدم.")
        else:
            await query.answer("المستخدم غير محظور أو حدث خطأ.", show_alert=True)
    else:
        await query.answer("عملية غير معروفة.", show_alert=True)
        return

    # إعادة حساب المعلومات لتحديث الرسالة
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    cur = conn.cursor()
    cur.execute("SELECT display_name, username FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if row:
        display_name, username = row
    else:
        display_name, username = "مستخدم غير معروف", None
    cur.execute("SELECT user_id FROM blocked_users WHERE user_id = ?", (user_id,))
    blocked_result = cur.fetchone()
    blocked_status = "محظور 🚫" if blocked_result else "غير محظور ✅"
    conn.close()

    info_text = f"📌 **معلومات المستخدم:**\n\n"
    info_text += f"👤 **الاسم:** {display_name}\n"
    info_text += f"🆔 **المعرف (ID):** `{user_id}`\n"
    info_text += f"🔒 **الحالة:** {blocked_status}\n"
    if username:
        info_text += f"📧 **اسم المستخدم:** @{username}\n"
        user_link = f"https://t.me/{username}"
    else:
        info_text += f"📧 **اسم المستخدم:** (لا يوجد)\n"
        user_link = f"tg://openmessage?user_id={user_id}"

    # تحديث الزر بناءً على الحالة الجديدة
    if blocked_result:
        block_button = InlineKeyboardButton("✅ إلغاء الحظر", callback_data=f"unblock_{user_id}")
    else:
        block_button = InlineKeyboardButton("🚫 حظر", callback_data=f"block_{user_id}")

    keyboard = [
        [InlineKeyboardButton("✉️ تواصل", url=user_link), block_button]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await query.edit_message_text(text=info_text, parse_mode="Markdown", reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"خطأ أثناء تحديث رسالة المعلومات: {e}", exc_info=True)
        await query.answer("حدث خطأ أثناء تحديث المعلومات.", show_alert=True)



BROADCAST_INPUT = 0  # حالة انتظار إدخال نص الإذاعة

@limit_concurrency
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    يبدأ أمر الإذاعة: يطلب من المستخدم كتابة الرسالة التي سيتم إرسالها إلى باقي المستخدمين،
    مع عرض زر إلغاء العملية.
    """
    # تأكد من أن الأمر جاء من مجموعة (حسب طلبك) ويمكنك تعديل ذلك إذا لزم الأمر
    if not update.message or update.message.chat.type not in ['group', 'supergroup']:
        return

    user = update.message.from_user
    # حفظ معرف المستخدم الذي بدأ العملية للتأكد لاحقاً من الاستجابة له فقط
    context.user_data["broadcast_initiator"] = user.id

    # إنشاء لوحة مفاتيح مع زر إلغاء
    keyboard = [
        [InlineKeyboardButton("إلغاء الإرسال", callback_data="cancel_broadcast")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "يرجى كتابة الرسالة التي تريد إرسالها إلى باقي المستخدمين.\n"
        "إذا لم ترغب في إرسالها، اضغط على زر 'إلغاء الإرسال'.\n"
        "فقط من وضع الأمر يمكنه إرسال الرسالة من يريد إرسال رسالة جماعية يجب ان يضغط على أمر البث.",
        reply_markup=reply_markup
    )
    return BROADCAST_INPUT

@limit_concurrency
async def receive_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    يستقبل رسالة الإذاعة من المستخدم الذي بدأ العملية ويقوم بإرسالها إلى باقي المستخدمين.
    يتم التحقق من هوية المرسل بحيث يتم الاستجابة فقط للمستخدم الذي بدأ الأمر.
    """
    # تحقق من أن المرسل هو نفس الذي بدأ الأمر
    if update.message.from_user.id != context.user_data.get("broadcast_initiator"):
        return  # تجاهل الرسائل من مستخدمين آخرين

    message_text = update.message.text
    admin_user = update.message.from_user

    # جلب قائمة المستخدمين غير المحظورين من قاعدة البيانات
    users_to_broadcast = await asyncio.to_thread(
        db_execute,
        """
        SELECT u.user_id, u.display_name
        FROM users u
        LEFT JOIN blocked_users b ON u.user_id = b.user_id
        WHERE b.user_id IS NULL
        """,
        fetch_all=True
    )

    if users_to_broadcast is None:
        await update.message.reply_text("❌ حدث خطأ أثناء جلب قائمة المستخدمين للإذاعة.")
        return ConversationHandler.END

    total_users = len(users_to_broadcast)
    if total_users == 0:
        await update.message.reply_text("ℹ️ لا يوجد مستخدمون (غير محظورين) لإرسال الإذاعة إليهم حالياً.")
        return ConversationHandler.END

    status_message = await update.message.reply_text(f"⏳ جاري بدء الإذاعة إلى {total_users} مستخدم...")
    success_count = 0
    failure_count = 0
    blocked_count = 0

    for i, (user_id, display_name) in enumerate(users_to_broadcast):
        try:
            await context.bot.send_message(chat_id=user_id, text=message_text)
            success_count += 1
        except Forbidden:
            blocked_count += 1
            await asyncio.to_thread(
                db_execute,
                "INSERT OR REPLACE INTO blocked_users (user_id, display_name) VALUES (?, ?)",
                (user_id, display_name),
                commit=True
            )
        except BadRequest:
            failure_count += 1
        except Exception:
            failure_count += 1

        if (i + 1) % 25 == 0 or (i + 1) == total_users:
            try:
                status_text = (
                    f"⏳ عملية إرسال الرسائل جارية... ({i + 1}/{total_users})\n"
                    f"✅ نجح: {success_count}\n"
                    f"🚫 حظر: {blocked_count}\n"
                    f"❌ فشل: {failure_count}"
                )
                await status_message.edit_text(status_text)
            except Exception:
                pass
        await asyncio.sleep(0.05)

    final_text = (
        f"🏁 **اكتملت عملية ارسال الرسائل للجميع**\n\n"
        f"📬 تم الإرسال بنجاح إلى: {success_count} مستخدم\n"
        f"🚫 تم اكتشاف حظر من: {blocked_count} مستخدم (وتم حظرهم)\n"
        f"❌ فشل الإرسال إلى: {failure_count} مستخدم"
    )
    try:
        await status_message.edit_text(final_text, parse_mode="Markdown")
    except Exception:
        await update.message.reply_text(final_text, parse_mode="Markdown")

    return ConversationHandler.END

@limit_concurrency
async def cancel_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    معالج زر إلغاء الإرسال: ينهي عملية الإذاعة ويحدّث نص الرسالة إلى "تم إلغاء الإرسال".
    """
    query = update.callback_query
    if query:
        await query.answer("تم إلغاء عملية الإذاعة.")
        await query.edit_message_text("تم إلغاء إرسال الرسالة الجماعية.")
    return ConversationHandler.END


async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    دالة تنفذ أمر /ban لحظر مستخدم من القنوات والمجموعات المحددة عند الرد على رسالته.
    """
    if not update.message.reply_to_message:
        await update.message.reply_text("⚠️ يرجى الرد على رسالة المستخدم الذي ترغب في حظره.")
        return

    # تحديد الرسالة التي تم الرد عليها
    replied_message = update.message.reply_to_message
    msg_id = replied_message.message_id
    user_to_ban_id = None
    user_to_ban_display_name = "مستخدم غير معروف"

    # محاولة تحديد المستخدم من قاعدة البيانات أولاً (msg_map)
    mapping = await asyncio.to_thread(
        db_execute,
        "SELECT original_chat_id, display_name FROM msg_map WHERE forwarded_msg_id = ?",
        (msg_id,),
        fetch_one=True
    )
    if mapping:
        user_to_ban_id, user_to_ban_display_name = mapping
        logger.info(f"Identified user {user_to_ban_id} ('{user_to_ban_display_name}') via msg_map for banning.")
    # إذا لم يكن في msg_map، حاول من معلومات إعادة التوجيه
    elif replied_message.forward_from:
        fwd_user = replied_message.forward_from
        user_to_ban_id = fwd_user.id
        # محاولة الحصول على الاسم الأحدث من جدول users كأفضلية
        user_data = await asyncio.to_thread(db_execute,"SELECT display_name FROM users WHERE user_id = ?", (user_to_ban_id,), fetch_one=True)
        user_to_ban_display_name = user_data[0] if user_data else get_display_name(fwd_user)
        logger.info(f"Identified user {user_to_ban_id} ('{user_to_ban_display_name}') via forward_from for banning.")
    else:
        logger.warning(f"Could not identify user to ban from reply to message {msg_id} in chat {update.message.chat.id}.")
        await update.message.reply_text("❌ لم أتمكن من تحديد المستخدم الأصلي من هذه الرسالة. هل هي رسالة تم إعادة توجيهها بواسطة البوت؟")
        return

    if not user_to_ban_id:
        logger.error(f"Failed to extract user_id for banning from message {msg_id}.")
        await update.message.reply_text("❌ حدث خطأ غير متوقع أثناء محاولة تحديد معرف المستخدم.")
        return

    # قائمة القنوات والمجموعات المحظورة
    banned_chats = [-1002362198685,  -1002576351421,-1002411178192]  # يمكن إضافة القنوات والمجموعات هنا

    # محاولة حظر المستخدم من كل قناة أو مجموعة في القائمة
    failed_chats = []  # لتخزين القنوات التي فشل الحظر فيها
    successful_chats = []  # لتخزين القنوات/المجموعات التي تم حظر المستخدم منها بنجاح
    for chat_id in banned_chats:
        try:
            chat = await context.bot.get_chat(chat_id)

            # الحصول على اسم القناة أو المجموعة
            chat_name = chat.title

            if chat.type in ['supergroup', 'group']:  # إذا كانت مجموعة أو سوبرغروب
                # حظر المستخدم من المجموعة
                await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_to_ban_id)
                logger.info(f"تم حظر المستخدم ذو المعرف {user_to_ban_id} من المجموعة {chat_name}.")
                successful_chats.append(f"{chat_name} (المجموعة)")
            elif chat.type == 'channel':  # إذا كانت قناة
                # استخدام ban_chat_member بدلاً من kick_chat_member في القنوات
                await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_to_ban_id)
                logger.info(f"تم حظر المستخدم ذو المعرف {user_to_ban_id} من القناة {chat_name}.")
                successful_chats.append(f"{chat_name} (القناة)")
            else:
                failed_chats.append(chat_id)

        except Exception as e:
            logger.error(f"خطأ أثناء محاولة حظر المستخدم {user_to_ban_id} من القناة/المجموعة {chat_id}: {e}")
            failed_chats.append(chat_id)

    # إعلام المستخدم في حالة الفشل أو النجاح
    if failed_chats:
        await update.message.reply_text(
            f"حدث خطأ في محاولة حظر المستخدم ذو المعرف {user_to_ban_id} من القنوات والمجموعات التالية:\n" + "\n".join(map(str, failed_chats))
        )
    else:
        await update.message.reply_text(f"تم حظر المستخدم **{user_to_ban_display_name}** (`{user_to_ban_id}`) من القنوات والمجموعات التالية:\n" + "\n".join(successful_chats))




    
async def get_bot_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    دالة للحصول على جميع القنوات والمجموعات التي تم إضافة البوت إليها.
    """
    chat_ids = []
    # الحصول على معلومات المجموعات والقنوات التي يكون البوت عضوًا فيها
    for chat in await context.bot.get_chat_administrators(update.message.chat.id):
        chat_ids.append(chat.chat.id)
    return chat_ids

# --- دالة التهيئة بعد تشغيل التطبيق ---
async def post_init(application: Application):
    """يتم استدعاؤها بعد تهيئة التطبيق وقبل بدء التشغيل."""
    # 1. تهيئة قاعدة البيانات أولاً
    try:
        # تأكد من أن الدالة init_db نفسها تتعامل مع الاتصال
        await asyncio.to_thread(init_db)
    except Exception as e:
        logger.critical(f"CRITICAL: Database initialization failed during post_init. Bot cannot start properly. Error: {e}", exc_info=True)
        # يمكنك إيقاف البوت هنا إذا كانت قاعدة البيانات ضرورية جداً
        # application.stop() # قد تحتاج إلى طريقة أكثر قوة للإيقاف
        return # أو منع تعيين الأوامر إذا فشلت الـ DB

    # 2. تعيين أوامر البوت
    logger.info("Setting bot commands...")
    group_commands = [
        BotCommand("setgroup", "تعيين هذه المجموعة كوجهة للرسائل"),
        BotCommand("ban", "للحظر من القنوات والمجموعات"),
        BotCommand("block", "حظر مستخدم (بالرد على رسالته)"),
        BotCommand("unblock", "فك حظر مستخدم (بالرد أو بالمعرف)"),
        BotCommand("showblocked", "عرض قائمة المحظورين"),
        BotCommand("info", "عرض معلومات المستخدم (بالرد على رسالته)"),
        BotCommand("broadcast", "إرسال رسالة جماعية (للمشرفين)")
    ]
    private_commands = [
        BotCommand("start", "بدء استخدام البوت والتواصل")
    ]
    try:
        await application.bot.set_my_commands(
            commands=group_commands,
            scope=BotCommandScopeAllGroupChats()
        )
        logger.info("Group commands set.")
        await application.bot.set_my_commands(
            commands=private_commands,
            scope=BotCommandScopeAllPrivateChats()
        )
        logger.info("Private commands set.")
    except Exception as e:
        logger.error(f"Failed to set bot commands: {e}", exc_info=True)


def main():
    logger.info("Starting bot application...")

    application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    # أوامر البوت الأخرى
    application.add_handler(CommandHandler("start", start, filters=filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("setgroup", setgroup, filters=filters.ChatType.GROUPS))
    application.add_handler(CommandHandler("ban", ban_user, filters=filters.ChatType.GROUPS))
    application.add_handler(CommandHandler("block", block, filters=filters.ChatType.GROUPS))
    application.add_handler(CommandHandler("unblock", unblock, filters=filters.ChatType.GROUPS))
    application.add_handler(CommandHandler("showblocked", showblocked, filters=filters.ChatType.GROUPS))
    application.add_handler(CommandHandler("info", info, filters=filters.ChatType.GROUPS))

    application.add_handler(CommandHandler("ban", ban_user, filters=filters.ChatType.GROUPS))

    
    # إضافة معالج الضغط على أزرار الحظر وإلغاء الحظر
    application.add_handler(CallbackQueryHandler(button_callback, pattern="^(block_|unblock_).*"))

    # إضافة معالج المحادثة لأمر broadcast
    broadcast_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("broadcast", broadcast_command, filters=filters.ChatType.GROUPS)],
        states={
            BROADCAST_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_broadcast_message),
                CallbackQueryHandler(cancel_broadcast, pattern="^cancel_broadcast$")
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel_broadcast)],
        per_user=True,
        per_chat=True
    )
    application.add_handler(broadcast_conv_handler)

    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_private_message))
    application.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.REPLY & ~filters.COMMAND, handle_group_reply))

    logger.info("Running application polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("Bot application stopped.")




# --- نقطة الدخول ---
if __name__ == '__main__':
    main()