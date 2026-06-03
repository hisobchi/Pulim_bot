"""
Pulim Bot v3 — Wallets + Admin Panel
"""

import os, io, json, logging, asyncio, pathlib
import aiohttp
from datetime import datetime, timedelta

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
    WebAppInfo, BotCommand
)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

import aiosqlite
import anthropic
from aiohttp import web

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
WEB_PORT = int(os.getenv("PORT", os.getenv("WEB_PORT", "8080")))

if not BOT_TOKEN: print("ERROR: BOT_TOKEN"); exit(1)
if not ANTHROPIC_API_KEY: print("ERROR: ANTHROPIC_API_KEY"); exit(1)

DB_PATH = "pulim.db"
_root = pathlib.Path(__file__).parent
WEBAPP_DIR = _root / "webapp" if (_root / "webapp").exists() else _root

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("pulim")

# ══════════════════════════════════════════════════════
#  CATEGORIES
# ══════════════════════════════════════════════════════
CATEGORIES = {
    "food":{"icon":"🍽","ru":"Еда","uz":"Ovqat","color":"#FF6B6B"},
    "transport":{"icon":"🚕","ru":"Транспорт","uz":"Transport","color":"#4ECDC4"},
    "housing":{"icon":"🏠","ru":"Жильё","uz":"Uy-joy","color":"#38bdf8"},
    "entertainment":{"icon":"🎮","ru":"Развлечения","uz":"Ko'ngilochar","color":"#96CEB4"},
    "health":{"icon":"💊","ru":"Здоровье","uz":"Salomatlik","color":"#fbbf24"},
    "clothing":{"icon":"👕","ru":"Одежда","uz":"Kiyim","color":"#DDA0DD"},
    "education":{"icon":"📚","ru":"Образование","uz":"Ta'lim","color":"#74B9FF"},
    "transfers":{"icon":"💸","ru":"Переводы","uz":"O'tkazmalar","color":"#A29BFE"},
    "shopping":{"icon":"🛒","ru":"Покупки","uz":"Xaridlar","color":"#FF9FF3"},
    "subscriptions":{"icon":"📱","ru":"Подписки","uz":"Obunalar","color":"#54A0FF"},
    "credit":{"icon":"💳","ru":"Кредит","uz":"Kredit","color":"#EE5A24"},
    "communication":{"icon":"📞","ru":"Связь","uz":"Aloqa","color":"#78E08F"},
    "beauty":{"icon":"💇","ru":"Красота","uz":"Go'zallik","color":"#F8A5C2"},
    "other_expense":{"icon":"📦","ru":"Прочее","uz":"Boshqa","color":"#B2BEC3"},
    "salary":{"icon":"💰","ru":"Зарплата","uz":"Maosh","color":"#00d4a1"},
    "freelance":{"icon":"💻","ru":"Фриланс","uz":"Frilanser","color":"#8b5cf6"},
    "business":{"icon":"🏢","ru":"Бизнес","uz":"Biznes","color":"#6C5CE7"},
    "other_income":{"icon":"📥","ru":"Другой доход","uz":"Boshqa daromad","color":"#55EFC4"},
}

WALLET_ICONS = ["💰","💳","🏦","👛","💵","🏠","🚗","✈️","🎓","👨‍👩‍👧‍👦"]

# ══════════════════════════════════════════════════════
#  EXCHANGE RATES
# ══════════════════════════════════════════════════════
exchange_rates = {"USD":12750,"EUR":13800,"RUB":137,"GBP":16100,"KZT":27,"UZS":1}

async def update_exchange_rates():
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://cbu.uz/ru/arkhiv-kursov-valyut/json/", timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 200:
                    for item in await r.json():
                        code = item.get("Ccy","")
                        rate = float(item.get("Rate",0))
                        if code in exchange_rates and rate > 0:
                            exchange_rates[code] = round(rate)
                    log.info(f"Rates: USD={exchange_rates['USD']} EUR={exchange_rates['EUR']}")
    except Exception as e:
        log.warning(f"Rates error: {e}")


