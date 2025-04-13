import asyncio
import aiosqlite
import logging
import math
import aiomysql
import re

from datetime import datetime, timedelta
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    CallbackContext,
    filters,
)

# إعدادات التسجيل
TOKEN = "7828234002:AAFm48EYXAvvk6y628u4bkH--ylsKsrk8kI"
ADMIN_IDS = [949946393, 7715493020, 6908524236,999599887]
PRIVILEGED_ADMIN_IDS = [6908524236] 

# تعريف حالات المحادثة
SELL_ACCOUNT = 1
BUY_EMAILS = 10               # شراء ايميلات
BUY_BY_BALANCE = 11           # شراء ايميلات حسب الرصيد
RECHARGE_SERIAL_NUMBER = 12   # انتظار رقم العملية لشحن البوت (سيرياتيل كاش)
RECHARGE_AMOUNT = 13          # انتظار مبلغ الشحن (سيرياتيل كاش)
RECHARGE_SERIAL_NUMBER_PAYEER = 14   # انتظار رقم العملية لشحن البوت عبر بايير
RECHARGE_AMOUNT_PAYEER = 15          # انتظار مبلغ الشحن بالدولار (بايير)
ADMIN_UPDATE_PRICE = 20       # تحديث سعر الحساب
ADMIN_BROADCAST = 30          # إرسال رسالة لجميع المستخدمين
ADMIN_SET_CHANNEL = 500       # تعيين رابط القناة
ADMIN_ADD_EMAILS = 700
ADMIN_CHANGE_PASSWORD = 401   # تغيير كلمة المرور للأدمن
ADMIN_SET_PAYEER_RATE = 505   # تعيين سعر الدولار للبايير
ADMIN_DAILY_REPORT = 600      # حالة عرض تقرير المبيعات اليومية
REJECT_RECHARGE_REASON = 210  # حالة رفض طلب الشحن مع سبب
ADMIN_CHANGE_SYRIATELCASH = 801
REJECT_WITHDRAWAL_REASON = 220
# عدد العمليات المتزامنة المسموح بها للوصول إلى قاعدة البيانات
MAX_CONCURRENT_DB = 10
# ثابت حالات محادثات المستخدم للطلبات الجديدة:
WITHDRAW_ACCOUNT, WITHDRAW_AMOUNT = range(2)
REFUND_EMAILS_STATE, EXCHANGE_EMAILS_STATE = range(10, 12)
##############################################################################
# دالة مساعدة للتعديل الآمن على نص الرسالة
##############################################################################
async def safe_edit_message_text(update: Update, context: CallbackContext, text: str, reply_markup=None):
    if update.callback_query and update.callback_query.message:
        try:
            return await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
        except Exception as e:
            logging.error("Error editing message: %s", e)
    return await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup, parse_mode="HTML")

##############################################################################
# قسم قاعدة البيانات
##############################################################################
import aiomysql
import asyncio
import logging
import math
import re
from datetime import datetime, timedelta

# عدد العمليات المتزامنة المسموح بها للوصول إلى قاعدة البيانات
MAX_CONCURRENT_DB = 10

