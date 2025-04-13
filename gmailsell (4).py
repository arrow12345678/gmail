import asyncio
import aiosqlite
import logging
import math
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

# عدد العمليات المتزامنة المسموح بها للوصول إلى قاعدة البيانات
MAX_CONCURRENT_DB = 10

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
class Database:
    def __init__(self, db_path="data.db"):
        self.db_path = db_path
        self.conn = None
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_DB)
        self.settings_cache = {}

    async def init_db(self):
        async with self.semaphore:
            self.conn = await aiosqlite.connect(self.db_path)
            # جدول الحسابات مع عمود جديد لتخزين كلمة المرور وعمود sold_at لتاريخ البيع
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seller_id INTEGER NOT NULL,
                    seller_name TEXT NOT NULL,
                    details TEXT NOT NULL,
                    password TEXT,
                    purchased_emails TEXT,
                    status TEXT NOT NULL DEFAULT 'approved',
                    reject_reason TEXT,
                    verifier_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    sold_at TEXT
                )
            """)
            # جدول السحوبات
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS withdrawals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    user_name TEXT NOT NULL,
                    account_code TEXT NOT NULL,
                    amount REAL NOT NULL,
                    method TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    reject_reason TEXT,
                    verifier_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # جدول الإعدادات
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            # جدول المستخدمين
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    user_name TEXT NOT NULL,
                    balance REAL NOT NULL DEFAULT 0
                )
            """)
            # جدول طلبات شراء الإيميلات
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS purchase_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    user_name TEXT,
                    count INTEGER,
                    emails TEXT,
                    timestamp TEXT
                )
            """)
            # جدول طلبات الشحن
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS recharge_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    user_name TEXT,
                    op_number TEXT,
                    amount REAL,
                    method TEXT,
                    timestamp TEXT
                )
            """)
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS processed_recharge_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    user_name TEXT,
                    op_number TEXT,
                    amount REAL,
                    status TEXT,
                    reject_reason TEXT,
                    timestamp TEXT
                )
            """)
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS email_swap_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    original_email TEXT NOT NULL,
                    seller_name TEXT NOT NULL,
                    new_email TEXT,
                    new_password TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await self.conn.commit()
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

    async def get_daily_sales_details(self, date_str):
            async with self.semaphore:
                cursor = await self.conn.execute(
                    "SELECT seller_name, purchased_emails FROM accounts WHERE status = 'sold' AND sold_at = ? ORDER BY created_at ASC",
                    (date_str,)
                )
                rows = await cursor.fetchall()
                result = {}
                for seller, purchased in rows:
                    # نفترض أن القيمة المخزنة هي بصيغة "email|password"
                    email = purchased.split("|")[0] if purchased and "|" in purchased else purchased
                    if seller in result:
                        result[seller].append(email)
                    else:
                        result[seller] = [email]
                return result


    async def get_setting(self, key):
        if key in self.settings_cache:
            return self.settings_cache[key]
        async with self.semaphore:
            cursor = await self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = await cursor.fetchone()
            value = row[0] if row else None
            if value is not None:
                self.settings_cache[key] = value
            return value

    async def set_setting(self, key, value):
        async with self.semaphore:
            await self.conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, str(value))
            )
            await self.conn.commit()
            self.settings_cache[key] = str(value)

    # دالة إضافة حساب تعدل لتأخذ seller_name من أول سطر من الرسالة
    async def add_account(self, seller_id, seller_name, details, password=None):
        async with self.semaphore:
            cursor = await self.conn.execute(
                "INSERT INTO accounts (seller_id, seller_name, details, password) VALUES (?, ?, ?, ?)",
                (seller_id, seller_name, details, password)
            )
            await self.conn.commit()
            return cursor.lastrowid

    async def update_account_status(self, account_id, status, reject_reason=None, verifier_id=None, sold_at=None):
        async with self.semaphore:
            await self.conn.execute(
                "UPDATE accounts SET status = ?, reject_reason = ?, verifier_id = ?, sold_at = ? WHERE id = ?",
                (status, reject_reason, verifier_id, sold_at, account_id)
            )
            await self.conn.commit()

    async def get_account_by_id(self, account_id):
        async with self.semaphore:
            cursor = await self.conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
            return await cursor.fetchone()

    async def get_accounts_by_status(self, status, seller_id=None):
        async with self.semaphore:
            if seller_id is not None:
                cursor = await self.conn.execute("SELECT * FROM accounts WHERE status = ? AND seller_id = ?", (status, seller_id))
            else:
                cursor = await self.conn.execute("SELECT * FROM accounts WHERE status = ?", (status,))
            return await cursor.fetchall()

    # تعديل دالة شراء الإيميلات لتحديد الإيميلات بناءً على زمن الإضافة (الأقدم أولاً)
    # كما يتم تحديث تاريخ البيع (sold_at) بالتاريخ الحالي بتوقيت سوريا +3 (بصيغة YYYY-MM-DD)
    async def purchase_emails(self, count):
        # جلب جميع الحسابات المتوفرة بترتيب زمن الإضافة
        async with self.semaphore:
            cursor = await self.conn.execute(
                "SELECT id, details, password, seller_name, created_at FROM accounts WHERE status = 'approved' AND (purchased_emails IS NULL OR purchased_emails = '') ORDER BY created_at ASC"
            )
            rows = await cursor.fetchall()
        if not rows:
            return []

        # تجميع الحسابات حسب صاحب الحزمة
        groups = {}
        for row in rows:
            seller = row[3]
            groups.setdefault(seller, []).append(row)

        # إنشاء قائمة مرتبة من أسماء أصحاب الحزم
        sorted_sellers = sorted(groups.keys())
        if not sorted_sellers:
            return []

        # استرجاع آخر مؤشر مستخدم من الإعدادات (افتراضيًا -1 إذا لم يُحفظ شيء)
        last_index_str = await self.get_setting("last_used_seller_index")
        last_index = int(last_index_str) if last_index_str and last_index_str.isdigit() else -1

        # تحديد نقطة البداية في القائمة بحيث يكون التالي بعد آخر مستخدم
        start_index = (last_index + 1) % len(sorted_sellers)
        # عمل قائمة دورية (rotated) بدءًا من نقطة البداية
        rotated_sellers = sorted_sellers[start_index:] + sorted_sellers[:start_index]

        # إنشاء ترتيب تناوبي: أخذ أول حساب من كل مجموعة ثم الثاني وهكذا
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

        # اختيار العدد المطلوب من الحسابات
        selected = interleaved[:count]

        # تحديث آخر مؤشر مستخدم بناءً على صاحب الحزمة للحساب الأخير الذي تم اختياره
        if selected:
            last_selected_seller = selected[-1][3]
            new_last_index = sorted_sellers.index(last_selected_seller)
            await self.set_setting("last_used_seller_index", new_last_index)

        sold_date = (datetime.now() + timedelta(hours=3)).strftime("%Y-%m-%d")
        accounts = []
        for row in selected:
            account_id, email, pwd, seller, _ = row
            accounts.append((email, pwd, seller))
            formatted = f"{email}|{pwd}"
            await self.conn.execute(
                "UPDATE accounts SET purchased_emails = ?, status = 'sold', details = '', password = '', sold_at = ? WHERE id = ?",
                (formatted, sold_date, account_id)
            )
        await self.conn.commit()
        return accounts


        # جلب جميع الحسابات المتوفرة بترتيب زمن الإضافة
        async with self.semaphore:
            cursor = await self.conn.execute(
                "SELECT id, details, password, seller_name, created_at FROM accounts WHERE status = 'approved' AND (purchased_emails IS NULL OR purchased_emails = '') ORDER BY created_at ASC"
            )
            rows = await cursor.fetchall()
        # تجميع الحسابات حسب صاحب الحزمة
        groups = {}
        for row in rows:
            seller = row[3]
            groups.setdefault(seller, []).append(row)
        # إعادة ترتيب الحسابات بطريقة التناوب بين المجموعات
        interleaved = []
        i = 0
        while True:
            added = False
            for seller, items in groups.items():
                if i < len(items):
                    interleaved.append(items[i])
                    added = True
            if not added:
                break
            i += 1
        selected = interleaved[:count]
        sold_date = (datetime.now() + timedelta(hours=3)).strftime("%Y-%m-%d")
        accounts = []
        for row in selected:
            account_id, email, pwd, seller, _ = row
            accounts.append((email, pwd, seller))
            formatted = f"{email}|{pwd}"
            await self.conn.execute(
                "UPDATE accounts SET purchased_emails = ?, status = 'sold', details = '', password = '', sold_at = ? WHERE id = ?",
                (formatted, sold_date, account_id)
            )
        await self.conn.commit()
        return accounts

    # إضافة طلب استبدال ايميل
    async def add_email_swap_request(self, user_id, original_email, seller_name):
        async with self.semaphore:
            cursor = await self.conn.execute(
                "INSERT INTO email_swap_requests (user_id, original_email, seller_name) VALUES (?, ?, ?)",
                (user_id, original_email, seller_name)
            )
            await self.conn.commit()
            return cursor.lastrowid

    # التحقق مما إذا كان قد تم إرسال طلب مسبق لنفس الايميل
    async def check_email_swap_request(self, user_id, original_email):
        async with self.semaphore:
            cursor = await self.conn.execute(
                "SELECT * FROM email_swap_requests WHERE user_id = ? AND original_email = ? AND status = 'pending'",
                (user_id, original_email)
            )
            row = await cursor.fetchone()
            return row is not None

    # استرجاع ايميل للاستبدال من نفس صاحب الحزمة
    async def retrieve_email_for_swap(self, seller_name):
        async with self.semaphore:
            cursor = await self.conn.execute(
                "SELECT id, details, password FROM accounts WHERE status = 'approved' AND seller_name = ? ORDER BY created_at ASC LIMIT 1",
                (seller_name,)
            )
            row = await cursor.fetchone()
            return row

    # تحديث طلب استبدال الايميل بعد الاستبدال
    async def update_email_swap_request(self, request_id, new_email, new_password):
        async with self.semaphore:
            await self.conn.execute(
                "UPDATE email_swap_requests SET new_email = ?, new_password = ?, status = 'swapped' WHERE id = ?",
                (new_email, new_password, request_id)
            )
            await self.conn.commit()
    async def count_available_emails(self):
        async with self.semaphore:
            cursor = await self.conn.execute(
                "SELECT COUNT(*) FROM accounts WHERE status = 'approved' AND (purchased_emails IS NULL OR purchased_emails = '')"
            )
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def add_withdrawal(self, user_id, user_name, account_code, amount, method):
        async with self.semaphore:
            cursor = await self.conn.execute(
                "INSERT INTO withdrawals (user_id, user_name, account_code, amount, method) VALUES (?, ?, ?, ?, ?)",
                (user_id, user_name, account_code, amount, method)
            )
            await self.conn.commit()
            return cursor.lastrowid

    async def update_withdrawal_status(self, withdrawal_id, status, reject_reason=None, verifier_id=None):
        async with self.semaphore:
            await self.conn.execute(
                "UPDATE withdrawals SET status = ?, reject_reason = ?, verifier_id = ? WHERE id = ?",
                (status, reject_reason, verifier_id, withdrawal_id)
            )
            await self.conn.commit()

    async def get_withdrawal_by_id(self, withdrawal_id):
        async with self.semaphore:
            cursor = await self.conn.execute("SELECT * FROM withdrawals WHERE id = ?", (withdrawal_id,))
            return await cursor.fetchone()

    async def get_withdrawals_by_status(self, status, user_id=None):
        async with self.semaphore:
            if user_id is not None:
                cursor = await self.conn.execute("SELECT * FROM withdrawals WHERE status = ? AND user_id = ?", (status, user_id))
            else:
                cursor = await self.conn.execute("SELECT * FROM withdrawals WHERE status = ?", (status,))
            return await cursor.fetchall()

    async def get_user_balance(self, user_id):
        async with self.semaphore:
            cursor = await self.conn.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            return float(row[0]) if row else 0.0

    async def update_user_balance(self, user_id, new_balance):
        async with self.semaphore:
            await self.conn.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_balance, user_id))
            await self.conn.commit()

    async def add_user(self, user_id, user_name):
        async with self.semaphore:
            await self.conn.execute("INSERT OR IGNORE INTO users (user_id, user_name, balance) VALUES (?, ?, ?)", (user_id, user_name, 0))
            await self.conn.commit()

    async def get_all_users(self):
        async with self.semaphore:
            cursor = await self.conn.execute("SELECT user_id FROM users")
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

    # دالة إضافة طلب شراء الإيميلات تبقى كما هي
    async def add_purchase_request(self, user_id, user_name, count, emails, timestamp):
        async with self.semaphore:
            emails_str = ",".join(emails) if emails else ""
            await self.conn.execute(
                "INSERT INTO purchase_requests (user_id, user_name, count, emails, timestamp) VALUES (?, ?, ?, ?, ?)",
                (user_id, user_name, count, emails_str, timestamp)
            )
            await self.conn.commit()

    async def get_purchase_requests(self):
        async with self.semaphore:
            cursor = await self.conn.execute("SELECT * FROM purchase_requests")
            return await cursor.fetchall()

    # دالة طلب الشحن تبقى كما هي
    async def add_recharge_request(self, user_id, user_name, op_number, amount, method, timestamp):
        async with self.semaphore:
            await self.conn.execute(
                "INSERT INTO recharge_requests (user_id, user_name, op_number, amount, method, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, user_name, op_number, amount, method, timestamp)
            )
            await self.conn.commit()

    async def get_recharge_requests(self):
        async with self.semaphore:
            cursor = await self.conn.execute("SELECT * FROM recharge_requests")
            return await cursor.fetchall()

    async def delete_recharge_request(self, op_number):
        async with self.semaphore:
            await self.conn.execute("DELETE FROM recharge_requests WHERE op_number = ?", (op_number,))
            await self.conn.commit()

    async def add_processed_recharge_request(self, user_id, user_name, op_number, amount, status, reject_reason, timestamp):
        async with self.semaphore:
            await self.conn.execute(
                "INSERT INTO processed_recharge_requests (user_id, user_name, op_number, amount, status, reject_reason, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, user_name, op_number, amount, status, reject_reason, timestamp)
            )
            await self.conn.commit()

    async def get_processed_recharge_requests(self):
        async with self.semaphore:
            cursor = await self.conn.execute("SELECT * FROM processed_recharge_requests")
            return await cursor.fetchall()

    # دالة للحصول على عدد المبيعات لكل صاحب حزمة (تجميع تراكمي)
    async def get_cumulative_sales(self):
        async with self.semaphore:
            cursor = await self.conn.execute(
                "SELECT seller_name, COUNT(*) FROM accounts WHERE status = 'sold' GROUP BY seller_name"
            )
            rows = await cursor.fetchall()
            # إرجاع قاموس: {seller_name: count}
            return {row[0]: row[1] for row in rows}

    # دالة للحصول على المبيعات اليومية (تجميع بحسب التاريخ وسلطات الحزمة)
    async def get_daily_sales(self, date_str):
        async with self.semaphore:
            cursor = await self.conn.execute(
                "SELECT seller_name, COUNT(*) FROM accounts WHERE status = 'sold' AND sold_at = ? GROUP BY seller_name",
                (date_str,)
            )
            rows = await cursor.fetchall()
            return {row[0]: row[1] for row in rows}

    # دالة للحصول على قائمة التواريخ التي تمت فيها عمليات البيع
    async def get_sales_dates(self):
        async with self.semaphore:
            cursor = await self.conn.execute(
                "SELECT DISTINCT sold_at FROM accounts WHERE sold_at IS NOT NULL ORDER BY sold_at DESC"
            )
            rows = await cursor.fetchall()
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
        # المجموعة الرئيسية
        [InlineKeyboardButton("شراء الإيميلات", callback_data="buy_emails")],
        [InlineKeyboardButton("شحن البوت", callback_data="recharge_bot")],
        [InlineKeyboardButton("تواصل", callback_data="contact")],
        # أزرار مشتركة بين الأدمن والمستخدم
        [InlineKeyboardButton("تبديل الحساب المقفول", callback_data="switch_locked_account"),
         InlineKeyboardButton("سحب الرصيد", callback_data="user_withdraw_balance")],
        # أزرار خاصة بواجهة المستخدم
        [InlineKeyboardButton("استبدال الايميل", callback_data="replace_email"),
         InlineKeyboardButton("استرجاع الرصيد", callback_data="retrieve_balance")]
    ]
    return InlineKeyboardMarkup(keyboard)


def build_admin_menu_keyboard():
    keyboard = [
        ["إضافة ايميلات جديدة",  "التحقق من طلبات شحن الرصيد"],
        ["عرض طلبات شراء الايميلات", "حالة طلبات الشحن"],
        ["تعيين سعر دولار البايير", "تحديث سعر الحساب"],
        ["تغيير كلمة المرور", "عدد المستخدمين"],
        ["تغيير رمز الكاش"],
        ["تعيين رابط القناة", "تقارير مبيعات يومية"],
        ["إرسال رسالة لجميع المستخدمين"],
        # إضافة الأزرار المشتركة (تبديل الحساب المقفول وسحب الرصيد)
        ["تبديل الحساب المقفول", "سحب الرصيد"],
        # أزرار مخصصة بواجهة الأدمن
        ["عرض طلبات سحب الرصيد", "عرض طلبات استبدال الايميل"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


##################################

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


# دوال المعالجة للأزرار في واجهة المستخدم (يمكنك استدعاؤها عبر CallbackQueryHandler)
async def switch_locked_account_callback(update: Update, context: CallbackContext):
    await update.callback_query.answer("تم اختيار تبديل الحساب المقفول")
    # أضف هنا منطق تبديل الحساب المقفول
    await safe_edit_message_text(update, context, "تم تبديل حالة الحساب المقفول.")

async def user_withdraw_balance_callback(update: Update, context: CallbackContext):
    await update.callback_query.answer("تم اختيار سحب الرصيد")
    # أضف هنا منطق سحب رصيد المستخدم
    await safe_edit_message_text(update, context, "سيتم سحب الرصيد الخاص بك قريباً.")

async def replace_email_callback(update: Update, context: CallbackContext):
    await update.callback_query.answer("تم اختيار استبدال الايميل")
    # منطق استبدال الايميل هنا، مثل عرض نموذج لتغيير الايميل
    await safe_edit_message_text(update, context, "يرجى إرسال الايميل الجديد للاستبدال.")

async def retrieve_balance_callback(update: Update, context: CallbackContext):
    await update.callback_query.answer("تم اختيار استرجاع الرصيد")
    # أضف منطق استرجاع الرصيد للمستخدم في حال كانت العملية تختلف عن سحب الرصيد
    await safe_edit_message_text(update, context, "يتم الآن استرجاع رصيدك الحالي.")

# دوال المعالجة للأزرار في واجهة الأدمن
async def view_withdrawal_requests_callback(update: Update, context: CallbackContext):
    await update.message.reply_text("عرض طلبات سحب الرصيد...")
    # إضافة منطق لاسترجاع وعرض طلبات سحب الرصيد من قاعدة البيانات

async def view_email_swap_requests_callback(update: Update, context: CallbackContext):
    await update.message.reply_text("عرض طلبات استبدال الايميل...")
    # إضافة منطق لاسترجاع وعرض طلبات استبدال الايميل من قاعدة البيانات

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

##############################################################################
# دالة start_command – بدء البوت وتسجيل المستخدم
##############################################################################
async def start_command(update: Update, context: CallbackContext):
    db: Database = context.bot_data["db"]
    user_id = update.effective_user.id
    await db.add_user(user_id, update.effective_user.full_name)
    available_balance = await get_available_balance(db, user_id)
    if update.effective_user.id in ADMIN_IDS:
        await update.message.reply_text(
            f"مرحباً بك أدمن!\nاختر ماذا تريد أن تفعل:",
            reply_markup=build_admin_menu_keyboard()
        )
    else:
        if not await check_subscription(update, context):
            channel_link = await db.get_setting("channel_link")
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("اشترك في القناة", url=channel_link)],
                [InlineKeyboardButton("تم الاشتراك", callback_data="check_subscription")]
            ])
            await update.message.reply_text("يرجى الاشتراك في القناة لتتمكن من استخدام البوت", reply_markup=keyboard)
        else:
            await send_main_menu(update, context)

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
            async with db.semaphore:
                cursor = await db.conn.execute(
                    "SELECT COUNT(*) FROM accounts WHERE details = ? OR (purchased_emails IS NOT NULL AND purchased_emails LIKE ?)",
                    (email, f"%{email}%")
                )
                row = await cursor.fetchone()
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
    async with db.semaphore:
        cursor = await db.conn.execute("SELECT * FROM recharge_requests WHERE op_number = ?", (op_number,))
        request_found = await cursor.fetchone()
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
    if text == "إضافة ايميلات جديدة":
        await update.message.reply_text("يرجى إرسال بيانات الحزمة كما هو موضح:\nالسطر الأول: اسم صاحب الحزمة\nالسطر الثاني: كلمة المرور\nالسطر الثالث وما بعده: قائمة الإيميلات (كل إيميل في سطر)")
        return
    elif text == "التحقق من طلبات شحن الرصيد":
        recharge_reqs = await db.get_recharge_requests()
        if not recharge_reqs:
            await update.message.reply_text("لا يوجد طلبات شحن رصيد حتى الآن.")
        else:
            for req in recharge_reqs:
                msg = (f"طلب شحن رصيد:\n"
                       f"المستخدم: {req[1]} - {req[2]}\n"
                       f"رقم العملية: {req[3]}\n"
                       f"المبلغ: {req[4]}\n"
                       f"طريقة الشحن: {req[5]}\n"
                       f"التاريخ: {req[6]}")
                kb = build_recharge_request_keyboard({"user_id": req[1], "user_name": req[2], "op_number": req[3], "amount": req[4], "timestamp": req[6]})
                await update.message.reply_text(msg, reply_markup=kb)
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
    elif text == "عرض طلبات سحب الرصيد":
        await view_withdrawal_requests_callback(update, context)
    elif text == "عرض طلبات استبدال الايميل":
        # استعلام عن كل طلبات استبدال الايميل التي ما زالت في حالة pending
        async with db.semaphore:
            cursor = await db.conn.execute("SELECT id, user_id, original_email, seller_name, created_at FROM email_swap_requests WHERE status = 'pending'")
            requests = await cursor.fetchall()
        if not requests:
            await update.message.reply_text("لا توجد طلبات استبدال ايميل حتى الآن.")
        else:
            for req in requests:
                request_id, req_user_id, original_email, seller_name, created_at = req
                msg = (f"طلب استبدال ايميل:\n"
                    f"المستخدم: {req_user_id}\n"
                    f"الايميل القديم: {original_email}\n"
                    f"اسم الحزمة: {seller_name}\n"
                    f"التاريخ: {created_at}")
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("تبديل الايميل", callback_data=f"swap_email_{request_id}")]
                ])
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
    elif text == "حالة طلبات الشحن":
        processed_reqs = await db.get_processed_recharge_requests()
        if not processed_reqs:
            await update.message.reply_text("لا توجد طلبات شحن معالجة حتى الآن.")
        else:
            messages = []
            for req in processed_reqs:
                msg = (f"المستخدم: {req[1]} - {req[2]}\n"
                       f"رقم العملية: {req[3]}\n"
                       f"المبلغ: {req[4]}\n"
                       f"الحالة: {req[5]}\n"
                       f"سبب الرفض: {req[6] if req[6] else 'لا يوجد'}\n"
                       f"التاريخ: {req[7]}")
                messages.append(msg)
            await update.message.reply_text("\n\n".join(messages))
    elif text == "تعيين رابط القناة":
        await update.message.reply_text("أدخل رابط القناة التي يجب على المستخدم الاشتراك بها:")
        return ADMIN_SET_CHANNEL
    elif text == "تعيين سعر دولار البايير":
        await update.message.reply_text("الرجاء إدخال سعر الدولار للبايير:")
        return ADMIN_SET_PAYEER_RATE
    elif text == "تقارير مبيعات يومية":
        # استدعاء وظيفة عرض تقارير المبيعات اليومية
        await show_daily_report_menu(update, context)
    else:
        await update.message.reply_text("خيار غير معروف!")

async def approve_recharge_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    data = query.data
    op_number = data.split("_")[-1]
    db: Database = context.bot_data["db"]
    async with db.semaphore:
        cursor = await db.conn.execute("SELECT * FROM recharge_requests WHERE op_number = ?", (op_number,))
        request_found = await cursor.fetchone()
    if not request_found:
        await query.answer("طلب الشحن غير موجود!")
        return
    user_id = request_found[1]
    amount = request_found[4]
    balance = await db.get_user_balance(user_id)
    new_balance = balance + amount
    await db.update_user_balance(user_id, new_balance)
    await db.conn.execute("DELETE FROM recharge_requests WHERE op_number = ?", (op_number,))
    await db.conn.commit()
    timestamp = (datetime.now() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    await db.add_processed_recharge_request(user_id, request_found[2], op_number, amount, "مقبول", "", timestamp)
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"تمت الموافقة على طلب الشحن.\nتمت إضافة {amount} ليرة إلى رصيدك.\nرصيدك الجديد: {new_balance} ليرة سورية."
        )
    except Exception as e:
        logging.error("Error sending approval message to user %s: %s", user_id, e)
    await safe_edit_message_text(update, context, f"تمت الموافقة على الشحن.\nتم إضافة {amount} ليرة إلى رصيد المستخدم {user_id}.")
    await send_admin_status(context, db)

async def swap_email_admin_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    # استخراج request_id من البيانات
    try:
        request_id = int(query.data.split("_")[-1])
    except:
        await query.answer("خطأ في البيانات")
        return

    db: Database = context.bot_data["db"]
    # جلب تفاصيل الطلب من قاعدة البيانات
    async with db.semaphore:
        cursor = await db.conn.execute("SELECT user_id, original_email, seller_name FROM email_swap_requests WHERE id = ? AND status = 'pending'", (request_id,))
        req = await cursor.fetchone()
    if not req:
        await query.edit_message_text("هذا الطلب غير موجود أو تمت معالجته سابقاً.")
        return

    user_id, original_email, seller_name = req
    # استرجاع ايميل جديد من قاعدة البيانات من نفس صاحب الحزمة
    new_account = await db.retrieve_email_for_swap(seller_name)
    if not new_account:
        await query.edit_message_text("لا يوجد ايميلات متاحة للاستبدال من نفس صاحب الحزمة.")
        return
    account_id, new_email, new_password = new_account

    # تحديث طلب استبدال الايميل في قاعدة البيانات
    await db.update_email_swap_request(request_id, new_email, new_password)

    # تحديث حالة الحساب في جدول accounts (مثلاً تغيير status إلى 'swapped' أو ما يلزم)
    await db.conn.execute(
        "UPDATE accounts SET status = 'swapped' WHERE id = ?",
        (account_id,)
    )
    await db.conn.commit()

    # تحديث الرسالة لتظهر البيانات بعد التبديل: الايميل القديم، الايميل الجديد، كلمة المرور واسم صاحب الحزمة
    response_text = (f"تم استبدال الايميل بنجاح:\n\n"
                     f"الايميل القديم: {original_email}\n"
                     f"الايميل الجديد: {new_email}\n"
                     f"كلمة المرور: {new_password}\n"
                     f"اسم صاحب الحزمة: {seller_name}")
    await query.edit_message_text(response_text)


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
# دالة بدء عملية استبدال الإيميل
async def replace_email_start_callback(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    prompt = "يرجى إرسال عنوان الايميل الذي تريد استبداله، يمكنك إرسال أكثر من ايميل؛ يُرجى إرسال كل ايميل في رسالة منفصلة."
    await safe_edit_message_text(update, context, prompt)
    # الدخول إلى حالة انتظار استقبال رسالة النص (يمكن استخدام حالة جديدة مثلاً: 1000)
    return 1000
async def process_replace_email_request(update: Update, context: CallbackContext):
    db: Database = context.bot_data["db"]
    user_id = update.effective_user.id
    text = update.message.text.strip()
    
    # إذا أراد المستخدم إنهاء إرسال الطلبات
    if text.lower() in ["انتهى", "تم"]:
        await update.message.reply_text("تم استلام طلبات استبدال الايميل، سيتم مراجعتها من قبل الأدمن.")
        return ConversationHandler.END

    # يفترض هنا أن المستخدم يرسل ايميل واحد في كل رسالة
    email = text  # يمكن إضافة التحقق من صيغة البريد الإلكتروني
    # تحقق من صحة الايميل (مثال بسيط)
    import re
    email_pattern = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
    if not email_pattern.match(email):
        await update.message.reply_text("الرجاء إرسال ايميل صالح.")
        return 1000

    # تحقق مما إذا كان هذا الايميل قد تم شراؤه من قبل المستخدم
    # مثال: البحث في جدول purchase_requests للعثور على الايميل ضمن السلسلة (يمكن تعديل المنطق بحسب التطبيق)
    cursor = await db.conn.execute("SELECT emails FROM purchase_requests WHERE user_id = ?", (user_id,))
    rows = await cursor.fetchall()
    purchased = False
    seller_name = None
    for row in rows:
        emails_str = row[0]
        # نفترض أن الصيغة هي: "email|password" لكل ايميل، مفصولة بفواصل
        for item in emails_str.split(","):
            if "|" in item:
                orig_email, pwd = item.split("|", 1)
                if orig_email.strip().lower() == email.lower():
                    purchased = True
                    # نعتمد أن seller_name يكون متوفرًا (يمكن تخزينه مع كل طلب شراء)
                    seller_name = "اسم_الحزمة"  # يمكنك تعديل هذا حسب ما هو مخزن في عملية الشراء
                    break
        if purchased:
            break

    if not purchased:
        await update.message.reply_text("هذا الايميل غير مسجل ضمن حساباتك المشتراة.")
        return 1000

    # تحقق مما إذا كان قد تم تقديم طلب استبدال لنفس الايميل مسبقاً
    if await db.check_email_swap_request(user_id, email):
        await update.message.reply_text("لقد سبق وأرسلت طلب استبدال لهذا الايميل.")
        return 1000

    # إضافة طلب استبدال الايميل إلى قاعدة البيانات
    await db.add_email_swap_request(user_id, email, seller_name)
    await update.message.reply_text(f"تم إرسال طلب استبدال الايميل: {email}\nيمكنك إرسال ايميل آخر أو إرسال 'انتهى' لإنهاء العملية.")

    # العودة إلى نفس الحالة لاستقبال رسائل إضافية
    return 1000

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
        await query.answer("خيار غير معروف")
        return ConversationHandler.END

##############################################################################
# الدالة الرئيسية لتشغيل البوت
##############################################################################
async def main():
    application = Application.builder().token(TOKEN).build()
    db = Database()
    await db.init_db()
    application.bot_data["db"] = db

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(approve_recharge_callback, pattern="^approve_recharge_"))
    application.add_handler(CallbackQueryHandler(contact_callback, pattern="^contact$"))
    application.add_handler(CallbackQueryHandler(contact_user_callback, pattern="^contact_user_"))
    application.add_handler(MessageHandler(filters.Regex("الرئيسية"), send_main_menu))
    application.add_handler(CallbackQueryHandler(check_subscription_callback, pattern="^check_subscription$"))
    application.add_handler(CallbackQueryHandler(lambda u, c: asyncio.create_task(send_main_menu(u, c)), pattern="^back$"))
    application.add_handler(CallbackQueryHandler(daily_report_callback, pattern="^daily_report_"))
    # (لأزرار واجهة المستخدم عبر inline keyboard)
    application.add_handler(CallbackQueryHandler(switch_locked_account_callback, pattern="^switch_locked_account$"))
    application.add_handler(CallbackQueryHandler(user_withdraw_balance_callback, pattern="^user_withdraw_balance$"))
    application.add_handler(CallbackQueryHandler(replace_email_callback, pattern="^replace_email$"))
    application.add_handler(CallbackQueryHandler(retrieve_balance_callback, pattern="^retrieve_balance$"))
    application.add_handler(CallbackQueryHandler(swap_email_admin_callback, pattern="^swap_email_"))

    user_conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(user_start_callback, pattern="^(sell_account|sold_accounts)$"),
            CallbackQueryHandler(buy_emails_callback, pattern="^buy_emails$"),
            CallbackQueryHandler(recharge_bot_callback, pattern="^recharge_bot$"),
            CallbackQueryHandler(recharge_syriatel_cash_callback, pattern="^recharge_syriatel_cash$"),
            CallbackQueryHandler(recharge_payeer_callback, pattern="^recharge_payeer$")
        ],
        states={
            SELL_ACCOUNT: [MessageHandler(filters.ALL, process_sell_account)],
            BUY_EMAILS: [CallbackQueryHandler(buy_emails_choice_callback, pattern="^buy_(1|5|10|20|30|by_balance|back)$")],
            RECHARGE_SERIAL_NUMBER: [MessageHandler(filters.TEXT, process_recharge_serial_number)],
            RECHARGE_AMOUNT: [MessageHandler(filters.TEXT, process_recharge_amount)],
            RECHARGE_SERIAL_NUMBER_PAYEER: [MessageHandler(filters.TEXT, process_recharge_serial_number_payeer)],
            RECHARGE_AMOUNT_PAYEER: [MessageHandler(filters.TEXT, process_recharge_amount_payeer)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )
    application.add_handler(user_conv_handler)
    application.add_handler(CallbackQueryHandler(lambda u, c: asyncio.create_task(cancel_request_callback(u, c)), pattern="^cancel_request$"))

    admin_update_price_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS) & filters.Regex("^تحديث سعر الحساب$"), ask_update_price)],
        states={
            ADMIN_UPDATE_PRICE: [MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS), process_update_price)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    application.add_handler(admin_update_price_conv_handler)

    admin_change_password_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS) & filters.Regex("^تغيير كلمة المرور$"), ask_admin_change_password)],
        states={
            ADMIN_CHANGE_PASSWORD: [MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS), process_admin_change_password)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    application.add_handler(admin_change_password_conv_handler)

    admin_change_syriatlcash_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS) & filters.Regex("^تغيير رمز الكاش$"), ask_admin_change_syriatelcash)],
        states={
            ADMIN_CHANGE_SYRIATELCASH: [MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS), process_ADMIN_CHANGE_SYRIATELCASH)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    application.add_handler(admin_change_syriatlcash_conv_handler)

    replace_email_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(replace_email_start_callback, pattern="^replace_email$")],
        states={
            1000: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_replace_email_request)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    application.add_handler(replace_email_conv_handler)

    admin_broadcast_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS) & filters.Regex("^إرسال رسالة لجميع المستخدمين$"), ask_broadcast_message)],
        states={
            ADMIN_BROADCAST: [MessageHandler(filters.TEXT, process_broadcast_message),
                              CallbackQueryHandler(confirm_broadcast_message, pattern="^confirm_broadcast$"),
                              CallbackQueryHandler(cancel_broadcast, pattern="^cancel_broadcast$")]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    application.add_handler(admin_broadcast_conv_handler)

    admin_set_channel_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS) & filters.Regex("^تعيين رابط القناة$"), ask_set_channel)],
        states={
            ADMIN_SET_CHANNEL: [MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS), process_set_channel)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    application.add_handler(admin_set_channel_conv_handler)

    admin_set_payeer_rate_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS) & filters.Regex("^تعيين سعر دولار البايير$"), ask_set_payeer_rate)],
        states={
            ADMIN_SET_PAYEER_RATE: [MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS), process_set_payeer_rate)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    application.add_handler(admin_set_payeer_rate_conv_handler)

    admin_add_emails_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS) & filters.Regex("^إضافة ايميلات جديدة$"), ask_admin_add_emails)],
        states={
            ADMIN_ADD_EMAILS: [MessageHandler(filters.TEXT, process_admin_add_emails)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    application.add_handler(admin_add_emails_conv_handler)

    reject_recharge_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(reject_recharge_callback, pattern="^reject_recharge_")],
        states={
            REJECT_RECHARGE_REASON: [MessageHandler(filters.ALL, process_reject_recharge_reason)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    application.add_handler(reject_recharge_conv_handler)

    application.add_handler(MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS), handle_admin_buttons))
    logging.info("البوت يعمل بكفاءة مع الميزات والمحسّنات الجديدة...")
    await application.run_polling()

if __name__ == '__main__':
    import nest_asyncio
    nest_asyncio.apply()
    asyncio.run(main())