# ══════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                language TEXT DEFAULT 'ru',
                is_approved INTEGER DEFAULT 0,
                is_admin INTEGER DEFAULT 0,
                is_blocked INTEGER DEFAULT 0,
                active_wallet_id INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS wallets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                icon TEXT DEFAULT '💰',
                currency TEXT DEFAULT 'UZS',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                wallet_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                category TEXT NOT NULL,
                amount REAL NOT NULL,
                currency TEXT DEFAULT 'UZS',
                amount_uzs REAL NOT NULL,
                exchange_rate REAL DEFAULT 1,
                description TEXT,
                original_text TEXT,
                source TEXT DEFAULT 'text',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(user_id);
            CREATE INDEX IF NOT EXISTS idx_tx_wallet ON transactions(wallet_id);
            CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(created_at);
        """)

        # Make admin IDs admin + approved
        for aid in ADMIN_IDS:
            cur = await db.execute("SELECT telegram_id FROM users WHERE telegram_id=?", (aid,))
            if await cur.fetchone():
                await db.execute("UPDATE users SET is_admin=1, is_approved=1 WHERE telegram_id=?", (aid,))

        await db.commit()
    log.info("DB ready")


async def ensure_user(tg_user):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT telegram_id FROM users WHERE telegram_id=?", (tg_user.id,))
        if not await cur.fetchone():
            is_admin = 1 if tg_user.id in ADMIN_IDS else 0
            is_approved = 1 if tg_user.id in ADMIN_IDS else 0
            await db.execute(
                "INSERT INTO users (telegram_id,username,first_name,is_admin,is_approved) VALUES (?,?,?,?,?)",
                (tg_user.id, tg_user.username, tg_user.first_name, is_admin, is_approved)
            )
            # Create default wallet
            await db.execute(
                "INSERT INTO wallets (user_id,name,icon) VALUES (?,?,?)",
                (tg_user.id, "Основной", "💰")
            )
            wallet_id = (await (await db.execute("SELECT last_insert_rowid()")).fetchone())[0]
            await db.execute("UPDATE users SET active_wallet_id=? WHERE telegram_id=?", (wallet_id, tg_user.id))
            await db.commit()
            log.info(f"New user: {tg_user.first_name} (approved={is_approved})")


async def is_user_approved(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT is_approved, is_blocked FROM users WHERE telegram_id=?", (user_id,))
        row = await cur.fetchone()
        if not row: return False
        return row[0] == 1 and row[1] == 0


async def is_user_admin(user_id):
    return user_id in ADMIN_IDS


async def get_user_lang(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT language FROM users WHERE telegram_id=?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row else "ru"

async def set_user_lang(user_id, lang):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET language=? WHERE telegram_id=?", (lang, user_id))
        await db.commit()


# ── WALLETS ──
async def get_user_wallets(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM wallets WHERE user_id=? ORDER BY id", (user_id,))
        return await cur.fetchall()

async def get_active_wallet(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT w.* FROM wallets w
               JOIN users u ON u.active_wallet_id = w.id
               WHERE u.telegram_id=?""", (user_id,))
        row = await cur.fetchone()
        if not row:
            cur2 = await db.execute("SELECT * FROM wallets WHERE user_id=? LIMIT 1", (user_id,))
            row = await cur2.fetchone()
        return row