##############################################################################
# فصل قاعدة البيانات باستخدام MySQL
##############################################################################
class Database:
    def __init__(self, host="localhost", port=3306, user="root", password="haedaralaliite5556", db="data"):
        self.pool = None
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.db = db
        self.settings_cache = {}

    async def init_db(self):
        # إنشاء تجمع الاتصالات مع خيارات تحسين الأداء وسعة التزامن
        self.pool = await aiomysql.create_pool(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            db=self.db,
            autocommit=True,
            maxsize=MAX_CONCURRENT_DB,
            charset="utf8mb4"
        )
        # إنشاء الجداول إن لم تكن موجودة
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                # جدول الحسابات مع عمود كلمة المرور وعمود sold_at لتاريخ البيع
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS accounts (
                        id INT PRIMARY KEY AUTO_INCREMENT,
                        seller_id BIGINT  NOT NULL,
                        seller_name VARCHAR(255) NOT NULL,
                        details TEXT NOT NULL,
                        password VARCHAR(255),
                        purchased_emails TEXT,
                        status VARCHAR(50) NOT NULL DEFAULT 'approved',
                        reject_reason TEXT,
                        verifier_id BIGINT ,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        sold_at VARCHAR(50)
                    )
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS email_exchange_requests (
                        id INT PRIMARY KEY AUTO_INCREMENT,
                        user_id BIGINT,
                        user_name VARCHAR(255),
                        emails TEXT,
                        request_type VARCHAR(50), -- يمكن أن تكون "refund" أو "exchange"
                        timestamp VARCHAR(50)
                    )
                """)
                # جدول السحوبات
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS withdrawals (
                        id INT PRIMARY KEY AUTO_INCREMENT,
                        user_id BIGINT  NOT NULL,
                        user_name VARCHAR(255) NOT NULL,
                        account_code VARCHAR(255) NOT NULL,
                        amount DOUBLE NOT NULL,
                        method VARCHAR(50) NOT NULL,
                        status VARCHAR(50) NOT NULL DEFAULT 'pending',
                        reject_reason TEXT,
                        verifier_id BIGINT ,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                # جدول الإعدادات
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS settings (
                        `key` VARCHAR(255) PRIMARY KEY,
                        `value` VARCHAR(255) NOT NULL
                    )
                """)
                # جدول المستخدمين
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        user_id BIGINT  PRIMARY KEY,
                        user_name VARCHAR(255) NOT NULL,
                        balance DOUBLE NOT NULL DEFAULT 0
                    )
                """)
                # جدول طلبات شراء الإيميلات
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS purchase_requests (
                        id INT PRIMARY KEY AUTO_INCREMENT,
                        user_id BIGINT ,
                        user_name VARCHAR(255),
                        count INT,
                        emails TEXT,
                        timestamp VARCHAR(50)
                    )
                """)
                # جدول طلبات الشحن
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS recharge_requests (
                        id INT PRIMARY KEY AUTO_INCREMENT,
                        user_id BIGINT ,
                        user_name VARCHAR(255),
                        op_number VARCHAR(255),
                        amount DOUBLE,
                        method VARCHAR(50),
                        timestamp VARCHAR(50)
                    )
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS processed_recharge_requests (
                        id INT PRIMARY KEY AUTO_INCREMENT,
                        user_id BIGINT ,
                        user_name VARCHAR(255),
                        op_number VARCHAR(255),
                        amount DOUBLE,
                        status VARCHAR(50),
                        reject_reason TEXT,
                        timestamp VARCHAR(50)
                    )
                """)
        # إعداد القيم الافتراضية للإعدادات إذا لم تكن موجودة
        if await self.get_setting("account_price") is None:
            await self.set_setting("account_price", 1500.0)
        if await self.get_setting("account_password") is None:
            await self.set_setting("account_password", "default_pass")
        if await self.get_setting("account_syriatelcash") is None:
            await self.set_setting("account_syriatelcash", "default_cash")
        if await self.get_setting("channel_link") is None:
            await self.set_setting("channel_link", "")
        if await self.get_setting("payeer_rate") is None:
            await self.set_setting("payeer_rate", 10000.0)

    # دالة جلب إعداد معين
    async def get_setting(self, key):
        if key in self.settings_cache:
            return self.settings_cache[key]
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT `value` FROM settings WHERE `key` = %s", (key,))
                row = await cur.fetchone()
                value = row[0] if row else None
                if value is not None:
                    self.settings_cache[key] = value
                return value

    # دالة تعديل إعداد باستخدام INSERT ... ON DUPLICATE KEY UPDATE
    async def set_setting(self, key, value):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO settings (`key`, `value`) VALUES (%s, %s) ON DUPLICATE KEY UPDATE `value` = VALUES(`value`)",
                    (key, str(value))
                )
                self.settings_cache[key] = str(value)

    # دالة إضافة حساب جديد
    async def add_account(self, seller_id, seller_name, details, password=None):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO accounts (seller_id, seller_name, details, password) VALUES (%s, %s, %s, %s)",
                    (seller_id, seller_name, details, password)
                )
                account_id = cur.lastrowid
                return account_id

    async def update_account_status(self, account_id, status, reject_reason=None, verifier_id=None, sold_at=None):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE accounts SET status = %s, reject_reason = %s, verifier_id = %s, sold_at = %s WHERE id = %s",
                    (status, reject_reason, verifier_id, sold_at, account_id)
                )

    async def get_account_by_id(self, account_id):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT * FROM accounts WHERE id = %s", (account_id,))
                return await cur.fetchone()

    async def get_accounts_by_status(self, status, seller_id=None):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                if seller_id is not None:
                    await cur.execute("SELECT * FROM accounts WHERE status = %s AND seller_id = %s", (status, seller_id))
                else:
                    await cur.execute("SELECT * FROM accounts WHERE status = %s", (status,))
                return await cur.fetchall()

    # دالة شراء الإيميلات مع ترتيب تناوبي بين أصحاب الحزم وتحديث حالة الحسابات
    async def purchase_emails(self, count):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, details, password, seller_name, created_at FROM accounts WHERE status = 'approved' AND (purchased_emails IS NULL OR purchased_emails = '') ORDER BY created_at ASC"
                )
                rows = await cur.fetchall()
        if not rows:
            return []

        # تجميع الحسابات حسب صاحب الحزمة
        groups = {}
        for row in rows:
            seller = row[3]
            groups.setdefault(seller, []).append(row)

        # إعادة ترتيب أسماء أصحاب الحزم
        sorted_sellers = sorted(groups.keys())
        if not sorted_sellers:
            return []
        # الحصول على آخر مؤشر مستخدم من الإعدادات
        last_index_str = await self.get_setting("last_used_seller_index")
        last_index = int(last_index_str) if last_index_str and last_index_str.isdigit() else -1
        start_index = (last_index + 1) % len(sorted_sellers)
        rotated_sellers = sorted_sellers[start_index:] + sorted_sellers[:start_index]

        # ترتيب تناوبي للحسابات
        interleaved = []
        index = 0
        while True:
            added = False
            for seller in rotated_sellers:
                if index < len(groups[seller]):
                    interleaved.append(groups[seller][index])
                    added = True
            if not added:
                break
            index += 1

        selected = interleaved[:count]
        if selected:
            last_selected_seller = selected[-1][3]
            new_last_index = sorted_sellers.index(last_selected_seller)
            await self.set_setting("last_used_seller_index", new_last_index)

        sold_date = (datetime.now() + timedelta(hours=3)).strftime("%Y-%m-%d")
        accounts = []
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                for row in selected:
                    account_id, email, pwd, seller, _ = row
                    accounts.append((email, pwd, seller))
                    formatted = f"{email}|{pwd}"
                    await cur.execute(
                        "UPDATE accounts SET purchased_emails = %s, status = 'sold', details = '', password = '', sold_at = %s WHERE id = %s",
                        (formatted, sold_date, account_id)
                    )
        return accounts

    async def count_available_emails(self):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT COUNT(*) FROM accounts WHERE status = 'approved' AND (purchased_emails IS NULL OR purchased_emails = '')"
                )
                row = await cur.fetchone()
                return row[0] if row else 0

    async def add_withdrawal(self, user_id, user_name, account_code, amount, method):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO withdrawals (user_id, user_name, account_code, amount, method) VALUES (%s, %s, %s, %s, %s)",
                    (user_id, user_name, account_code, amount, method)
                )
                return cur.lastrowid

    async def update_withdrawal_status(self, withdrawal_id, status, reject_reason=None, verifier_id=None):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE withdrawals SET status = %s, reject_reason = %s, verifier_id = %s WHERE id = %s",
                    (status, reject_reason, verifier_id, withdrawal_id)
                )

    async def get_withdrawal_by_id(self, withdrawal_id):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT * FROM withdrawals WHERE id = %s", (withdrawal_id,))
                return await cur.fetchone()

    async def get_withdrawals_by_status(self, status, user_id=None):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                if user_id is not None:
                    await cur.execute("SELECT * FROM withdrawals WHERE status = %s AND user_id = %s", (status, user_id))
                else:
                    await cur.execute("SELECT * FROM withdrawals WHERE status = %s", (status,))
                return await cur.fetchall()

    # دالة جلب رصيد المستخدم
    async def get_user_balance(self, user_id):
        """
        تستخرج رصيد المستخدم من جدول users باستخدام معرف المستخدم.
        تستخدم هذه الدالة اتصال من تجمع الاتصالات (pool) لضمان كفاءة الأداء.
        """
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT balance FROM users WHERE user_id = %s", (user_id,))
                row = await cur.fetchone()
                # في حالة عدم وجود المستخدم يتم إرجاع 0.0 كرصيد افتراضي.
                return float(row[0]) if row else 0.0

    # دالة تحديث رصيد المستخدم
    async def update_user_balance(self, user_id, new_balance):
        """
        تقوم بتحديث رصيد المستخدم في جدول users إلى القيمة الجديدة.
        تستخدم اتصال من تجمع الاتصالات لتقليل استهلاك الموارد وتحسين الأداء.
        """
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("UPDATE users SET balance = %s WHERE user_id = %s", (new_balance, user_id))
                
    # دالة إضافة مستخدم جديد في حالة عدم وجوده مسبقًا
    async def add_user(self, user_id, user_name):
        """
        تُضيف مستخدم جديد إلى جدول users مع رصيد ابتدائي يساوي 0.
        يتم استخدام INSERT IGNORE أو ما يعادله لضمان عدم تكرار سجلات المستخدم.
        """
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT IGNORE INTO users (user_id, user_name, balance) VALUES (%s, %s, %s)",
                    (user_id, user_name, 0)
                )
    async def get_all_users(self):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT user_id FROM users")
                rows = await cur.fetchall()
                return [row[0] for row in rows]

    async def add_purchase_request(self, user_id, user_name, count, emails, timestamp):
        emails_str = ",".join(emails) if emails else ""
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO purchase_requests (user_id, user_name, count, emails, timestamp) VALUES (%s, %s, %s, %s, %s)",
                    (user_id, user_name, count, emails_str, timestamp)
                )

    async def get_purchase_requests(self):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT * FROM purchase_requests")
                return await cur.fetchall()

    async def add_recharge_request(self, user_id, user_name, op_number, amount, method, timestamp):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO recharge_requests (user_id, user_name, op_number, amount, method, timestamp) VALUES (%s, %s, %s, %s, %s, %s)",
                    (user_id, user_name, op_number, amount, method, timestamp)
                )

    async def get_recharge_requests(self):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT * FROM recharge_requests")
                return await cur.fetchall()

    async def delete_recharge_request(self, op_number):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM recharge_requests WHERE op_number = %s", (op_number,))

    async def add_processed_recharge_request(self, user_id, user_name, op_number, amount, status, reject_reason, timestamp):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO processed_recharge_requests (user_id, user_name, op_number, amount, status, reject_reason, timestamp) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (user_id, user_name, op_number, amount, status, reject_reason, timestamp)
                )

    async def get_processed_recharge_requests(self):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT * FROM processed_recharge_requests")
                return await cur.fetchall()

    async def get_cumulative_sales(self):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT seller_name, COUNT(*) FROM accounts WHERE status = 'sold' GROUP BY seller_name"
                )
                rows = await cur.fetchall()
                return {row[0]: row[1] for row in rows}

    async def get_daily_sales(self, date_str):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT seller_name, COUNT(*) FROM accounts WHERE status = 'sold' AND sold_at = %s GROUP BY seller_name",
                    (date_str,)
                )
                rows = await cur.fetchall()
                return {row[0]: row[1] for row in rows}

    async def get_sales_dates(self):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT DISTINCT sold_at FROM accounts WHERE sold_at IS NOT NULL ORDER BY sold_at DESC"
                )
                rows = await cur.fetchall()
                return [row[0] for row in rows]

##############################################################################
# دوال حساب الرصيد المتاح
##############################################################################
async def get_available_balance(db: Database, user_id):
    return await db.get_user_balance(user_id)

##############################################################################
# دوال بناء لوحات المفاتيح
##############################################################################
def build_cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("إلغاء", callback_data="cancel_request")]])

def build_back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("رجوع", callback_data="back")]])

def build_account_keyboard(account, current_admin_id=None):
    account_id = account["id"]
    if account.get("verifier_id"):
        if current_admin_id and account["verifier_id"] == current_admin_id:
            verify_button = InlineKeyboardButton("إلغاء التحقق", callback_data=f"cancelverify_{account_id}")
            approve_button = InlineKeyboardButton("الموافقة", callback_data=f"approve_{account_id}")
            reject_button = InlineKeyboardButton("رفض الحساب", callback_data=f"reject_{account_id}")
        else:
            verify_button = InlineKeyboardButton("جارٍ التحقق", callback_data="noop")
            approve_button = InlineKeyboardButton("الموافقة (مقفول)", callback_data="noop")
            reject_button = InlineKeyboardButton("رفض الحساب (مقفول)", callback_data="noop")
    else:
        verify_button = InlineKeyboardButton("جارٍ التحقق", callback_data=f"verify_{account_id}")
        approve_button = InlineKeyboardButton("الموافقة", callback_data=f"approve_{account_id}")
        reject_button = InlineKeyboardButton("رفض الحساب", callback_data=f"reject_{account_id}")
    keyboard = [
        [approve_button, reject_button],
        [verify_button]
    ]
    return InlineKeyboardMarkup(keyboard)

def build_recharge_request_keyboard(request):
    op_number = request.get("op_number", "unknown")
    approve_button = InlineKeyboardButton("الموافقة على الشحن", callback_data=f"approve_recharge_{op_number}")
    reject_button = InlineKeyboardButton("رفض الشحن", callback_data=f"reject_recharge_{op_number}")
    contact_button = InlineKeyboardButton("تواصل", callback_data=f"contact_user_{request['user_id']}")
    keyboard = [[approve_button, reject_button], [contact_button]]
    return InlineKeyboardMarkup(keyboard)

def build_purchase_request_keyboard(request):
    contact_button = InlineKeyboardButton("تواصل", callback_data=f"contact_user_{request['user_id']}")
    keyboard = [[contact_button]]
    return InlineKeyboardMarkup(keyboard)

def build_main_menu_keyboard(balance):
    keyboard = [
        [InlineKeyboardButton("شراء الإيميلات", callback_data="buy_emails")],
        [InlineKeyboardButton("شحن البوت", callback_data="recharge_bot")],
        [InlineKeyboardButton("سحب الرصيد", callback_data="withdraw_request")],
        [InlineKeyboardButton("استبدال/استرجاع الايميل", callback_data="email_exchange_request")],
        [InlineKeyboardButton("تواصل", callback_data="contact")]
    ]
    return InlineKeyboardMarkup(keyboard)

# --- START OF MODIFIED FUNCTION build_admin_menu_keyboard ---

def build_admin_menu_keyboard(admin_id: int):
    """
    تنشئ لوحة مفاتيح قائمة الأدمن، مع إظهار أزرار إضافية للأدمن المميزين.
    """
    # القائمة الأساسية من الأزرار التي يراها جميع الأدمن
    base_buttons = [
        ["إضافة ايميلات جديدة"],
        # ["التحقق من طلبات شحن الرصيد", "طلبات استبدال/استرجاع الحسابات"], # <- تم نقلها للشرط
        ["عرض طلبات شراء الايميلات"],
        ["تعيين سعر دولار البايير", "تحديث سعر الحساب"],
        ["تغيير كلمة المرور", "عدد المستخدمين"],
        ["تغيير رمز الكاش"],
        ["تعيين رابط القناة", "تقارير مبيعات يومية"],
        ["إرسال رسالة لجميع المستخدمين"]
    ]

    # التحقق مما إذا كان الأدمن الحالي ضمن قائمة الأدمن المميزين
    if admin_id in PRIVILEGED_ADMIN_IDS:
        # الأزرار الإضافية التي تظهر فقط للأدمن المميزين
        privileged_buttons_row = [["طلبات سحب الرصيد", "طلبات استبدال/استرجاع الحسابات"]
                                  
        ]
        # إدراج صف الأزرار الإضافية في الموضع الثاني (بعد الصف الأول)
        base_buttons.insert(1, privileged_buttons_row)

    return ReplyKeyboardMarkup(base_buttons, resize_keyboard=True)



##############################################################################
# دوال إرسال إشعارات الأدمن
##############################################################################
async def send_admin_status(context: CallbackContext, db: Database):
    # حساب تاريخ اليوم الحالي بتوقيت سوريا +3
    sold_date = (datetime.now() + timedelta(hours=3)).strftime("%Y-%m-%d")
    # الحصول على المبيعات اليومية لكل صاحب حزمة في التاريخ الحالي
    daily_sales = await db.get_daily_sales(sold_date)
    recharge_reqs = await db.get_recharge_requests()
    lines = []
    # إضافة أصحاب الحزم الذين لديهم مبيعات (أي عدد > 0)
    for seller, count in daily_sales.items():
        if count > 0:
            lines.append(f"عدد الإيميلات المشتراة من {seller}: {count}")
    lines.append(f"عدد طلبات الشحن قيد الانتظار: {len(recharge_reqs)}")
    admin_msg = "\n".join(lines)
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=admin_msg)
        except Exception as e:
            logging.error("Error sending admin status to %s: %s", admin_id, e)


##############################################################################
# دوال تقارير المبيعات اليومية (لواجهة الأدمن)
##############################################################################
async def show_daily_report_menu(update: Update, context: CallbackContext):
    db: Database = context.bot_data["db"]
    dates = await db.get_sales_dates()
    if not dates:
        await safe_edit_message_text(update, context, "لا توجد مبيعات حتى الآن.")
        return
    # إنشاء لوحة مفاتيح تحتوي على زر لكل يوم (أول زر هو اليوم الأحدث)
    buttons = []
    for d in dates:
        buttons.append([InlineKeyboardButton(d, callback_data=f"daily_report_{d}")])
    kb = InlineKeyboardMarkup(buttons)
    await safe_edit_message_text(update, context, "اختر اليوم للحصول على تقرير المبيعات:", reply_markup=kb)

async def daily_report_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    # استخراج التاريخ الصحيح من البيانات (مثلاً "daily_report_2025-04-06" تُصبح "2025-04-06")
    date_str = query.data.replace("daily_report_", "")
    db: Database = context.bot_data["db"]
    sales_details = await db.get_daily_sales_details(date_str)
    if not sales_details:
        text = f"لا توجد مبيعات بتاريخ {date_str}"
    else:
        lines = [f"تقرير المبيعات ليوم {date_str}:"]
        for seller, emails in sales_details.items():
            lines.append(f"عدد الإيميلات المشتراة من {seller}: {len(emails)}")
            for email in emails:
                lines.append(email)
        recharge_reqs = await db.get_recharge_requests()

        text = "\n".join(lines)
    await query.answer()
    await safe_edit_message_text(update, context, text)



##############################################################################
# دوال إرسال البث للأدمن
##############################################################################
async def ask_broadcast_message(update: Update, context: CallbackContext):
    await update.message.reply_text("أدخل الرسالة التي تريد إرسالها لجميع المستخدمين:")
    return ADMIN_BROADCAST

async def process_broadcast_message(update: Update, context: CallbackContext):
    message_text = update.message.text.strip()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("إلغاء", callback_data="cancel_broadcast"),
         InlineKeyboardButton("ارسال", callback_data="confirm_broadcast")]
    ])
    await update.message.reply_text(
        f"سيتم إرسال الرسالة التالية لجميع المستخدمين:\n\n{message_text}",
        reply_markup=kb
    )
    context.user_data["broadcast_message"] = message_text
    return ADMIN_BROADCAST

async def confirm_broadcast_message(update: Update, context: CallbackContext):
    message_text = context.user_data.get("broadcast_message")
    db: Database = context.bot_data["db"]
    user_ids = await db.get_all_users()
    success = 0
    for uid in user_ids:
        try:
            await context.bot.send_message(chat_id=uid, text=message_text)
            success += 1
        except Exception as e:
            logging.error("Error broadcasting to user %s: %s", uid, e)
    await safe_edit_message_text(update, context, f"تم إرسال الرسالة إلى {success} من المستخدمين.")
    return ConversationHandler.END

async def cancel_broadcast(update: Update, context: CallbackContext):
    await safe_edit_message_text(update, context, "تم إلغاء إرسال الرسالة.")
    return ConversationHandler.END

##############################################################################
# دوال التحقق من اشتراك المستخدم
##############################################################################
async def check_subscription(update: Update, context: CallbackContext) -> bool:
    db: Database = context.bot_data["db"]
    channel_link = await db.get_setting("channel_link")
    if not channel_link or not channel_link.startswith("https://"):
        return True
    try:
        username = channel_link.rstrip("/").split("/")[-1]
        chat_id = username if username.startswith('@') else "@" + username
        member = await context.bot.get_chat_member(chat_id, update.effective_user.id)
        return member.status not in ["left", "kicked"]
    except Exception as e:
        logging.error("Error checking subscription: %s", e)
        return False

async def check_subscription_callback(update: Update, context: CallbackContext):
    await update.callback_query.answer()  # تأكيد الاستلام للمستخدم
    if await check_subscription(update, context):
        await send_main_menu(update, context)
    else:
        await update.callback_query.edit_message_text(
            "لم تقم بالاشتراك بعد. يرجى الاشتراك في القناة ثم اضغط على 'تم الاشتراك'."
        )

async def send_main_menu(update: Update, context: CallbackContext):
    db: Database = context.bot_data["db"]
    if not await check_subscription(update, context):
        channel_link = await db.get_setting("channel_link")
        inline_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("اشترك في القناة", url=channel_link)],
            [InlineKeyboardButton("تم الاشتراك", callback_data="check_subscription")]
        ])
        msg = "يرجى الاشتراك في القناة لتتمكن من استخدام البوت"
        if update.callback_query and update.callback_query.message:
            await safe_edit_message_text(update, context, msg, reply_markup=inline_kb)
        else:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=msg, reply_markup=inline_kb)
        return
    user_id = update.effective_user.id
    available_balance = await get_available_balance(db, user_id)
    inline_kb = build_main_menu_keyboard(available_balance)
    welcome_text = f"مرحباً بك!\nرصيدك الحالي هو: {available_balance} ليرة سورية.\nاختر ماذا تريد أن تفعل:"
    persistent_kb = ReplyKeyboardMarkup([["الرئيسية"]], resize_keyboard=True)
    if update.callback_query and update.callback_query.message:
        await safe_edit_message_text(update, context, welcome_text, reply_markup=inline_kb)
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=welcome_text, reply_markup=inline_kb)

#############################################
# دوال للمستخدم: طلب سحب الرصيد
#############################################
# دوال للمستخدم: طلب سحب الرصيد

async def start_withdrawal_request(update: Update, context: CallbackContext):
    """
    يبدأ طلب سحب الرصيد ويطلب من المستخدم إدخال رقم الحساب.
    """
    await safe_edit_message_text(update, context, "يرجى إدخال رقم الحساب الخاص بك:")
    return WITHDRAW_ACCOUNT

async def process_withdrawal_account(update: Update, context: CallbackContext):
    """
    يستقبل رقم الحساب من المستخدم ويطلب منه إدخال قيمة المبلغ المراد سحبه.
    """
    account_number = update.message.text.strip()
    context.user_data["withdraw_account_number"] = account_number
    await update.message.reply_text("الرجاء إدخال قيمة المبلغ المراد سحبه:")
    return WITHDRAW_AMOUNT

async def process_withdrawal_amount(update: Update, context: CallbackContext):
    """
    يستقبل قيمة المبلغ من المستخدم ويضيف طلب السحب إلى قاعدة البيانات.
    """
    user_id = update.effective_user.id
    try:
        amount = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("الرجاء إدخال مبلغ صالح (رقم).")
        return WITHDRAW_AMOUNT

    account_number = context.user_data.get("withdraw_account_number", "غير محدد")
    timestamp = (datetime.now() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    db: Database = context.bot_data["db"]
    await db.add_withdrawal(user_id, update.effective_user.full_name, account_number, amount, "سيرياتيل كاش")
    await update.message.reply_text("تم تقديم طلب سحب الرصيد، سيتم مراجعته من قبل الأدمن.")
    return ConversationHandler.END

#############################################
# دوال للمستخدم: طلب استبدال/استرجاع الإيميل
#############################################
async def start_email_exchange_request(update: Update, context: CallbackContext):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("استرجاع سعر الايميل", callback_data="refund_email")],
        [InlineKeyboardButton("استبدال الايميل", callback_data="exchange_email")],
        # أضف زر رجوع لتحسين تجربة المستخدم
        [InlineKeyboardButton("رجوع", callback_data="back")]
    ])
    # استخدم safe_edit_message_text بدلاً من update.message.reply_text إذا كان ناتجاً عن callback
    await safe_edit_message_text(update, context, "اختر ما تريد القيام به:", reply_markup=keyboard)
    # لا تقم بإعادة ConversationHandler.END هنا. دع الـ callback التالي يكون نقطة الدخول للمحادثة.
    # return ConversationHandler.END  # <--- قم بإزالة أو التعليق على هذا السطر

# دالة معالجة طلب استرجاع سعر الايميل
async def refund_email_request(update: Update, context: CallbackContext):
    """
    بعد اختيار المستخدم لطلب استرجاع سعر الايميل، يتم طلب إرسال قائمة الإيميلات المطلوب استرجاع سعرها.
    """
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("يرجى إرسال قائمة الإيميلات التي تريد استرجاع سعرها، كل ايميل في سطر منفصل:")
    return REFUND_EMAILS_STATE

async def process_refund_emails(update: Update, context: CallbackContext):
    """
    يستقبل القائمة ويقوم بالتالي:
      - البحث عن الإيميلات في جدول accounts التي تحمل الحالة 'sold'
      - تغيير حالتها إلى 'refunded'
      - إضافة قيمة سعر الحساب إلى رصيد المستخدم لكل ايميل تم استرجاعه
    """
    db: Database = context.bot_data["db"]
    account_price = float(await db.get_setting("account_price"))
    user_id = update.effective_user.id
    emails = [line.strip() for line in update.message.text.splitlines() if line.strip()]
    refunded_count = 0
    for email in emails:
        async with db.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE accounts SET status='refunded' WHERE purchased_emails LIKE %s AND status='sold'",
                    (f"%{email}%",)
                )
                if cur.rowcount > 0:
                    refunded_count += 1
    if refunded_count > 0:
        current_balance = await db.get_user_balance(user_id)
        new_balance = current_balance + (refunded_count * account_price)
        await db.update_user_balance(user_id, new_balance)
        await update.message.reply_text(f"تم استرجاع سعر {refunded_count} ايميل وإضافته إلى رصيدك. رصيدك الجديد: {new_balance}")
    else:
        await update.message.reply_text("لم يتم العثور على إيميلات تطابق البيانات المدخلة أو ربما تم استرجاعها سابقاً.")
    # حفظ الطلب لدى الأدمن للمراجعة (اختياري)
    timestamp = (datetime.now() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    await db.add_email_exchange_request(user_id, update.effective_user.full_name, emails, "refund", timestamp)
    return ConversationHandler.END

# دالة معالجة طلب استبدال الإيميل
async def exchange_email_request(update: Update, context: CallbackContext):
    """
    بعد اختيار المستخدم لاستبدال الإيميل، يتم طلب إرسال قائمة الإيميلات التي يريد استبدالها.
    """
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("يرجى إرسال قائمة الإيميلات التي تريد استبدالها، كل ايميل في سطر منفصل:")
    return EXCHANGE_EMAILS_STATE

async def process_exchange_emails(update: Update, context: CallbackContext):
    """
    يستقبل قائمة الإيميلات ويقوم بالآتي:
      - لكل ايميل، يتم استدعاء دالة purchase_emails للحصول على ايميل جديد بنفس ترتيب الشراء
      - يتم إرسال الإيميل الجديد للمستخدم
    """
    db: Database = context.bot_data["db"]
    emails = [line.strip() for line in update.message.text.splitlines() if line.strip()]
    exchanged_results = []
    for email in emails:
        new_email_list = await db.purchase_emails(1)
        if new_email_list:
            new_email = new_email_list[0]  # (email, password, seller)
            exchanged_results.append(new_email)
        else:
            exchanged_results.append(("غير متوفر", "غير متوفر", ""))
    if exchanged_results:
        msg_lines = []
        for e in exchanged_results:
            new_email, pwd, seller = e
            msg_lines.append(f"الايميل:\n{new_email}\nكلمة المرور:\n{pwd}")
        await update.message.reply_text("تم استبدال الإيميلات الجديدة:\n\n" + "\n\n".join(msg_lines))
    else:
        await update.message.reply_text("لا توجد إيميلات متوفرة للاستبدال حالياً")
    # حفظ الطلب لدى الأدمن للمراجعة (اختياري)
    timestamp = (datetime.now() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    await db.add_email_exchange_request(update.effective_user.id, update.effective_user.full_name, emails, "exchange", timestamp)
    return ConversationHandler.END


#############################################
# دوال الأدمن لعرض الطلبات
#############################################
def build_withdrawal_request_keyboard(request_id, user_id):
    """
    تنشئ لوحة مفاتيح لطلب السحب مع أزرار قبول، رفض، وتواصل.
    """
    approve_button = InlineKeyboardButton("✅ قبول السحب", callback_data=f"approve_withdrawal_{request_id}")
    reject_button = InlineKeyboardButton("❌ رفض السحب", callback_data=f"reject_withdrawal_{request_id}")
    contact_button = InlineKeyboardButton("👤 تواصل", callback_data=f"contact_user_{user_id}")
    keyboard = [
        [approve_button, reject_button],
        [contact_button]
    ]
    return InlineKeyboardMarkup(keyboard)

async def approve_withdrawal_callback(update: Update, context: CallbackContext):
    """
    يعالج الموافقة على طلب سحب الرصيد.
    """
    query = update.callback_query
    await query.answer("جارٍ معالجة الموافقة...")

    try:
        withdrawal_id = int(query.data.split("_")[-1])
    except (IndexError, ValueError):
        await query.edit_message_text("خطأ: معرف طلب السحب غير صالح.")
        return

    db: Database = context.bot_data["db"]
    admin_id = update.effective_user.id

    # جلب تفاصيل الطلب للتأكد من حالته ولمعرفة المستخدم والمبلغ
    withdrawal_request = await db.get_withdrawal_by_id(withdrawal_id)

    if not withdrawal_request:
        await safe_edit_message_text(update, context, f"لم يتم العثور على طلب السحب (ID: {withdrawal_id}).")
        return

    # التحقق من أن الطلب لا يزال معلقًا
    # الافتراض: status هو الفهرس 6
    if withdrawal_request[6] != 'pending':
        await safe_edit_message_text(update, context, f"تمت معالجة هذا الطلب (ID: {withdrawal_id}) مسبقاً.")
        return

    # الحصول على معرف المستخدم والمبلغ
    # الافتراض: user_id=1, amount=4
    user_id = withdrawal_request[1]
    amount_to_withdraw = withdrawal_request[4]
    user_name = withdrawal_request[2] # للاستخدام في الرسائل

    # التحقق من رصيد المستخدم قبل الخصم
    current_balance = await db.get_user_balance(user_id)
    if current_balance < amount_to_withdraw:
        await safe_edit_message_text(update, context, f"خطأ: رصيد المستخدم {user_name} (ID: {user_id}) غير كافٍ ({current_balance:.2f} ل.س) لإتمام عملية السحب بقيمة {amount_to_withdraw:.2f} ل.س.\nتم رفض الطلب تلقائياً.")
        # تحديث حالة الطلب إلى مرفوض بسبب الرصيد
        await db.update_withdrawal_status(withdrawal_id, 'rejected', reject_reason="رصيد غير كافٍ", verifier_id=admin_id)
        # إعلام المستخدم بالرفض التلقائي
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"تم رفض طلب سحب الرصيد الخاص بك بقيمة {amount_to_withdraw:.2f} ل.س تلقائياً بسبب عدم كفاية الرصيد.\nرصيدك الحالي: {current_balance:.2f} ل.س."
            )
        except Exception as e:
            logging.error(f"فشل إرسال رسالة الرفض التلقائي للمستخدم {user_id}: {e}")
        return # إنهاء المعالجة هنا

    # الرصيد كافٍ، قم بخصم المبلغ وتحديث الحالة
    new_balance = current_balance - amount_to_withdraw
    await db.update_user_balance(user_id, new_balance)
    await db.update_withdrawal_status(withdrawal_id, 'approved', verifier_id=admin_id)

    # تعديل الرسالة الأصلية لتأكيد الموافقة
    original_message_text = query.message.text # الحصول على النص الأصلي
    await safe_edit_message_text(
        update,
        context,
        f"{original_message_text}\n\n---\n✅ تمت الموافقة بواسطة الأدمن (ID: {admin_id}).\nتم خصم {amount_to_withdraw:.2f} ل.س من رصيد المستخدم {user_name}."
    )

    # إعلام المستخدم بالموافقة والرصيد الجديد
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"✅ تمت الموافقة على طلب سحب الرصيد الخاص بك.\nتم خصم {amount_to_withdraw:.2f} ل.س.\nرصيدك الجديد هو: {new_balance:.2f} ل.س."
        )
    except Exception as e:
        logging.error(f"فشل إرسال رسالة الموافقة للمستخدم {user_id}: {e}")

    # (اختياري) إرسال إشعار حالة للأدمن
    await send_admin_status(context, db)
    
async def reject_withdrawal_callback(update: Update, context: CallbackContext):
    """
    يبدأ عملية رفض طلب السحب ويطلب من الأدمن إرسال السبب.
    (نقطة الدخول لمحادثة الرفض)
    """
    query = update.callback_query
    await query.answer()

    try:
        withdrawal_id = int(query.data.split("_")[-1])
    except (IndexError, ValueError):
        await query.edit_message_text("خطأ: معرف طلب السحب غير صالح.")
        return ConversationHandler.END # إنهاء المحادثة إذا كان المعرف خاطئًا

    # تخزين معرف الطلب في بيانات المستخدم (للأدمن) لاستخدامه في الخطوة التالية
    context.user_data['reject_withdrawal_id'] = withdrawal_id

    # التحقق من أن الطلب لا يزال معلقًا قبل طلب السبب
    db: Database = context.bot_data["db"]
    withdrawal_request = await db.get_withdrawal_by_id(withdrawal_id)
    if not withdrawal_request or withdrawal_request[6] != 'pending':
         await safe_edit_message_text(update, context, f"لا يمكن رفض الطلب (ID: {withdrawal_id}). قد يكون تمت معالجته مسبقاً.")
         # تنظيف بيانات المستخدم
         if 'reject_withdrawal_id' in context.user_data:
            del context.user_data['reject_withdrawal_id']
         return ConversationHandler.END

    await safe_edit_message_text(
        update, context,
        f"طلب السحب (ID: {withdrawal_id}):\nالرجاء إرسال سبب الرفض (نص أو صورة).",
        reply_markup=build_cancel_keyboard() # لوحة مفاتيح الإلغاء
    )
    return REJECT_WITHDRAWAL_REASON # الانتقال إلى حالة انتظار السبب

async def process_reject_withdrawal_reason(update: Update, context: CallbackContext):
    """
    يعالج سبب الرفض المُرسل من الأدمن (نص أو صورة).
    (معالج الحالة REJECT_WITHDRAWAL_REASON)
    """
    # استعادة معرف الطلب من بيانات المستخدم (للأدمن)
    withdrawal_id = context.user_data.get('reject_withdrawal_id')
    if not withdrawal_id:
        await update.message.reply_text("خطأ: لا يمكن العثور على معرف طلب السحب للمعالجة.")
        return ConversationHandler.END

    db: Database = context.bot_data["db"]
    admin_id = update.effective_user.id

    # جلب تفاصيل الطلب مرة أخرى للتأكد ولمعرفة المستخدم
    withdrawal_request = await db.get_withdrawal_by_id(withdrawal_id)
    if not withdrawal_request:
        await update.message.reply_text(f"خطأ: لم يتم العثور على طلب السحب (ID: {withdrawal_id}) للمعالجة.")
        # تنظيف بيانات المستخدم
        del context.user_data['reject_withdrawal_id']
        return ConversationHandler.END

    # التحقق مرة أخرى من أن الطلب لا يزال معلقًا
    if withdrawal_request[6] != 'pending':
        await update.message.reply_text(f"خطأ: تم معالجة هذا الطلب (ID: {withdrawal_id}) بالفعل أثناء إدخال السبب.")
        del context.user_data['reject_withdrawal_id']
        return ConversationHandler.END

    user_id = withdrawal_request[1]
    user_name = withdrawal_request[2]
    amount_withdrawn = withdrawal_request[4] # المبلغ الذي كان سيُسحب

    reject_reason_text = ""
    photo_file_id = None

    if update.message.photo:
        photo_file_id = update.message.photo[-1].file_id
        # يمكنك استخدام الكابشن كسبب نصي إضافي إذا أردت
        reject_reason_text = update.message.caption if update.message.caption else "تم إرفاق صورة كسبب للرفض."
        await update.message.reply_text("تم استلام الصورة كسبب للرفض.")
    elif update.message.text:
        reject_reason_text = update.message.text.strip()
        await update.message.reply_text("تم استلام النص كسبب للرفض.")
    else:
        await update.message.reply_text("نوع الرسالة غير مدعوم كسبب للرفض. الرجاء إرسال نص أو صورة.")
        return REJECT_WITHDRAWAL_REASON # البقاء في نفس الحالة لإعادة المحاولة

    # تحديث حالة طلب السحب في قاعدة البيانات إلى "مرفوض" مع السبب والمُراجع
    await db.update_withdrawal_status(withdrawal_id, 'rejected', reject_reason=reject_reason_text, verifier_id=admin_id)

    # إعلام المستخدم بالرفض والسبب
    rejection_message_to_user = f"❌ تم رفض طلب سحب الرصيد الخاص بك بقيمة {amount_withdrawn:.2f} ل.س.\nالسبب: {reject_reason_text}"
    try:
        if photo_file_id:
            await context.bot.send_photo(chat_id=user_id, photo=photo_file_id, caption=rejection_message_to_user)
        else:
            await context.bot.send_message(chat_id=user_id, text=rejection_message_to_user)
    except Exception as e:
        logging.error(f"فشل إرسال رسالة الرفض للمستخدم {user_id}: {e}")
        await update.message.reply_text(f"تم رفض الطلب، ولكن فشل إعلام المستخدم {user_name} (ID: {user_id}).")

    # تأكيد الرفض للأدمن الذي قام بالإجراء
    await update.message.reply_text(f"تم بنجاح رفض طلب السحب (ID: {withdrawal_id}) للمستخدم {user_name}.\nتم إرسال السبب للمستخدم.")

    # تنظيف بيانات المستخدم وإنهاء المحادثة
    del context.user_data['reject_withdrawal_id']
    # يمكنك أيضاً تعديل الرسالة الأصلية التي تحتوي على أزرار الطلب للإشارة إلى أنه تم رفضه، ولكن هذا قد يكون معقداً إذا كان هناك عدة رسائل
    await send_admin_status(context, db) # (اختياري) تحديث حالة الأدمن

    return ConversationHandler.END


async def show_withdrawal_requests(update: Update, context: CallbackContext):
    """
    يعرض لجميع الأدمن طلبات سحب الرصيد المعلقة مع أزرار للإجراء.
    """
    db: Database = context.bot_data["db"]
    # جلب الطلبات المعلقة فقط
    requests = await db.get_withdrawals_by_status("pending")

    if not requests:
        await update.message.reply_text("لا توجد طلبات سحب رصيد معلقة حالياً.")
        return

    await update.message.reply_text("طلبات سحب الرصيد المعلقة:")
    for req in requests:
        # الحصول على البيانات من الصف (Tuple) - تأكد من صحة الفهارس بناءً على استعلامك
        # الافتراض: id=0, user_id=1, user_name=2, account_code=3, amount=4, method=5, status=6, created_at=9 (من تعريف الجدول)
        req_id = req[0]
        user_id = req[1]
        user_name = req[2] if req[2] else "غير متوفر"
        account_code = req[3] if req[3] else "غير محدد"
        amount = req[4]
        method = req[5] if req[5] else "غير محدد"
        # تنسيق التاريخ - تأكد من أن req[9] هو بالفعل created_at وهو كائن datetime أو None
        created_at_dt = req[9]
        # تاريخ الإنشاء قد يكون في فهرس مختلف حسب استعلام get_withdrawals_by_status
        # ابحث عن فهرس created_at في مخرجات get_withdrawals_by_status
        # لنفترض أنه الفهرس 9 كما في تعريف الجدول
        try:
            # حاول العثور على created_at في الفهرس الصحيح
            # إذا كان الفهرس مختلفاً، قم بتغيير req[9]
            created_at_dt = req[9]
            date_str = created_at_dt.strftime("%Y-%m-%d %H:%M") if isinstance(created_at_dt, datetime) else "غير محدد"
        except IndexError:
             # في حال لم يكن الفهرس 9 موجوداً، حاول البحث في الفهارس الشائعة الأخرى أو اتركه غير محدد
             try:
                 created_at_dt = req[7] # الفهرس المستخدم في الكود الأصلي
                 date_str = created_at_dt.strftime("%Y-%m-%d %H:%M") if isinstance(created_at_dt, datetime) else "غير محدد"
             except (IndexError, AttributeError):
                 date_str = "غير محدد" # Fallback

        msg = (
            f"طلب سحب رصيد (ID: {req_id}):\n"
            f"👤 المستخدم: {user_name} (ID: {user_id})\n"
            f"💳 رقم الحساب: {account_code}\n"
            f"💰 المبلغ: {amount:.2f} ل.س\n" # تنسيق المبلغ
            f" Lطريقة السحب: {method}\n"
            f" Lالتاريخ: {date_str}"
        )
        # إنشاء لوحة المفاتيح الخاصة بهذا الطلب
        keyboard = build_withdrawal_request_keyboard(req_id, user_id)
        await update.message.reply_text(msg, reply_markup=keyboard)
        
async def show_email_exchange_requests(update: Update, context: CallbackContext):
    """
    يعرض لمدير الأدمن جميع طلبات استبدال أو استرجاع سعر الإيميلات المسجلة في جدول email_exchange_requests.
    """
    db: Database = context.bot_data["db"]
    requests = await db.get_email_exchange_requests()
    if not requests:
        await update.message.reply_text("لا توجد طلبات استبدال/استرجاع حسابات حالياً.")
    else:
        for req in requests:
            # يفترض أن ترتيب الأعمدة في جدول email_exchange_requests هو:
            # id, user_id, user_name, emails, request_type, timestamp
            msg = (f"طلب {req[4]}:\n"
                   f"المستخدم: {req[2]}\n"
                   f"الإيميلات:\n{req[3]}\n"
                   f"التاريخ: {req[5]}")
            await update.message.reply_text(msg)
    return

##############################################################################
# دالة start_command – بدء البوت وتسجيل المستخدم
##############################################################################
# --- START OF MODIFIED FUNCTION start_command ---

async def start_command(update: Update, context: CallbackContext):
    db: Database = context.bot_data["db"]
    user_id = update.effective_user.id
    user_full_name = update.effective_user.full_name

    # التأكد من إضافة المستخدم أو تحديث اسمه إذا تغير
    await db.add_user(user_id, user_full_name)

    if user_id in ADMIN_IDS:
        # بناء لوحة المفاتيح بناءً على معرف الأدمن الحالي
        admin_keyboard = build_admin_menu_keyboard(admin_id=user_id)
        await update.message.reply_text(
            f"مرحباً بك أدمن {user_full_name}!\nاختر ماذا تريد أن تفعل:",
            reply_markup=admin_keyboard
        )
    else:
        # التحقق من الاشتراك للمستخدمين العاديين
        if not await check_subscription(update, context):
            channel_link = await db.get_setting("channel_link")
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("اشترك في القناة", url=channel_link)],
                [InlineKeyboardButton("تم الاشتراك", callback_data="check_subscription")]
            ])
            await update.message.reply_text("يرجى الاشتراك في القناة لتتمكن من استخدام البوت", reply_markup=keyboard)
        else:
            # إرسال القائمة الرئيسية للمستخدم العادي
            await send_main_menu(update, context)

# --- END OF MODIFIED FUNCTION start_command ---

##############################################################################
# دوال المستخدم – شراء الإيميلات وشحن البوت
##############################################################################
async def process_sell_account(update: Update, context: CallbackContext):
    await update.message.reply_text("تم استقبال تفاصيل الحساب للبيع. (تنفيذ الدالة الأصلية هنا)")
    return ConversationHandler.END

async def buy_emails_callback(update: Update, context: CallbackContext):
    db: Database = context.bot_data["db"]
    available_count = await db.count_available_emails()
    account_price = await db.get_setting("account_price")
    account_password = await db.get_setting("account_password")
    text = (f"عدد الإيميلات المتوفرة في قاعدة البيانات: {available_count}\n\n"
            f"سعر الحساب: {account_price}\n"
            f"كلمة المرور الافتراضية: {account_password}\n\n"
            "اختر كمية الشراء:")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("ايميل واحد", callback_data="buy_1"),
         InlineKeyboardButton("5 ايميلات", callback_data="buy_5")],
        [InlineKeyboardButton("10 ايميلات", callback_data="buy_10"),
         InlineKeyboardButton("20 ايميلات", callback_data="buy_20")],
        [InlineKeyboardButton("30 ايميلات", callback_data="buy_30"),
         InlineKeyboardButton("رجوع", callback_data="buy_back")]
    ])
    await safe_edit_message_text(update, context, text, reply_markup=keyboard)
    return BUY_EMAILS

async def buy_emails_choice_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    data = query.data
    db: Database = context.bot_data["db"]
    user_id = update.effective_user.id
    account_price = float(await db.get_setting("account_price"))

    if data == "buy_back":
        await send_main_menu(update, context)
        return ConversationHandler.END
    elif data.startswith("buy_") and data != "buy_by_balance":
        count = int(data.split("_")[1])
    elif data == "buy_by_balance":
        balance = await db.get_user_balance(user_id)
        count = math.floor(balance / account_price)
        if count <= 0:
            await query.answer("رصيدك غير كافي لشراء ايميلات.", show_alert=True)
            await send_main_menu(update, context)
            return ConversationHandler.END
    else:
        await query.answer("خيار غير معروف")
        return BUY_EMAILS

    available_emails = await db.count_available_emails()
    if available_emails < count:
        await query.answer("لا توجد كمية كافية من الإيميلات المتوفرة.", show_alert=True)
        await send_main_menu(update, context)
        return ConversationHandler.END

    total_cost = count * account_price
    balance = await db.get_user_balance(user_id)
    if balance < total_cost:
        await query.answer("رصيدك غير كافي لشراء هذا العدد من الإيميلات.", show_alert=True)
        await send_main_menu(update, context)
        return ConversationHandler.END

    new_balance = balance - total_cost
    await db.update_user_balance(user_id, new_balance)

    # استدعاء دالة purchase_emails المُعدلة التي تُطبق ترتيب تناوبي بين أصحاب الحزم
    accounts = await db.purchase_emails(count)
    await query.delete_message()

    if accounts:
        # إعادة تجميع الإيميلات حسب صاحب الحزمة لتجميعها في الرسالة للمستخدم
        groups = {}
        for email, pwd, seller in accounts:
            groups.setdefault(seller, []).append((email, pwd))
        messages = []
        for seller, email_list in groups.items():

            for email, pwd in email_list:
                messages.append(f"الايميل:\n{email}\nكلمة المرور:\n{pwd}")
            messages.append("")  # فاصل بين الحزم
        emails_text = "\n".join(messages)

        header = f"لقد قمت بشراء {count} حساب مقابل {total_cost} ليرة سورية.\n\n"
        await context.bot.send_message(
            chat_id=user_id,
            text=f"{header}تم شراء الإيميلات التالية:\n\n{emails_text}",
            parse_mode="HTML"
        )
    else:
        await context.bot.send_message(chat_id=user_id, text="لا توجد إيميلات متوفرة حالياً")

    timestamp = (datetime.now() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    await db.add_purchase_request(user_id, update.effective_user.full_name, count, [f"{email}|{pwd}" for email, pwd, _ in accounts], timestamp)

    cumulative = await db.get_cumulative_sales()
    lines = []

    admin_notification = "\n".join(lines)

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=(f"قام المستخدم {user_id} - {update.effective_user.full_name} بشراء {count} ايميل مقابل {total_cost} ليرة سورية.\n"
                      f"{admin_notification}")
            )
        except Exception as e:
            logging.error("Error sending purchase notification to admin %s: %s", admin_id, e)

    await send_admin_status(context, db)
    await send_main_menu(update, context)
    return ConversationHandler.END



async def recharge_bot_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    db: Database = context.bot_data["db"]
    payeer_rate = await db.get_setting("payeer_rate")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("سيرياتيل كاش", callback_data="recharge_syriatel_cash")],
        [InlineKeyboardButton("بايير", callback_data="recharge_payeer")],
        [InlineKeyboardButton("إلغاء", callback_data="cancel_request")]
    ])
    syriatel_cash = await db.get_setting("account_syriatelcash")
    await safe_edit_message_text(update, context, f"اختر طريقة شحن البوت:\nرمز حساب سيرياتيل كاش الشخصي:\n{syriatel_cash}\nعنوان محفظة payeer:\nP1056913846\nسعر الدولار للبايير حالياً هو {payeer_rate}", reply_markup=keyboard)
    return

async def recharge_payeer_callback(update: Update, context: CallbackContext):
    db: Database = context.bot_data["db"]
    payeer_rate = await db.get_setting("payeer_rate")
    msg = f"سعر الدولار للبايير حالياً هو {payeer_rate} ليرة.\nالرجاء إرسال رقم العملية :"
    await safe_edit_message_text(update, context, msg, reply_markup=build_cancel_keyboard())
    return RECHARGE_SERIAL_NUMBER_PAYEER

async def recharge_syriatel_cash_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_edit_message_text(update, context, "الرجاء إرسال رقم العملية :", reply_markup=build_cancel_keyboard())
    return RECHARGE_SERIAL_NUMBER

async def process_recharge_serial_number(update: Update, context: CallbackContext):
    op_number = update.message.text.strip()
    context.user_data["op_number"] = op_number
    await update.message.reply_text("الرجاء إرسال مبلغ الشحن:", reply_markup=build_cancel_keyboard())
    return RECHARGE_AMOUNT

async def process_recharge_amount(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    amount_text = update.message.text.strip()
    try:
        amount = float(amount_text)
    except ValueError:
        await update.message.reply_text("الرجاء إدخال مبلغ صالح (رقم).")
        return RECHARGE_AMOUNT
    op_number = context.user_data.get("op_number", "غير محدد")
    timestamp = (datetime.now() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    db: Database = context.bot_data["db"]
    await db.add_recharge_request(user_id, update.effective_user.full_name, op_number, amount, "سيرياتيل كاش", timestamp)
    await update.message.delete()
    await context.bot.send_message(chat_id=user_id, text="تم استلام طلب الشحن، سيتم مراجعته من قبل الأدمن.")
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=(f"طلب شحن جديد من المستخدم {user_id} - {update.effective_user.full_name}:\n"
                      f"رقم العملية: {op_number}\n"
                      f"المبلغ: {amount} ليرة سورية\n"
                      f"طريقة الشحن: سيرياتيل كاش")
            )
        except Exception as e:
            logging.error("Error sending recharge notification to admin %s: %s", admin_id, e)
    await send_admin_status(context, db)
    await send_main_menu(update, context)
    return ConversationHandler.END

async def process_recharge_serial_number_payeer(update: Update, context: CallbackContext):
    op_number = update.message.text.strip()
    context.user_data["op_number"] = op_number
    db: Database = context.bot_data["db"]
    payeer_rate = await db.get_setting("payeer_rate")
    await update.message.reply_text(
        f"سعر الدولار للبايير حالياً هو {payeer_rate} ليرة.\nالرجاء إرسال مبلغ الشحن بالدولار:",
        reply_markup=build_cancel_keyboard()
    )
    return RECHARGE_AMOUNT_PAYEER

async def process_recharge_amount_payeer(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    amount_text = update.message.text.strip()
    try:
        usd_amount = float(amount_text)
    except ValueError:
        await update.message.reply_text("الرجاء إدخال مبلغ صالح (رقم).")
        return RECHARGE_AMOUNT_PAYEER
    db: Database = context.bot_data["db"]
    payeer_rate = float(await db.get_setting("payeer_rate"))
    converted_amount = usd_amount * payeer_rate
    op_number = context.user_data.get("op_number", "غير محدد")
    timestamp = (datetime.now() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    await db.add_recharge_request(user_id, update.effective_user.full_name, op_number, converted_amount, "بايير", timestamp)
    await update.message.delete()
    await context.bot.send_message(
        chat_id=user_id,
        text=(f"تم استلام طلب الشحن عبر بايير.\n"
              f"المبلغ المحول: {converted_amount} ليرة سورية (تم تحويل {usd_amount} دولار حسب السعر {payeer_rate}).\n"
              "سيتم مراجعته من قبل الأدمن.")
    )
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=(f"طلب شحن جديد عبر بايير من المستخدم {user_id} - {update.effective_user.full_name}:\n"
                      f"رقم العملية: {op_number}\n"
                      f"المبلغ بالدولار: {usd_amount}\n"
                      f"المبلغ المحول إلى ليرة: {converted_amount}\n"
                      f"طريقة الشحن: بايير\n"
                      f"التاريخ: {timestamp}")
            )
        except Exception as e:
            logging.error("Error sending recharge notification to admin %s: %s", admin_id, e)
    await send_admin_status(context, db)
    await send_main_menu(update, context)
    return ConversationHandler.END

##############################################################################
# دوال الأدمن الجديدة
##############################################################################
# تعديل دالة إضافة الإيميلات بحيث يكون التنسيق:
# السطر الأول: اسم صاحب الحزمة
# السطر الثاني: كلمة المرور
# السطر الثالث وما بعده: الإيميلات
async def ask_admin_add_emails(update: Update, context: CallbackContext):
    await update.message.reply_text("يرجى إرسال بيانات الحزمة بالشكل التالي:\nالسطر الأول: اسم صاحب الحزمة\nالسطر الثاني: كلمة المرور\nالسطر الثالث وما بعده: قائمة الإيميلات (كل إيميل في سطر)")
    return ADMIN_ADD_EMAILS

async def process_admin_add_emails(update: Update, context: CallbackContext):
    db: Database = context.bot_data["db"]
    seller_id = update.effective_user.id
    data = update.message.text.strip().splitlines()
    if len(data) < 3:
        await update.message.reply_text("الرجاء التأكد من إدخال اسم صاحب الحزمة وكلمة المرور وقائمة الإيميلات.")
        return ConversationHandler.END
    seller_name = data[0].strip()
    password = data[1].strip()
    emails = data[2:]
    email_pattern = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
    added = 0
    duplicates = 0
    for email in emails:
        email = email.strip()
        if email and email_pattern.match(email):
            async with db.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT COUNT(*) FROM accounts WHERE details = %s OR (purchased_emails IS NOT NULL AND purchased_emails LIKE %s)",
                        (email, f"%{email}%")
                    )
                    row = await cur.fetchone()
                    count = row[0] if row else 0
            if count > 0:
                duplicates += 1
                continue
            account_id = await db.add_account(seller_id, seller_name, email, password)
            await db.update_account_status(account_id, "approved", sold_at=None)
            added += 1
    message = f"تمت إضافة {added} إيميل(ات) جديدة من {seller_name}."
    if duplicates:
        message += f" وتم رفض {duplicates} حساب(ات) مكررة."
    await update.message.reply_text(message)
    return ConversationHandler.END


async def ask_update_price(update: Update, context: CallbackContext):
    await update.message.reply_text("الرجاء إدخال السعر الجديد للحساب:")
    return ADMIN_UPDATE_PRICE

async def process_update_price(update: Update, context: CallbackContext):
    new_price_text = update.message.text.strip()
    try:
        new_price = float(new_price_text)
    except ValueError:
        await update.message.reply_text("الرجاء إدخال رقم صالح.")
        return ADMIN_UPDATE_PRICE
    db: Database = context.bot_data["db"]
    await db.set_setting("account_price", new_price)
    await update.message.reply_text(f"تم تحديث سعر الحساب إلى {new_price} ليرة سورية.")
    return ConversationHandler.END

async def ask_admin_change_password(update: Update, context: CallbackContext):
    await update.message.reply_text("الرجاء إدخال كلمة المرور الجديدة:")
    return ADMIN_CHANGE_PASSWORD

async def ask_admin_change_syriatelcash(update: Update, context: CallbackContext):
    await update.message.reply_text("الرجاء إدخال رمز الكاش الجديد:")
    return ADMIN_CHANGE_SYRIATELCASH


async def process_admin_change_password(update: Update, context: CallbackContext):
    new_password = update.message.text.strip()
    db: Database = context.bot_data["db"]
    await db.set_setting("account_password", new_password)
    await update.message.reply_text(f"تم تغيير كلمة المرور إلى: {new_password}")
    return ConversationHandler.END

async def process_ADMIN_CHANGE_SYRIATELCASH(update: Update, context: CallbackContext):
    syriatel_cash = update.message.text.strip()
    db: Database = context.bot_data["db"]
    await db.set_setting("account_syriatelcash", syriatel_cash)
    await update.message.reply_text(f"تم تغيير رمز الكاش إلى: {syriatel_cash}")
    return ConversationHandler.END

async def ask_set_channel(update: Update, context: CallbackContext):
    await update.message.reply_text("أدخل رابط القناة التي يجب على المستخدم الاشتراك بها:")
    return ADMIN_SET_CHANNEL

async def process_set_channel(update: Update, context: CallbackContext):
    channel_link = update.message.text.strip()
    db: Database = context.bot_data["db"]
    if not channel_link.startswith("https://"):
        await update.message.reply_text("يرجى إدخال رابط قناة صالح يبدأ ب https://")
        return ADMIN_SET_CHANNEL
    await db.set_setting("channel_link", channel_link)
    await update.message.reply_text("تم تحديث رابط القناة بنجاح.")
    return ConversationHandler.END

async def ask_set_payeer_rate(update: Update, context: CallbackContext):
    await update.message.reply_text("الرجاء إدخال سعر الدولار للبايير:")
    return ADMIN_SET_PAYEER_RATE

async def process_set_payeer_rate(update: Update, context: CallbackContext):
    rate_text = update.message.text.strip()
    try:
        rate = float(rate_text)
    except ValueError:
        await update.message.reply_text("الرجاء إدخال رقم صالح لسعر الدولار للبايير.")
        return ADMIN_SET_PAYEER_RATE
    db: Database = context.bot_data["db"]
    await db.set_setting("payeer_rate", rate)
    await update.message.reply_text(f"تم تحديث سعر الدولار للبايير إلى {rate} ليرة سورية.")
    return ConversationHandler.END

async def ask_reject_recharge_reason(update: Update, context: CallbackContext):
    op_number = update.callback_query.data.split("_")[-1]
    context.user_data["reject_op_number"] = op_number
    await safe_edit_message_text(update, context, "أدخل سبب رفض الشحن (يمكنك إرسال صورة أو نص):", reply_markup=build_cancel_keyboard())
    return REJECT_RECHARGE_REASON

async def process_reject_recharge_reason(update: Update, context: CallbackContext):
    op_number = context.user_data.get("reject_op_number")
    db: Database = context.bot_data["db"]
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT * FROM recharge_requests WHERE op_number = %s", (op_number,))
            request_found = await cur.fetchone()
    if not request_found:
        await update.message.reply_text("طلب الشحن غير موجود!")
        return ConversationHandler.END
    user_id = request_found[1]
    amount = request_found[4]
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        reject_reason = f"صورة: {file_id}"
    else:
        reject_reason = update.message.text.strip()
    await db.delete_recharge_request(op_number)
    timestamp = (datetime.now() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    await db.add_processed_recharge_request(user_id, request_found[2], op_number, amount, "مرفوض", reject_reason, timestamp)
    try:
        if update.message.photo:
            await context.bot.send_photo(
                chat_id=user_id,
                photo=file_id,
                caption=f"تم رفض طلب الشحن الخاص بك.\nسبب الرفض: {reject_reason}"
            )
        else:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"تم رفض طلب الشحن الخاص بك.\nسبب الرفض: {reject_reason}"
            )
    except Exception as e:
        logging.error("Error sending rejection message to user %s: %s", user_id, e)
    await safe_edit_message_text(update, context, f"تم رفض طلب الشحن للمستخدم {user_id}.")
    await send_admin_status(context, db)
    return ConversationHandler.END
async def handle_admin_buttons(update: Update, context: CallbackContext):
    db: Database = context.bot_data["db"]
    text = update.message.text
    # إذا كان الأدمن لا يملك صلاحية رؤية الأزرار الإضافية، يمكنه الوصول للأزرار الأخرى فقط
    if text == "إضافة ايميلات جديدة":
        await update.message.reply_text("يرجى إرسال بيانات الحزمة كما هو موضح:\nالسطر الأول: اسم صاحب الحزمة\nالسطر الثاني: كلمة المرور\nالسطر الثالث وما بعده: قائمة الإيميلات (كل إيميل في سطر)")
        return
    elif text == "التحقق من طلبات شحن الرصيد":
        # عند الضغط على هذا الزر يتم عرض رسالة ثابتة مع تفاصيل الطلب وثلاثة أزرار
        fixed_msg = ("طلب سحب رصيد:\n"
                     "المستخدم: Profit Plex\n"
                     "رقم الحساب: 7668\n"
                     "المبلغ: 85000.0\n"
                     "طريقة السحب: سيرياتيل كاش\n"
                     "التاريخ: None")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("قبول", callback_data="approve_custom")],
            [InlineKeyboardButton("رفض", callback_data="reject_custom")],
            [InlineKeyboardButton("تواصل", callback_data="contact_custom")]
        ])
        await update.message.reply_text(fixed_msg, reply_markup=kb)
    elif text == "طلبات استبدال/استرجاع الحسابات":
        # عرض طلبات استبدال واسترجاع الحسابات (يمكن تعديل الرسالة أو طريقة العرض حسب الحاجة)
        exchange_reqs = await db.get_email_exchange_requests()
        if not exchange_reqs:
            await update.message.reply_text("لا توجد طلبات استبدال/استرجاع حسابات حالياً.")
        else:
            for req in exchange_reqs:
                # يفترض ترتيب الأعمدة في جدول email_exchange_requests كالتالي:
                # id, user_id, user_name, emails, request_type, timestamp
                msg = (f"طلب {req[4]}:\n"
                       f"المستخدم: {req[2]}\n"
                       f"الإيميلات:\n{req[3]}\n"
                       f"التاريخ: {req[5]}")
                await update.message.reply_text(msg)
    elif text == "عرض طلبات شراء الايميلات":
        purchase_reqs = await db.get_purchase_requests()
        if not purchase_reqs:
            await update.message.reply_text("لا يوجد طلبات شراء ايميلات حتى الآن.")
        else:
            for req in purchase_reqs:
                msg = (f"طلب شراء ايميلات:\n"
                       f"المستخدم: {req[1]} - {req[2]}\n"
                       f"عدد الإيميلات: {req[3]}\n"
                       f"الإيميلات: {req[4]}\n"
                       f"التاريخ: {req[5]}")
                kb = build_purchase_request_keyboard({"user_id": req[1], "user_name": req[2]})
                await update.message.reply_text(msg, reply_markup=kb)
    elif text == "عدد المستخدمين":
        users = await db.get_all_users()
        await update.message.reply_text(f"عدد المستخدمين الذين استخدموا البوت: {len(users)}")
    elif text == "تحديث سعر الحساب":
        await update.message.reply_text("الرجاء إدخال السعر الجديد للحساب:")
        return ADMIN_UPDATE_PRICE
    elif text == "تغيير كلمة المرور":
        await update.message.reply_text("الرجاء إدخال كلمة المرور الجديدة:")
        return ADMIN_CHANGE_PASSWORD
    elif text == "تغيير رمز الكاش":
        await update.message.reply_text("الرجاء ادخال رمز الكاش الجديد:")
        return ADMIN_CHANGE_SYRIATELCASH
    elif text == "إرسال رسالة لجميع المستخدمين":
        await update.message.reply_text("ابدأ بكتابة الرسالة التي تريد إرسالها لجميع المستخدمين:")
        return ADMIN_BROADCAST
    elif text == "تقارير مبيعات يومية":
        await show_daily_report_menu(update, context)
    elif text == "تعيين رابط القناة":
        await update.message.reply_text("أدخل رابط القناة التي يجب على المستخدم الاشتراك بها:")
        return ADMIN_SET_CHANNEL
    elif text == "تعيين سعر دولار البايير":
        await update.message.reply_text("الرجاء إدخال سعر الدولار للبايير:")
        return ADMIN_SET_PAYEER_RATE
    else:
        await update.message.reply_text("خيار غير معروف!")

async def approve_recharge_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    data = query.data  # يفترض أن يكون الشكل "approve_recharge_{op_number}"
    op_number = data.split("_")[-1]
    db: Database = context.bot_data["db"]

    # استخراج الطلب من جدول recharge_requests باستخدام op_number
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT * FROM recharge_requests WHERE op_number = %s", (op_number,))
            request_found = await cur.fetchone()

    if not request_found:
        await query.answer("طلب الشحن غير موجود!")
        return

    # استخراج معلومات الطلب
    # نفترض ترتيب الأعمدة في recharge_requests هو:
    # id, user_id, user_name, op_number, amount, method, timestamp
    user_id = request_found[1]
    amount = request_found[4]

    # حساب الرصيد الجديد للمستخدم
    balance = await db.get_user_balance(user_id)
    new_balance = balance + amount

    # حذف الطلب من جدول recharge_requests
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM recharge_requests WHERE op_number = %s", (op_number,))

    # تحديث رصيد المستخدم في قاعدة البيانات
    await db.update_user_balance(user_id, new_balance)

    # تسجيل الطلب في جدول processed_recharge_requests مع الحالة "مقبول"
    timestamp = (datetime.now() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    await db.add_processed_recharge_request(user_id, request_found[2], op_number, amount, "مقبول", "", timestamp)

    # إرسال إشعار للمستخدم بإضافة الرصيد الجديد
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"تمت الموافقة على طلب الشحن.\nتمت إضافة {amount} ليرة إلى رصيدك.\nرصيدك الجديد: {new_balance} ليرة سورية."
        )
    except Exception as e:
        logging.error("Error sending approval message to user %s: %s", user_id, e)

    # تعديل الرسالة الأصلية المُرتبطة بالـ Callback لتوضيح الموافقة
    await safe_edit_message_text(
        update,
        context,
        f"تمت الموافقة على الشحن.\nتم إضافة {amount} ليرة إلى رصيد المستخدم {user_id}."
    )

    # إرسال إشعار عام للأدمن بحالة الرصيد
    await send_admin_status(context, db)

async def reject_recharge_callback(update: Update, context: CallbackContext):
    return await ask_reject_recharge_reason(update, context)

async def cancel_request_callback(update: Update, context: CallbackContext):
    await safe_edit_message_text(update, context, "تم إلغاء العملية.", reply_markup=build_back_keyboard())
    return ConversationHandler.END

async def cancel(update: Update, context: CallbackContext):
    if update.message:
        await update.message.reply_text("تم إلغاء العملية.", reply_markup=build_back_keyboard())
    elif update.callback_query:
        await safe_edit_message_text(update, context, "تم إلغاء العملية.", reply_markup=build_back_keyboard())
    return ConversationHandler.END

def noop_callback(update: Update, context: CallbackContext):
    update.callback_query.answer()

async def contact_callback(update: Update, context: CallbackContext):
    await safe_edit_message_text(update, context, "للتواصل مع الدعم:\n@DigiX13")

async def contact_user_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    data = query.data
    user_id = data.split("_")[-1]
    contact_link = f"tg://user?id={user_id}"
    await query.answer()
    await safe_edit_message_text(update, context, f"يمكنك التواصل مع المستخدم عبر الرابط التالي:\n{contact_link}")

async def user_start_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    data = query.data
    db: Database = context.bot_data["db"]
    if data == "sell_account":
        account_price = float(await db.get_setting("account_price"))
        account_password = await db.get_setting("account_password")
        text = (
            f"الرجاء إنشاء حساب جديد وإرسال تفاصيل الحساب.\n"
            f"السعر الحالي للحساب: {account_price} ليرة سورية.\n"
            f"استخدم كلمة السر: {account_password}\n"
            f"يمكنك وضع أكثر من ايميل بحيث تضع كل ايميل على سطر جديد"
        )
        await safe_edit_message_text(update, context, text, reply_markup=build_cancel_keyboard())
        return SELL_ACCOUNT
    elif data == "sold_accounts":
        await safe_edit_message_text(update, context, "اختر الحالة لعرض حساباتك:", reply_markup=build_account_keyboard({}))
        return ConversationHandler.END
    else:
        
        return ConversationHandler.END

##############################################################################
# الدالة الرئيسية لتشغيل البوت
##############################################################################

async def main():
    # إعدادات التسجيل الأساسية لرؤية الأخطاء
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    logger = logging.getLogger(__name__)

    # تأكد من استبدال TOKEN الخاص بك هنا
    if not TOKEN:
        logger.error("خطأ: لم يتم تعيين TOKEN البوت.")
        return

    application = Application.builder().token(TOKEN).build()

    # إعداد قاعدة البيانات وتخزينها في بيانات البوت
    db = Database() # افترض أن كلاس Database معرف أعلاه
    try:
        await db.init_db()
        application.bot_data["db"] = db
        logger.info("تم الاتصال بقاعدة البيانات بنجاح.")
    except Exception as e:
        logger.error(f"فشل الاتصال بقاعدة البيانات: {e}")
        return # لا يمكن تشغيل البوت بدون قاعدة بيانات

    # --- تسجيل Conversation Handlers أولاً (من الأكثر تحديداً إلى الأقل) ---

    # 1. محادثة رفض طلب السحب (جديدة)
    reject_withdrawal_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(reject_withdrawal_callback, pattern="^reject_withdrawal_")],
        states={
            REJECT_WITHDRAWAL_REASON: [MessageHandler(filters.TEXT | filters.PHOTO & ~filters.COMMAND, process_reject_withdrawal_reason)]
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel_request_callback, pattern="^cancel_request$")
            ],
        map_to_parent={ ConversationHandler.END: -1 }
    )
    application.add_handler(reject_withdrawal_conv_handler, group=1) # استخدام المجموعات لتحديد الأولويات

    # 2. محادثة رفض طلب الشحن
    reject_recharge_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(reject_recharge_callback, pattern="^reject_recharge_")],
        states={
            REJECT_RECHARGE_REASON: [MessageHandler(filters.ALL & ~filters.COMMAND, process_reject_recharge_reason)]
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel_request_callback, pattern="^cancel_request$")
            ],
        map_to_parent={ ConversationHandler.END: -1 }
    )
    application.add_handler(reject_recharge_conv_handler, group=1)

    # 3. محادثات المستخدم المحددة (سحب، استرجاع، استبدال)
    withdraw_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_withdrawal_request, pattern="^withdraw_request$")],
        states={
            WITHDRAW_ACCOUNT: [ MessageHandler(filters.TEXT & ~filters.COMMAND, process_withdrawal_account) ],
            WITHDRAW_AMOUNT: [ MessageHandler(filters.TEXT & ~filters.COMMAND, process_withdrawal_amount) ]
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel_request_callback, pattern="^cancel_request$")
            ],
        map_to_parent={ ConversationHandler.END: -1 }
    )
    application.add_handler(withdraw_conv_handler, group=1)

    refund_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(refund_email_request, pattern="^refund_email$")],
        states={
            REFUND_EMAILS_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_refund_emails)]
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel_request_callback, pattern="^cancel_request$")
            ],
        map_to_parent={ ConversationHandler.END: -1 }
    )
    application.add_handler(refund_conv_handler, group=1)

    exchange_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(exchange_email_request, pattern="^exchange_email$")],
        states={
            EXCHANGE_EMAILS_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_exchange_emails)]
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel_request_callback, pattern="^cancel_request$")
            ],
        map_to_parent={ ConversationHandler.END: -1 }
    )
    application.add_handler(exchange_conv_handler, group=1)

    # 4. محادثات الأدمن
    admin_update_price_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS) & filters.Regex("^تحديث سعر الحساب$"), ask_update_price)],
        states={ ADMIN_UPDATE_PRICE: [MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS) & ~filters.COMMAND, process_update_price)] },
        fallbacks=[CommandHandler("cancel", cancel)],
        map_to_parent={ConversationHandler.END: -1}
    )
    application.add_handler(admin_update_price_conv_handler, group=1)

    admin_change_password_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS) & filters.Regex("^تغيير كلمة المرور$"), ask_admin_change_password)],
        states={ ADMIN_CHANGE_PASSWORD: [MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS) & ~filters.COMMAND, process_admin_change_password)] },
        fallbacks=[CommandHandler("cancel", cancel)],
        map_to_parent={ConversationHandler.END: -1}
    )
    application.add_handler(admin_change_password_conv_handler, group=1)

    admin_change_syriatlcash_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS) & filters.Regex("^تغيير رمز الكاش$"), ask_admin_change_syriatelcash)],
        states={ ADMIN_CHANGE_SYRIATELCASH: [MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS) & ~filters.COMMAND, process_ADMIN_CHANGE_SYRIATELCASH)] },
        fallbacks=[CommandHandler("cancel", cancel)],
        map_to_parent={ConversationHandler.END: -1}
    )
    application.add_handler(admin_change_syriatlcash_conv_handler, group=1)

    admin_broadcast_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS) & filters.Regex("^إرسال رسالة لجميع المستخدمين$"), ask_broadcast_message)],
        states={
            ADMIN_BROADCAST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_broadcast_message),
                CallbackQueryHandler(confirm_broadcast_message, pattern="^confirm_broadcast$"),
                CallbackQueryHandler(cancel_broadcast, pattern="^cancel_broadcast$")
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        map_to_parent={ConversationHandler.END: -1}
    )
    application.add_handler(admin_broadcast_conv_handler, group=1)

    admin_set_channel_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS) & filters.Regex("^تعيين رابط القناة$"), ask_set_channel)],
        states={ ADMIN_SET_CHANNEL: [MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS) & ~filters.COMMAND, process_set_channel)] },
        fallbacks=[CommandHandler("cancel", cancel)],
        map_to_parent={ConversationHandler.END: -1}
    )
    application.add_handler(admin_set_channel_conv_handler, group=1)

    admin_set_payeer_rate_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS) & filters.Regex("^تعيين سعر دولار البايير$"), ask_set_payeer_rate)],
        states={ ADMIN_SET_PAYEER_RATE: [MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS) & ~filters.COMMAND, process_set_payeer_rate)] },
        fallbacks=[CommandHandler("cancel", cancel)],
        map_to_parent={ConversationHandler.END: -1}
    )
    application.add_handler(admin_set_payeer_rate_conv_handler, group=1)

    admin_add_emails_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS) & filters.Regex("^إضافة ايميلات جديدة$"), ask_admin_add_emails)],
        states={ ADMIN_ADD_EMAILS: [MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS) & ~filters.COMMAND, process_admin_add_emails)] },
        fallbacks=[CommandHandler("cancel", cancel)],
        map_to_parent={ConversationHandler.END: -1}
    )
    application.add_handler(admin_add_emails_conv_handler, group=1)

    # 5. محادثة المستخدم العامة (شراء، شحن) - تأتي بعد المحادثات الأكثر تحديداً
    user_conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(buy_emails_callback, pattern="^buy_emails$"),
            CallbackQueryHandler(recharge_bot_callback, pattern="^recharge_bot$"),
            CallbackQueryHandler(recharge_syriatel_cash_callback, pattern="^recharge_syriatel_cash$"),
            CallbackQueryHandler(recharge_payeer_callback, pattern="^recharge_payeer$")
        ],
        states={
            BUY_EMAILS: [CallbackQueryHandler(buy_emails_choice_callback, pattern="^buy_(1|5|10|20|30|by_balance|back)$")],
            RECHARGE_SERIAL_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_recharge_serial_number)],
            RECHARGE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_recharge_amount)],
            RECHARGE_SERIAL_NUMBER_PAYEER: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_recharge_serial_number_payeer)],
            RECHARGE_AMOUNT_PAYEER: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_recharge_amount_payeer)]
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel_request_callback, pattern="^cancel_request$")
            ],
        allow_reentry=True,
        map_to_parent={ ConversationHandler.END: -1 }
    )
    application.add_handler(user_conv_handler, group=2) # مجموعة أقل أولوية من المجموعة 1

    # --- تسجيل المعالجات العامة (Commands, Callbacks غير المرتبطة بمحادثات, Messages) ---

    # Commands
    application.add_handler(CommandHandler("start", start_command, block=False)) # السماح للمعالجات الأخرى بالعمل

    # Callbacks (التي لا تبدأ محادثات أو يتم التعامل معها كـ entry_points أعلاه)
    application.add_handler(CallbackQueryHandler(approve_withdrawal_callback, pattern="^approve_withdrawal_")) # قبول السحب (جديد)
    application.add_handler(CallbackQueryHandler(approve_recharge_callback, pattern="^approve_recharge_"))   # قبول الشحن
    application.add_handler(CallbackQueryHandler(contact_callback, pattern="^contact$"))
    application.add_handler(CallbackQueryHandler(contact_user_callback, pattern="^contact_user_"))
    application.add_handler(CallbackQueryHandler(check_subscription_callback, pattern="^check_subscription$"))
    application.add_handler(CallbackQueryHandler(lambda u, c: asyncio.create_task(send_main_menu(u, c)), pattern="^back$"))
    application.add_handler(CallbackQueryHandler(daily_report_callback, pattern="^daily_report_"))
    # Callback لعرض أزرار استبدال/استرجاع (لم يعد يبدأ محادثة بنفسه)
    application.add_handler(CallbackQueryHandler(start_email_exchange_request, pattern="^email_exchange_request$"))
    application.add_handler(CallbackQueryHandler(noop_callback, pattern="^noop$")) # للمهام التي لا تفعل شيئاً

    # Message Handlers (غير المرتبطة بمحادثات)
    application.add_handler(MessageHandler(filters.Regex("^الرئيسية$") & ~filters.COMMAND, send_main_menu))
    # أدمن: عرض الطلبات (يجب أن تأتي قبل معالج الأزرار العام للأدمن)
    application.add_handler(MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS) & filters.Regex("^طلبات سحب الرصيد$") & ~filters.COMMAND, show_withdrawal_requests)) # تأكد أنها النسخة المحدثة
    application.add_handler(MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS) & filters.Regex("^طلبات استبدال/استرجاع الحسابات$") & ~filters.COMMAND, show_email_exchange_requests))
    application.add_handler(MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS) & filters.Regex("^التحقق من طلبات شحن الرصيد$") & ~filters.COMMAND, handle_admin_buttons)) # مثال إذا كان handle_admin_buttons يعالج هذا
    application.add_handler(MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS) & filters.Regex("^عرض طلبات شراء الايميلات$") & ~filters.COMMAND, handle_admin_buttons)) # مثال
    application.add_handler(MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS) & filters.Regex("^عدد المستخدمين$") & ~filters.COMMAND, handle_admin_buttons)) # مثال
    application.add_handler(MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS) & filters.Regex("^حالة طلبات الشحن$") & ~filters.COMMAND, handle_admin_buttons)) # مثال
    application.add_handler(MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS) & filters.Regex("^تقارير مبيعات يومية$") & ~filters.COMMAND, handle_admin_buttons)) # مثال


    # معالج الأزرار النصية العام للأدمن (يأتي أخيراً لمعالجات نص الأدمن)
    # يلتقط أي نص من الأدمن لا يتطابق مع Regex للمحادثات أو الأوامر المحددة أعلاه
    application.add_handler(MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS) & ~filters.COMMAND, handle_admin_buttons), group=10) # أولوية منخفضة جداً


    logger.info("تم تسجيل جميع المعالجات. بدء تشغيل البوت...")
    await application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    # التأكد من تشغيل الكود بشكل غير متزامن
    import nest_asyncio
    nest_asyncio.apply()
    try:
        asyncio.run(main())
    except RuntimeError as e:
        # معالجة الخطأ الشائع عند إيقاف التشغيل بـ Ctrl+C في بعض البيئات
        if "Cannot run the event loop while another loop is running" in str(e):
            print("تم إيقاف البوت.")
        else:
            raise e
    except KeyboardInterrupt:
        print("تم إيقاف البوت يدوياً.")
    except Exception as e:
        print(f"حدث خطأ غير متوقع عند تشغيل البوت: {e}")