async def set_active_wallet(user_id, wallet_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET active_wallet_id=? WHERE telegram_id=?", (wallet_id, user_id))
        await db.commit()

async def create_wallet(user_id, name, icon="💰"):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO wallets (user_id,name,icon) VALUES (?,?,?)", (user_id,name,icon))
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid()")
        return (await cur.fetchone())[0]

async def delete_wallet(wallet_id, user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cnt = await db.execute("SELECT COUNT(*) FROM wallets WHERE user_id=?", (user_id,))
        if (await cnt.fetchone())[0] <= 1: return False
        await db.execute("DELETE FROM transactions WHERE wallet_id=?", (wallet_id,))
        await db.execute("DELETE FROM wallets WHERE id=? AND user_id=?", (wallet_id, user_id))
        first = await db.execute("SELECT id FROM wallets WHERE user_id=? LIMIT 1", (user_id,))
        frow = await first.fetchone()
        if frow: await db.execute("UPDATE users SET active_wallet_id=? WHERE telegram_id=?", (frow[0], user_id))
        await db.commit()
        return True


# ── TRANSACTIONS ──
async def save_transaction(user_id, wallet_id, data, source="text", original=""):
    currency = data.get("currency","UZS").upper()
    amount = abs(float(data.get("amount",0)))
    rate = exchange_rates.get(currency, 1)
    amount_uzs = amount * rate if currency != "UZS" else amount

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO transactions
               (user_id,wallet_id,type,category,amount,currency,amount_uzs,exchange_rate,description,original_text,source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (user_id,wallet_id,data["type"],data["category"],amount,currency,amount_uzs,rate,data.get("description",""),original,source)
        )
        await db.commit()
    return {"amount":amount,"currency":currency,"amount_uzs":amount_uzs,"rate":rate}


async def get_balance(user_id, wallet_id=None, period="month"):
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.now()
        if period=="month": start=now.replace(day=1,hour=0,minute=0,second=0).isoformat()
        elif period=="week": start=(now-timedelta(days=now.weekday())).replace(hour=0,minute=0,second=0).isoformat()
        elif period=="today": start=now.replace(hour=0,minute=0,second=0).isoformat()
        else: start="2000-01-01"

        db.row_factory = aiosqlite.Row
        if wallet_id:
            cur = await db.execute(
                "SELECT type,amount_uzs FROM transactions WHERE user_id=? AND wallet_id=? AND created_at>=?",
                (user_id,wallet_id,start))
        else:
            cur = await db.execute(
                "SELECT type,amount_uzs FROM transactions WHERE user_id=? AND created_at>=?",
                (user_id,start))
        rows = await cur.fetchall()
        income = sum(r["amount_uzs"] for r in rows if r["type"]=="income")
        expense = sum(r["amount_uzs"] for r in rows if r["type"]=="expense")
        return {"income":income,"expense":expense,"balance":income-expense,"count":len(rows)}


async def get_stats_by_category(user_id, wallet_id=None):
    async with aiosqlite.connect(DB_PATH) as db:
        start = datetime.now().replace(day=1,hour=0,minute=0,second=0).isoformat()
        db.row_factory = aiosqlite.Row
        if wallet_id:
            cur = await db.execute(
                """SELECT category,SUM(amount_uzs) as total,COUNT(*) as cnt
                   FROM transactions WHERE user_id=? AND wallet_id=? AND type='expense' AND created_at>=?
                   GROUP BY category ORDER BY total DESC""", (user_id,wallet_id,start))
        else:
            cur = await db.execute(
                """SELECT category,SUM(amount_uzs) as total,COUNT(*) as cnt
                   FROM transactions WHERE user_id=? AND type='expense' AND created_at>=?
                   GROUP BY category ORDER BY total DESC""", (user_id,start))
        return await cur.fetchall()


async def get_recent_transactions(user_id, wallet_id=None, limit=10):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if wallet_id:
            cur = await db.execute(
                "SELECT * FROM transactions WHERE user_id=? AND wallet_id=? ORDER BY created_at DESC LIMIT ?",
                (user_id,wallet_id,limit))
        else:
            cur = await db.execute(
                "SELECT * FROM transactions WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                (user_id,limit))
        return await cur.fetchall()


async def delete_last_transaction(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id FROM transactions WHERE user_id=? ORDER BY created_at DESC LIMIT 1",(user_id,))
        row = await cur.fetchone()
        if row:
            await db.execute("DELETE FROM transactions WHERE id=?",(row[0],))
            await db.commit()
            return True
        return False


# ── ADMIN DB ──
async def get_all_users():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT u.*, COUNT(t.id) as tx_count,
                   COALESCE(SUM(CASE WHEN t.type='expense' THEN t.amount_uzs ELSE 0 END),0) as total_expense,
                   COALESCE(SUM(CASE WHEN t.type='income' THEN t.amount_uzs ELSE 0 END),0) as total_income
            FROM users u LEFT JOIN transactions t ON t.user_id=u.telegram_id
            GROUP BY u.telegram_id ORDER BY u.created_at DESC
        """)
        return await cur.fetchall()

async def approve_user(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_approved=1,is_blocked=0 WHERE telegram_id=?",(user_id,))
        await db.commit()

async def block_user(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_blocked=1,is_approved=0 WHERE telegram_id=?",(user_id,))
        await db.commit()

async def revoke_user(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_approved=0 WHERE telegram_id=?",(user_id,))
        await db.commit()


# ══════════════════════════════════════════════════════
#  AI
# ══════════════════════════════════════════════════════
ai_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """Ты — финансовый AI. Пользователь пишет расход или доход.

Определи:
1. type: "income" или "expense"
2. category: одна из списка
3. amount: число (50к=50000, 1.5м=1500000, млн=×1000000)
4. currency: "UZS","USD","EUR","RUB" (по умолчанию UZS. $=USD, €=EUR, ₽=RUB)
5. description: 1-3 слова

Категории расходов: food,transport,housing,entertainment,health,clothing,education,transfers,shopping,subscriptions,credit,communication,beauty,other_expense
Категории доходов: salary,freelance,business,other_income

ОТВЕТ ТОЛЬКО JSON:
{"type":"expense","category":"food","amount":50000,"currency":"UZS","description":"обед"}"""

async def categorize_with_ai(text):
    try:
        msg = ai_client.messages.create(
            model="claude-sonnet-4-6", max_tokens=200, system=SYSTEM_PROMPT,
            messages=[{"role":"user","content":text}])
        raw = msg.content[0].text.strip()
        if raw.startswith("```"): raw=raw.split("\n",1)[1].rsplit("```",1)[0].strip()
        r = json.loads(raw)
        if r["type"] not in ("income","expense"): r["type"]="expense"
        if r["category"] not in CATEGORIES: r["category"]="other_expense" if r["type"]=="expense" else "other_income"
        r["amount"]=abs(float(r.get("amount",0)))
        r["currency"]=r.get("currency","UZS").upper()
        if r["currency"] not in exchange_rates: r["currency"]="UZS"
        log.info(f"AI: '{text}' -> {r['type']}/{r['category']}/{r['amount']} {r['currency']}")
        return r
    except Exception as e:
        log.error(f"AI error: {e}")
        return {"type":"expense","category":"other_expense","amount":0,"currency":"UZS","description":text[:30]}


# ══════════════════════════════════════════════════════
#  VOICE
# ══════════════════════════════════════════════════════
async def voice_to_text(bot_inst, file_id):
    if not OPENAI_API_KEY: return ""
    try:
        import openai
        f = await bot_inst.get_file(file_id)
        fb = await bot_inst.download_file(f.file_path)
        c = openai.OpenAI(api_key=OPENAI_API_KEY)
        t = c.audio.transcriptions.create(model="whisper-1",file=("voice.ogg",fb.read(),"audio/ogg"))
        return t.text
    except Exception as e:
        log.error(f"Voice: {e}"); return ""


# ══════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════
def fmt(n): return f"{n:,.0f}".replace(",", " ")
def fmtS(n):
    a=abs(n)
    if a>=1e6: return f"{n/1e6:.1f}M"
    if a>=1e3: return f"{n/1e3:.0f}k"
    return str(int(n))
def cat_icon(k): return CATEGORIES.get(k,{}).get("icon","📦")
def cat_label(k,lang="ru"): return CATEGORIES.get(k,{}).get("icon","📦")+" "+CATEGORIES.get(k,{}).get(lang,"Прочее")

def get_main_keyboard(lang="ru"):
    buttons = []
    if WEBAPP_URL:
        buttons.append([KeyboardButton(text="💰 Финансы" if lang=="ru" else "💰 Moliya", web_app=WebAppInfo(url=WEBAPP_URL))])
    buttons.extend([
        [KeyboardButton(text="📊 Баланс" if lang=="ru" else "📊 Balans"),
         KeyboardButton(text="📈 Статистика" if lang=="ru" else "📈 Statistika")],
        [KeyboardButton(text="👛 Кошельки" if lang=="ru" else "👛 Hamyonlar"),
         KeyboardButton(text="📋 История" if lang=="ru" else "📋 Tarix")],
    ])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


# ══════════════════════════════════════════════════════
#  BOT
# ══════════════════════════════════════════════════════
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()


# ── ACCESS CHECK ──
async def check_access(message):
    """Returns True if user can use bot, False otherwise."""
    await ensure_user(message.from_user)
    if await is_user_admin(message.from_user.id): return True
    if await is_user_approved(message.from_user.id): return True

    # Request access
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📩 Запросить доступ", callback_data="request_access")]
    ])
    await message.answer(
        "🔒 <b>Доступ ограничен</b>\n\n"
        "Этот бот работает по приглашению.\n"
        "Нажмите кнопку чтобы запросить доступ.",
        reply_markup=keyboard
    )
    return False


@router.callback_query(F.data == "request_access")
async def cb_request_access(callback: CallbackQuery):
    user = callback.from_user
    # Notify all admins
    for aid in ADMIN_IDS:
        try:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Одобрить", callback_data=f"admin_approve_{user.id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"admin_block_{user.id}"),
            ]])
            await bot.send_message(aid,
                f"📩 <b>Запрос доступа</b>\n\n"
                f"👤 {user.first_name} (@{user.username})\n"
                f"🆔 <code>{user.id}</code>",
                reply_markup=kb)
        except: pass
    await callback.message.edit_text("📩 Запрос отправлен! Ожидайте одобрения администратором.")


@router.callback_query(F.data.startswith("admin_approve_"))
async def cb_admin_approve(callback: CallbackQuery):
    if not await is_user_admin(callback.from_user.id): return
    uid = int(callback.data.replace("admin_approve_",""))
    await approve_user(uid)
    await callback.message.edit_text(f"✅ Пользователь {uid} одобрен!")
    try: await bot.send_message(uid, "🎉 <b>Доступ одобрен!</b>\nНажмите /start чтобы начать.")
    except: pass

@router.callback_query(F.data.startswith("admin_block_"))
async def cb_admin_block(callback: CallbackQuery):
    if not await is_user_admin(callback.from_user.id): return
    uid = int(callback.data.replace("admin_block_",""))
    await block_user(uid)
    await callback.message.edit_text(f"🚫 Пользователь {uid} заблокирован.")


# ── /start ──
@router.message(CommandStart())
async def cmd_start(message: Message):
    if not await check_access(message): return
    lang = await get_user_lang(message.from_user.id)
    wallet = await get_active_wallet(message.from_user.id)
    wname = f"{wallet['icon']} {wallet['name']}" if wallet else "💰 Основной"

    await message.answer(
        f"Привет, {message.from_user.first_name}! 👋\n\n"
        f"Просто напишите или скажите голосом, что потратили — "
        f"например «<b>потратил 50 000 на обед</b>» или «<b>50к такси</b>».\n\n"
        f"Активный кошелёк: <b>{wname}</b>\n\n"
        f"Команды:\n"
        f"/balance — баланс\n"
        f"/wallet — кошельки\n"
        f"/stats — статистика\n"
        f"/history — история\n"
        f"/language — язык\n"
        f"/help — помощь",
        reply_markup=get_main_keyboard(lang)
    )


# ── /help ──
@router.message(Command("help"))
async def cmd_help(message: Message):
    if not await check_access(message): return
    await message.answer(
        "📖 <b>Как пользоваться</b>\n\n"
        "Пишите расходы/доходы:\n"
        "  <i>обед 89000</i>\n"
        "  <i>зп 12.5 млн</i>\n"
        "  <i>$50 кредит</i>\n\n"
        "Валюты: UZS, USD ($), EUR (€), RUB (₽)\n"
        "🎙 Голосовые тоже работают!\n\n"
        "👛 /wallet — управление кошельками\n"
        "/balance /stats /history /today /undo /language"
    )


# ── /language ──
@router.message(Command("language"))
async def cmd_language(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru"),
         InlineKeyboardButton(text="🇺🇿 O'zbek", callback_data="lang_uz")]
    ])
    await message.answer("🌐 Выберите язык:", reply_markup=kb)

@router.callback_query(F.data.startswith("lang_"))
async def cb_language(callback: CallbackQuery):
    lang = callback.data.replace("lang_","")
    await set_user_lang(callback.from_user.id, lang)
    msg = "Готово! Язык — Русский. 🇷🇺" if lang=="ru" else "Tayyor! Til — O'zbek. 🇺🇿"
    await callback.message.answer(msg, reply_markup=get_main_keyboard(lang))
    await callback.answer()


# ══════════════════════════════════════════════════════
#  WALLETS
# ══════════════════════════════════════════════════════
@router.message(Command("wallet"))
@router.message(F.text.in_({"👛 Кошельки","👛 Hamyonlar"}))
async def cmd_wallets(message: Message):
    if not await check_access(message): return
    wallets = await get_user_wallets(message.from_user.id)
    active = await get_active_wallet(message.from_user.id)
    active_id = active["id"] if active else 0

    lines = []
    buttons = []
    for w in wallets:
        mark = " ✅" if w["id"]==active_id else ""
        lines.append(f"{w['icon']} <b>{w['name']}</b>{mark}")
        if w["id"] != active_id:
            buttons.append([InlineKeyboardButton(
                text=f"{w['icon']} {w['name']}",
                callback_data=f"switch_wallet_{w['id']}"
            )])

    buttons.append([InlineKeyboardButton(text="➕ Новый кошелёк", callback_data="new_wallet")])
    if len(wallets) > 1:
        buttons.append([InlineKeyboardButton(text="🗑 Удалить кошелёк", callback_data="del_wallet_menu")])

    await message.answer(
        f"👛 <b>Кошельки</b>\n\n" + "\n".join(lines) +
        "\n\nНажмите чтобы переключить:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@router.callback_query(F.data.startswith("switch_wallet_"))
async def cb_switch_wallet(callback: CallbackQuery):
    wid = int(callback.data.replace("switch_wallet_",""))
    await set_active_wallet(callback.from_user.id, wid)
    wallets = await get_user_wallets(callback.from_user.id)
    w = next((w for w in wallets if w["id"]==wid), None)
    name = f"{w['icon']} {w['name']}" if w else "?"
    await callback.message.edit_text(f"✅ Активный кошелёк: <b>{name}</b>")


# New wallet flow
user_states = {}

@router.callback_query(F.data == "new_wallet")
async def cb_new_wallet(callback: CallbackQuery):
    user_states[callback.from_user.id] = {"action": "new_wallet_name"}
    await callback.message.answer("✏️ Введите название кошелька:")
    await callback.answer()

@router.callback_query(F.data.startswith("wallet_icon_"))
async def cb_wallet_icon(callback: CallbackQuery):
    icon = callback.data.replace("wallet_icon_","")
    uid = callback.from_user.id
    state = user_states.get(uid,{})
    name = state.get("wallet_name","Новый")
    wid = await create_wallet(uid, name, icon)
    await set_active_wallet(uid, wid)
    user_states.pop(uid, None)
    await callback.message.edit_text(f"✅ Кошелёк <b>{icon} {name}</b> создан и активирован!")


@router.callback_query(F.data == "del_wallet_menu")
async def cb_del_wallet_menu(callback: CallbackQuery):
    wallets = await get_user_wallets(callback.from_user.id)
    active = await get_active_wallet(callback.from_user.id)
    buttons = []
    for w in wallets:
        if w["id"] != active["id"]:
            buttons.append([InlineKeyboardButton(
                text=f"🗑 {w['icon']} {w['name']}",
                callback_data=f"del_wallet_{w['id']}"
            )])
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="del_wallet_cancel")])
    await callback.message.edit_text("Выберите кошелёк для удаления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("del_wallet_"))
async def cb_del_wallet(callback: CallbackQuery):
    if callback.data == "del_wallet_cancel":
        await callback.message.edit_text("👌 Отменено.")
        return
    wid = int(callback.data.replace("del_wallet_",""))
    ok = await delete_wallet(wid, callback.from_user.id)
    await callback.message.edit_text("✅ Кошелёк удалён!" if ok else "❌ Нельзя удалить единственный кошелёк.")


# ── /balance ──
@router.message(Command("balance"))
@router.message(F.text.in_({"📊 Баланс","📊 Balans"}))
async def cmd_balance(message: Message):
    if not await check_access(message): return
    wallet = await get_active_wallet(message.from_user.id)
    b = await get_balance(message.from_user.id, wallet["id"] if wallet else None, "month")
    wname = f"{wallet['icon']} {wallet['name']}" if wallet else "💰"

    await message.answer(
        f"📊 <b>Баланс за месяц</b>\n"
        f"👛 {wname}\n\n"
        f"💰 Доходы:  <b>+{fmt(b['income'])} UZS</b>\n"
        f"💸 Расходы: <b>-{fmt(b['expense'])} UZS</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{'📈' if b['balance']>=0 else '📉'} Итого: <b>{'+' if b['balance']>=0 else ''}{fmt(b['balance'])} UZS</b>"
    )

@router.message(Command("today"))
async def cmd_today(message: Message):
    if not await check_access(message): return
    wallet = await get_active_wallet(message.from_user.id)
    b = await get_balance(message.from_user.id, wallet["id"] if wallet else None, "today")
    await message.answer(
        f"📅 <b>Сегодня</b> ({wallet['icon']} {wallet['name']})\n\n"
        f"💰 +{fmt(b['income'])} · 💸 -{fmt(b['expense'])}\n"
        f"📈 Итого: <b>{'+' if b['balance']>=0 else ''}{fmt(b['balance'])} UZS</b>"
    )


# ── /stats ──
@router.message(Command("stats"))
@router.message(F.text.in_({"📈 Статистика","📈 Statistika"}))
async def cmd_stats(message: Message):
    if not await check_access(message): return
    wallet = await get_active_wallet(message.from_user.id)
    rows = await get_stats_by_category(message.from_user.id, wallet["id"] if wallet else None)
    if not rows:
        await message.answer(f"📈 Нет расходов за этот месяц в кошельке {wallet['icon']} {wallet['name']}.")
        return
    total = sum(r["total"] for r in rows)
    lines = [f"{cat_label(r['category'])} — <b>{fmt(r['total'])}</b> ({r['total']/total*100:.0f}%)" for r in rows]
    await message.answer(
        f"📈 <b>Расходы</b> ({wallet['icon']} {wallet['name']})\n\n" +
        "\n".join(lines) +
        f"\n\n💸 Всего: <b>{fmt(total)} UZS</b>"
    )


# ── /history ──
@router.message(Command("history"))
@router.message(F.text.in_({"📋 История","📋 Tarix"}))
async def cmd_history(message: Message):
    if not await check_access(message): return
    wallet = await get_active_wallet(message.from_user.id)
    txs = await get_recent_transactions(message.from_user.id, wallet["id"] if wallet else None, 10)
    if not txs:
        await message.answer("📋 Пока нет операций.")
        return
    lines = []
    for tx in txs:
        sign = "+" if tx["type"]=="income" else "-"
        cur = tx["currency"] or "UZS"
        dt = datetime.fromisoformat(tx["created_at"]).strftime("%d.%m %H:%M")
        line = f"{'💚' if tx['type']=='income' else '🔴'} {sign}{fmt(tx['amount'])} {cur}"
        if cur != "UZS" and tx["amount_uzs"]: line += f" (≈{fmt(tx['amount_uzs'])})"
        line += f"\n   {cat_icon(tx['category'])} {tx['description']} · {dt}"
        lines.append(line)
    await message.answer(f"📋 <b>История</b> ({wallet['icon']} {wallet['name']})\n\n"+"\n\n".join(lines))


# ── /undo ──
@router.message(Command("undo"))
async def cmd_undo(message: Message):
    if not await check_access(message): return
    ok = await delete_last_transaction(message.from_user.id)
    await message.answer("✅ Удалено!" if ok else "📋 Нечего удалять.")


# ── /admin ──
@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not await is_user_admin(message.from_user.id):
        await message.answer("🚫 Нет доступа.")
        return
    admin_url = f"http://localhost:{WEB_PORT}/admin?token={BOT_TOKEN[:20]}"
    if WEBAPP_URL:
        admin_url = f"{WEBAPP_URL}/admin?token={BOT_TOKEN[:20]}"

    users = await get_all_users()
    approved = sum(1 for u in users if u["is_approved"])
    blocked = sum(1 for u in users if u["is_blocked"])
    pending = sum(1 for u in users if not u["is_approved"] and not u["is_blocked"])

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Список пользователей", callback_data="admin_users")],
        [InlineKeyboardButton(text="🌐 Админ-панель (браузер)", url=admin_url)],
    ])
    await message.answer(
        f"👑 <b>Админ-панель</b>\n\n"
        f"👥 Пользователей: {len(users)}\n"
        f"✅ Одобрено: {approved}\n"
        f"⏳ Ожидают: {pending}\n"
        f"🚫 Заблокировано: {blocked}",
        reply_markup=kb
    )


@router.callback_query(F.data == "admin_users")
async def cb_admin_users(callback: CallbackQuery):
    if not await is_user_admin(callback.from_user.id): return
    users = await get_all_users()
    lines = []
    for u in users:
        status = "✅" if u["is_approved"] else ("🚫" if u["is_blocked"] else "⏳")
        admin = " 👑" if u["is_admin"] else ""
        lines.append(f"{status}{admin} {u['first_name']} (@{u['username']}) — {u['tx_count']} оп.")

    buttons = []
    for u in users:
        if not u["is_admin"]:
            if not u["is_approved"]:
                buttons.append([InlineKeyboardButton(
                    text=f"✅ Одобрить {u['first_name']}",
                    callback_data=f"admin_approve_{u['telegram_id']}"
                )])
            else:
                buttons.append([InlineKeyboardButton(
                    text=f"🚫 Заблокировать {u['first_name']}",
                    callback_data=f"admin_block_{u['telegram_id']}"
                )])

    await callback.message.edit_text(
        "👥 <b>Пользователи</b>\n\n" + "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    )


# ══════════════════════════════════════════════════════
#  MAIN TEXT HANDLER
# ══════════════════════════════════════════════════════
BUTTON_TEXTS = {"📊 Баланс","📊 Balans","📈 Статистика","📈 Statistika","📋 История","📋 Tarix","👛 Кошельки","👛 Hamyonlar","💰 Финансы","💰 Moliya"}

@router.message(F.text & ~F.text.startswith("/") & ~F.text.in_(BUTTON_TEXTS))
async def handle_text(message: Message):
    uid = message.from_user.id

    # Check if in new_wallet flow
    if uid in user_states and user_states[uid].get("action") == "new_wallet_name":
        name = message.text.strip()[:30]
        user_states[uid] = {"action":"new_wallet_icon","wallet_name":name}
        buttons = [[InlineKeyboardButton(text=ic, callback_data=f"wallet_icon_{ic}") for ic in WALLET_ICONS[i:i+5]] for i in range(0,len(WALLET_ICONS),5)]
        await message.answer(f"Выберите иконку для «{name}»:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        return

    if not await check_access(message): return

    wallet = await get_active_wallet(uid)
    if not wallet:
        await message.answer("❌ Нет кошелька. /wallet")
        return

    result = await categorize_with_ai(message.text)
    tx_info = await save_transaction(uid, wallet["id"], result, "text", message.text)

    # Compact response
    tp = "Доход" if result["type"]=="income" else "Расход"
    sign = "+" if result["type"]=="income" else "-"
    cur = tx_info["currency"]
    amt = tx_info["amount"]

    line1 = f"✅ {tp}: <b>{sign}{fmt(amt)} {cur}</b>"
    conv = ""
    if cur != "UZS":
        conv = f"   ≈ {sign}{fmt(tx_info['amount_uzs'])} UZS (курс {fmt(tx_info['rate'])})"

    cat = CATEGORIES.get(result["category"],{})
    line2 = f"{cat.get('icon','📦')} {cat.get('ru','Прочее')}"
    line3 = f"📝 {result.get('description','')}"
    line4 = f"👛 {wallet['icon']} {wallet['name']}"

    resp = line1
    if conv: resp += "\n" + conv
    resp += f"\n{line2}\n{line3}\n{line4}"
    await message.answer(resp)


# ── VOICE ──
@router.message(F.voice)
async def handle_voice(message: Message):
    if not await check_access(message): return
    if not OPENAI_API_KEY:
        await message.answer("🎙 Голосовые не настроены. Добавьте OPENAI_API_KEY в .env")
        return
    wallet = await get_active_wallet(message.from_user.id)
    thinking = await message.answer("🎙 ...")
    text = await voice_to_text(bot, message.voice.file_id)
    if not text:
        await thinking.edit_text("❌ Не распознано.")
        return
    result = await categorize_with_ai(text)
    tx_info = await save_transaction(message.from_user.id, wallet["id"], result, "voice", text)

    tp = "Доход" if result["type"]=="income" else "Расход"
    sign = "+" if result["type"]=="income" else "-"
    cat = CATEGORIES.get(result["category"],{})
    resp = f"🎙 <i>«{text}»</i>\n\n✅ {tp}: <b>{sign}{fmt(tx_info['amount'])} {tx_info['currency']}</b>"
    if tx_info["currency"]!="UZS": resp += f"\n   ≈ {sign}{fmt(tx_info['amount_uzs'])} UZS"
    resp += f"\n{cat.get('icon','📦')} {cat.get('ru','Прочее')}\n👛 {wallet['icon']} {wallet['name']}"
    await thinking.edit_text(resp)


# ══════════════════════════════════════════════════════
#  WEB SERVER + API
# ══════════════════════════════════════════════════════
def check_admin_token(request):
    token = request.query.get("token","")
    return token == BOT_TOKEN[:20]

async def api_balance(request):
    uid = int(request.query.get("user_id",0))
    wid = request.query.get("wallet_id")
    period = request.query.get("period","month")
    b = await get_balance(uid, int(wid) if wid else None, period)
    return web.json_response(b)

async def api_stats(request):
    uid = int(request.query.get("user_id",0))
    wid = request.query.get("wallet_id")
    rows = await get_stats_by_category(uid, int(wid) if wid else None)
    total = sum(r["total"] for r in rows) if rows else 0
    cats = [{"category":r["category"],"name":CATEGORIES.get(r["category"],{}).get("ru",r["category"]),
             "total":r["total"],"count":r["cnt"],"pct":round(r["total"]/total*100,1) if total>0 else 0} for r in rows]
    return web.json_response({"categories":cats,"total":total})

async def api_transactions(request):
    uid = int(request.query.get("user_id",0))
    wid = request.query.get("wallet_id")
    limit = int(request.query.get("limit",50))
    txs = await get_recent_transactions(uid, int(wid) if wid else None, limit)
    return web.json_response([{
        "id":t["id"],"type":t["type"],"category":t["category"],
        "cat_name":CATEGORIES.get(t["category"],{}).get("ru","?"),
        "amount":t["amount"],"currency":t["currency"],"amount_uzs":t["amount_uzs"],
        "description":t["description"],"original_text":t["original_text"],
        "source":t["source"],"created_at":t["created_at"]
    } for t in txs])

async def api_wallets(request):
    uid = int(request.query.get("user_id",0))
    wallets = await get_user_wallets(uid)
    active = await get_active_wallet(uid)
    return web.json_response({
        "wallets":[{"id":w["id"],"name":w["name"],"icon":w["icon"]} for w in wallets],
        "active_id": active["id"] if active else 0
    })

async def api_admin_users(request):
    if not check_admin_token(request): return web.json_response({"error":"unauthorized"},status=401)
    users = await get_all_users()
    return web.json_response([{
        "telegram_id":u["telegram_id"],"username":u["username"],"first_name":u["first_name"],
        "is_approved":u["is_approved"],"is_admin":u["is_admin"],"is_blocked":u["is_blocked"],
        "tx_count":u["tx_count"],"total_expense":u["total_expense"],"total_income":u["total_income"],
        "created_at":u["created_at"]
    } for u in users])

async def api_admin_action(request):
    if not check_admin_token(request): return web.json_response({"error":"unauthorized"},status=401)
    data = await request.json()
    uid = data.get("user_id")
    action = data.get("action")
    if action=="approve": await approve_user(uid)
    elif action=="block": await block_user(uid)
    elif action=="revoke": await revoke_user(uid)
    return web.json_response({"ok":True})

async def serve_index(request):
    for d in [_root/"webapp", _root]:
        f = d/"index.html"
        if f.exists(): return web.FileResponse(f)
    return web.Response(text="Not found",status=404)

async def serve_admin(request):
    for d in [_root/"webapp", _root]:
        f = d/"admin.html"
        if f.exists(): return web.FileResponse(f)
    return web.Response(text="Not found",status=404)

def create_web_app():
    app = web.Application()
    app.router.add_get("/", serve_index)
    app.router.add_get("/admin", serve_admin)
    app.router.add_get("/api/balance", api_balance)
    app.router.add_get("/api/stats", api_stats)
    app.router.add_get("/api/transactions", api_transactions)
    app.router.add_get("/api/wallets", api_wallets)
    app.router.add_get("/api/admin/users", api_admin_users)
    app.router.add_post("/api/admin/action", api_admin_action)
    if WEBAPP_DIR.exists(): app.router.add_static("/static/", WEBAPP_DIR)
    return app


# ══════════════════════════════════════════════════════
#  START
# ══════════════════════════════════════════════════════
async def on_startup():
    await init_db()
    await update_exchange_rates()
    await bot.set_my_commands([
        BotCommand(command="start",description="Start"),
        BotCommand(command="balance",description="Balance"),
        BotCommand(command="wallet",description="Wallets"),
        BotCommand(command="stats",description="Statistics"),
        BotCommand(command="history",description="History"),
        BotCommand(command="today",description="Today"),
        BotCommand(command="undo",description="Undo"),
        BotCommand(command="language",description="Language"),
        BotCommand(command="admin",description="Admin"),
        BotCommand(command="help",description="Help"),
    ])
    me = await bot.get_me()
    log.info("")
    log.info("=" * 40)
    log.info(f"  Pulim Bot v3")
    log.info(f"  @{me.username}")
    log.info(f"  USD={exchange_rates['USD']} EUR={exchange_rates['EUR']}")
    log.info(f"  Mini App: http://localhost:{WEB_PORT}")
    log.info(f"  Admin: http://localhost:{WEB_PORT}/admin?token={BOT_TOKEN[:20]}")
    log.info(f"  Admin IDs: {ADMIN_IDS}")
    log.info("=" * 40)

async def main():
    dp.include_router(router)
    dp.startup.register(on_startup)
    wa = create_web_app()
    runner = web.AppRunner(wa)
    await runner.setup()
    site = web.TCPSite(runner,"0.0.0.0",WEB_PORT)
    await site.start()
    try: await dp.start_polling(bot)
    finally: await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
