import json
import os
import random
import re
import asyncio
import math
import hashlib
import pytz
from datetime import datetime
from typing import Any, Dict, Optional, List, Tuple

from aiogram import Bot, Dispatcher, executor, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from dotenv import load_dotenv

# ================== ENV ==================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Kanal IDlar (K1 baza, K2 biznes)
CHANNEL1_ID = int(os.getenv("BASE_CHANNEL_ID", "0"))
CHANNEL2_ID = int(os.getenv("BUSINESS_CHANNEL_ID", "0"))
CHANNEL3_ID = int(os.getenv("TRAILER_CHANNEL_ID", "0"))

# Majburiy obuna (1 ta kanal)
FORCE_SUB_1_ID = int(os.getenv("FORCE_SUB_1_ID", "0"))
FORCE_SUB_1_LINK = os.getenv("FORCE_SUB_1_LINK", "")
FORCE_SUB_ENABLED = (os.getenv("FORCE_SUB_ENABLED", "true").lower() == "true")

BOT_USERNAME = (os.getenv("BOT_USERNAME") or "").lstrip("@").strip()
MOVIES_FILE = os.getenv("MOVIES_FILE", "movies.json")
STATS_FILE = os.getenv("STATS_FILE", "statistics.json")

AUTOPOST_FILE = os.getenv("AUTOPOST_FILE", "autopost.json")
TZ_NAME = os.getenv("TZ", "Asia/Tashkent")
TZ = pytz.timezone(TZ_NAME)

ADMINS = {ADMIN_ID}

# ================== BOT ==================
bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot, storage=MemoryStorage())

# ================== XOTIRA ==================
# Yakuniy talab:
# - Yakka film: tugma 1 marta ishlasin (bosilgandan keyin eskirsin)
# - Serial: epizod tugmalari xohlagancha ishlasin
last_movie_request: Dict[int, str] = {}     # {user_id: code}
last_watch_token: Dict[int, str] = {}       # {user_id: token}

# ================== EDIT BANNER ==================
MOVIE_BANNER = "♻️ Yangilandi"
SERIES_BANNER = "♻️ Yangi qismi qo'shildi yoki sifatli formatga almashtirildi"
BANNER_RE = re.compile(r"^♻️ .*?\n\n", re.IGNORECASE)

def _apply_edit_banner(caption: str, banner_text: str) -> str:
    cap = (caption or "").strip()
    cap = BANNER_RE.sub("", cap).strip()
    if not cap:
        return banner_text
    return f"{banner_text}\n\n{cap}"

# ================== PATH HELPERS ==================
def _ensure_parent_dir(path: str) -> None:
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    except Exception:
        pass

# ================== JSON (atomic) ==================
def _atomic_write_json(path: str, data: Any) -> None:
    _ensure_parent_dir(path)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

# ================== DB ==================
def load_db() -> Dict[str, Any]:
    if not os.path.exists(MOVIES_FILE):
        return {}
    try:
        with open(MOVIES_FILE, "r", encoding="utf-8") as f:
            db = json.load(f)
    except Exception:
        return {}

    # Backward compatibility (eski movies.json):
    fixed: Dict[str, Any] = {}
    for code, item in (db or {}).items():
        if not isinstance(item, dict):
            continue
        if "type" not in item:
            fixed[code] = {
                "type": "movie",
                "post_file_id": item.get("post_file_id"),
                "post_caption": item.get("post_caption", ""),
                "video_file_id": item.get("video_file_id"),
                "video_unique_id": item.get("video_unique_id"),
                "channel_msg_id": item.get("channel_msg_id"),
            }
        else:
            fixed[code] = item
    return fixed

def save_db(data: Dict[str, Any]) -> None:
    _atomic_write_json(MOVIES_FILE, data)

# ================== HLL (unique users approx, fixed size) ==================
# statistics.json users massivini olib tashlaymiz.
# unique_users_count taxminiy, lekin fayl hajmi o'smaydi.
HLL_P = 10  # 2^10 = 1024 registers
HLL_M = 1 << HLL_P
HLL_ALPHA = 0.7213 / (1 + 1.079 / HLL_M)

def _hll_init() -> List[int]:
    return [0] * HLL_M

def _hll_hash_64(x: int) -> int:
    # stable 64-bit from sha1
    h = hashlib.sha1(str(x).encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big")

def _rho(w: int, max_bits: int = 64) -> int:
    # position of first 1-bit in w (from MSB side of remaining bits)
    # return in [1..max_bits]
    if w == 0:
        return max_bits + 1
    # count leading zeros in max_bits space
    lz = (max_bits - w.bit_length())
    return lz + 1

def hll_add(regs: List[int], user_id: int) -> None:
    x = _hll_hash_64(user_id)
    idx = x >> (64 - HLL_P)
    w = (x << HLL_P) & ((1 << 64) - 1)
    r = _rho(w, 64 - HLL_P)
    if r > regs[idx]:
        regs[idx] = r

def hll_estimate(regs: List[int]) -> int:
    inv_sum = 0.0
    zeros = 0
    for v in regs:
        inv_sum += 2.0 ** (-v)
        if v == 0:
            zeros += 1
    raw = HLL_ALPHA * (HLL_M ** 2) / inv_sum

    # small range correction (linear counting)
    if raw <= 2.5 * HLL_M and zeros > 0:
        raw = HLL_M * math.log(HLL_M / zeros)

    return int(raw)

# ================== STATISTIKA ==================
def load_stats() -> Dict[str, Any]:
    if not os.path.exists(STATS_FILE):
        return {
            "total_requests": 0,
            "today": {"date": datetime.now(TZ).strftime("%Y-%m-%d"), "count": 0},
            "hll_p": HLL_P,
            "hll_regs": _hll_init(),
        }
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
            # migrate if old format had users list
            if "users" in st:
                # migrate users list into HLL, then remove it
                regs = _hll_init()
                try:
                    for uid in st.get("users", []):
                        if isinstance(uid, int):
                            hll_add(regs, uid)
                except Exception:
                    pass
                st.pop("users", None)
                st["hll_p"] = HLL_P
                st["hll_regs"] = regs
            if "hll_regs" not in st or not isinstance(st.get("hll_regs"), list) or len(st["hll_regs"]) != HLL_M:
                st["hll_p"] = HLL_P
                st["hll_regs"] = _hll_init()
            if "today" not in st:
                st["today"] = {"date": datetime.now(TZ).strftime("%Y-%m-%d"), "count": 0}
            if "total_requests" not in st:
                st["total_requests"] = 0
            return st
    except Exception:
        return {
            "total_requests": 0,
            "today": {"date": datetime.now(TZ).strftime("%Y-%m-%d"), "count": 0},
            "hll_p": HLL_P,
            "hll_regs": _hll_init(),
        }

def save_stats(data: Dict[str, Any]) -> None:
    _atomic_write_json(STATS_FILE, data)

def update_stats(user_id: int) -> None:
    stats = load_stats()
    today = datetime.now(TZ).strftime("%Y-%m-%d")

    stats["total_requests"] = int(stats.get("total_requests", 0)) + 1

    if stats.get("today", {}).get("date") != today:
        stats["today"] = {"date": today, "count": 1}
    else:
        stats["today"]["count"] = int(stats["today"].get("count", 0)) + 1

    regs = stats.get("hll_regs")
    if not isinstance(regs, list) or len(regs) != HLL_M:
        regs = _hll_init()
    hll_add(regs, user_id)
    stats["hll_regs"] = regs
    save_stats(stats)

# ================== AVTOKOD ==================
def generate_unique_code(db: Dict[str, Any]) -> str:
    while True:
        code = str(random.randint(1000, 9999))
        if code not in db:
            return code

# ================== OBUNA ==================
async def check_subscription(user_id: int) -> bool:
    if not FORCE_SUB_ENABLED:
        return True
    try:
        member1 = await bot.get_chat_member(FORCE_SUB_1_ID, user_id)
        ok1 = member1.status in ("member", "administrator", "creator")
        return ok1
    except Exception:
        return False

def subscribe_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🔔 Kanalga obuna bo‘lish", url=FORCE_SUB_1_LINK),
        types.InlineKeyboardButton("🎞 Treyler kanal", url=os.getenv("FORCE_SUB_2_LINK","")),
        types.InlineKeyboardButton("✅ Tekshirish", callback_data="check_sub")
    )
    return kb

# upgraded
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🔔 Kanalga obuna bo‘lish", url=FORCE_SUB_1_LINK),
        types.InlineKeyboardButton("✅ Tekshirish", callback_data="check_sub")
    )
    return kb

# ================== MENULAR ==================
def user_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("🎬 Qidiruv")
    return kb

def admin_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("➕ Kino qo‘shish", "➕ Serial qo‘shish")
    kb.row("✏️ Tahrirlash", "🗑 O‘chirish")
    kb.row("🎬 Qidiruv", "📊 Statistika")
    kb.row("📦 Kino backup", "📈 Statistika backup")
    kb.row("♻️ Kino restore", "♻️ Statistika restore")
    kb.row("📣 Kanalga yuborish", "⏰ Avtopost")
    kb.row("❌ Bekor qilish")
    return kb

def is_admin(uid: int) -> bool:
    return uid in ADMINS

def protect_for(uid: int) -> bool:
    # oddiy user yopiq, admin ochiq
    return not is_admin(uid)

# ================== FSM ==================
class AddMovie(StatesGroup):
    post = State()
    video = State()

class AddSeries(StatesGroup):
    poster = State()
    episodes = State()

class EditFlow(StatesGroup):
    choose_type = State()
    choose_code = State()
    choose_action = State()
    await_forward = State()
    await_ep_delete = State()
    trailer = State()  # NEW

class DeleteFlow(StatesGroup):
    code = State()

class RestoreFlow(StatesGroup):
    movies = State()
    stats = State()

class PublishLater(StatesGroup):
    code = State()

class AutoPostFlow(StatesGroup):
    menu = State()
    add_time = State()
    add_code = State()
    edit_id = State()
    edit_choose = State()
    edit_time = State()
    edit_code = State()
    del_id = State()

# ================== HELPERS ==================
CODE_LINE_RE = re.compile(r"(🆔\s*Kod:\s*([0-9]{4}))", re.IGNORECASE)

def _ensure_code_line_kept(new_caption: str, old_caption_with_code: str, code: str) -> str:
    m = CODE_LINE_RE.search(old_caption_with_code or "")
    code_line = m.group(1) if m else f"🆔 Kod: {code}"
    cleaned = CODE_LINE_RE.sub("", (new_caption or "")).strip()
    return f"{cleaned}\n\n{code_line}".strip() if cleaned else code_line

def _duplicate_video_exists(db: Dict[str, Any], video_unique_id: str) -> bool:
    for it in db.values():
        if it.get("type") == "movie":
            if it.get("video_unique_id") == video_unique_id:
                return True
        elif it.get("type") == "series":
            for epv in (it.get("episodes", {}) or {}).values():
                if isinstance(epv, dict) and epv.get("video_unique_id") == video_unique_id:
                    return True
    return False

async def _is_forward_from_base(message: types.Message) -> bool:
    return bool(message.forward_from_chat and int(message.forward_from_chat.id) == int(CHANNEL1_ID))

def _parse_episode_caption(caption: str) -> Tuple[Optional[int], str]:
    """
    QOIDALAR:
    - Birinchi uchragan raqam -> qism raqami
    - Qolgan matn -> nom (ichidagi boshqa raqamlar ahamiyatsiz)
    """
    if not caption:
        return None, ""
    text = caption.strip()
    m = re.search(r"\d+", text)
    if not m:
        return None, text
    ep = int(m.group(0))
    title = (text[:m.start()] + text[m.end():]).strip()
    title = re.sub(r"^[\s\|\-:–—]+", "", title).strip()
    return ep, title

def _episode_user_caption(ep: int, title: str) -> str:
    title = (title or "").strip()
    if title:
        return f"{ep}-qisim({title})"
    return f"{ep}-qisim"

def _sorted_episode_numbers(item: Dict[str, Any]) -> List[int]:
    eps = item.get("episodes", {}) or {}
    nums: List[int] = []
    for k in eps.keys():
        if str(k).isdigit():
            nums.append(int(k))
    return sorted(nums)

# ================== INLINE KB ==================
def movie_watch_kb(code: str, token: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🎬 Filmni ko‘rish", callback_data=f"watch2_{code}_{token}"))
    return kb

def channel_movie_kb(code: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🎬 Filmni bot orqali ko‘rish", url=f"https://t.me/{BOT_USERNAME}?start={code}"))
    return kb

def channel_series_kb(code: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📺 Barcha qismlari", url=f"https://t.me/{BOT_USERNAME}?start=series_{code}"))
    return kb

def series_eps_kb(code: str, eps: List[int]) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=5)
    kb.add(*[types.InlineKeyboardButton(str(n), callback_data=f"series_ep:{code}:{n}") for n in eps])
    return kb

def edited_done_kb(code: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("✏️ Eskini tahrirlash", callback_data=f"edit_again:{code}"),
        types.InlineKeyboardButton("📣 Kanalga qayta yuborish", callback_data=f"republish:{code}"),
    )
    return kb

# ================== AUTPOST STORAGE ==================
def load_autopost() -> Dict[str, Any]:
    if not os.path.exists(AUTOPOST_FILE):
        return {"meta": {"daily_done_sent": {}}, "jobs": []}
    try:
        with open(AUTOPOST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            # migrate old list format -> dict
            return {"meta": {"daily_done_sent": {}}, "jobs": data}
        if not isinstance(data, dict):
            return {"meta": {"daily_done_sent": {}}, "jobs": []}
        data.setdefault("meta", {"daily_done_sent": {}})
        data.setdefault("jobs", [])
        if not isinstance(data["jobs"], list):
            data["jobs"] = []
        if not isinstance(data["meta"], dict):
            data["meta"] = {"daily_done_sent": {}}
        data["meta"].setdefault("daily_done_sent", {})
        return data
    except Exception:
        return {"meta": {"daily_done_sent": {}}, "jobs": []}

def save_autopost(data: Dict[str, Any]) -> None:
    _atomic_write_json(AUTOPOST_FILE, data)

def _ap_new_id(jobs: List[Dict[str, Any]]) -> str:
    # simple unique id
    while True:
        x = random.randint(1000, 9999)
        apid = f"AP-{x}"
        if all(j.get("id") != apid for j in jobs):
            return apid

def _parse_dt_local(s: str) -> Optional[datetime]:
    try:
        naive = datetime.strptime(s.strip(), "%Y-%m-%d %H:%M")
        return TZ.localize(naive)
    except Exception:
        return None

def autopost_menu_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("➕ Rejalashtirish", "📋 Rejalashtirilganlar")
    kb.row("✏️ Tahrirlash", "🗑 O‘chirish")
    kb.row("❌ Bekor qilish")
    return kb

def autopost_edit_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🕒 Vaqtni o‘zgartirish", callback_data="ap_edit_time"),
        types.InlineKeyboardButton("🎬 Kinoni almashtirish", callback_data="ap_edit_code"),
        types.InlineKeyboardButton("❌ Bekor qilish", callback_data="ap_edit_cancel"),
    )
    return kb

# ================== PUBLISH HELPERS ==================
async def publish_to_channel(code: str) -> Tuple[bool, str]:
    """
    Returns (ok, message)
    - ok True => published
    - ok False => reason
    """
    db = load_db()
    item = db.get(code)
    if not item:
        return False, "❌ Bunaqa kino o'zi yo'q tog'o"

    if item.get("channel_msg_id"):
        return False, "⚠️ Bu kino kanalda bor tog'o. Dublikat chiqarmaymiz."

    if item.get("type") == "movie":
        caption = f"{(item.get('post_caption') or '').strip()}\n\n🆔 Kod: {code}".strip()
        msg = await bot.send_photo(
            CHANNEL2_ID,
            item["post_file_id"],
            caption=caption,
            reply_markup=channel_movie_kb(code),
            # Kanal postlari ochiq qoladi -> protect_content bermaymiz
        )
        item["channel_msg_id"] = msg.message_id
        db[code] = item
        save_db(db)
        return True, "🚀 Kanalga keeetti tog'o"

    if item.get("type") == "series":
        caption = f"{(item.get('poster_caption') or '').strip()}\n\n🆔 Kod: {code}".strip()
        msg = await bot.send_photo(
            CHANNEL2_ID,
            item["poster_file_id"],
            caption=caption,
            reply_markup=channel_series_kb(code),
        )
        item["channel_msg_id"] = msg.message_id
        db[code] = item
        save_db(db)
        return True, "🚀 Kanalga keeetti tog'o"

    return False, "❌ Topilmadi"

# ================== AUTPOST WATCHDOG ==================
async def autopost_loop():
    while True:
        try:
            data = load_autopost()
            jobs: List[Dict[str, Any]] = data.get("jobs", [])
            meta: Dict[str, Any] = data.get("meta", {})
            daily_done_sent: Dict[str, Any] = meta.get("daily_done_sent", {})

            now = datetime.now(TZ)
            changed = False

            # process due jobs
            for job in jobs:
                if job.get("status") not in (None, "pending"):
                    continue
                run_at = _parse_dt_local(job.get("run_at", ""))
                code = str(job.get("code", "")).strip()
                if not run_at or not code.isdigit():
                    job["status"] = "cancelled"
                    job["note"] = "bad job data"
                    changed = True
                    continue

                if run_at <= now:
                    ok, msg = await publish_to_channel(code)
                    job["status"] = "done" if ok else "skipped"
                    job["done_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
                    job["result"] = msg
                    changed = True

                    # Admin log
                    try:
                        if ok:
                            await bot.send_message(
                                ADMIN_ID,
                                f"🚀 Avtopost chiqdi tog'o\n\n🎬 Kod: {code}\n⏰ Vaqt: {run_at.strftime('%H:%M')}\n📣 Kanalga muvaffaqiyatli joylandi",
                            )
                        else:
                            await bot.send_message(
                                ADMIN_ID,
                                f"⚠️ Avtopost bekor qilindi tog'o\n\n🎬 Kod: {code}\nSabab: {msg}",
                            )
                    except Exception:
                        pass

            # daily completion check (for dates present in jobs)
            # If for a date all jobs are finished (done/skipped/cancelled) and not notified -> notify admin once.
            dates = set()
            for job in jobs:
                run_at = _parse_dt_local(job.get("run_at", ""))
                if run_at:
                    dates.add(run_at.strftime("%Y-%m-%d"))

            for d in sorted(dates):
                if str(daily_done_sent.get(d, "")).lower() == "true":
                    continue
                day_jobs = [j for j in jobs if (_parse_dt_local(j.get("run_at", "")) and _parse_dt_local(j.get("run_at", "")).strftime("%Y-%m-%d") == d)]
                if not day_jobs:
                    continue
                if all(j.get("status") in ("done", "skipped", "cancelled") for j in day_jobs):
                    # notify
                    try:
                        await bot.send_message(
                            ADMIN_ID,
                            "📅 Bugungi avtopostlar tugadi tog'o\n\nBugun rejalashtirilgan barcha kinolar tekshirildi va yakunlandi.",
                        )
                    except Exception:
                        pass
                    daily_done_sent[d] = True
                    meta["daily_done_sent"] = daily_done_sent
                    data["meta"] = meta
                    changed = True

            # optional cleanup: keep finished jobs for history, or prune old ones
            # We'll keep last 200 jobs max (to prevent file growth)
            if len(jobs) > 200:
                jobs_sorted = sorted(jobs, key=lambda j: j.get("created_at", ""))
                data["jobs"] = jobs_sorted[-200:]
                changed = True

            if changed:
                save_autopost(data)

        except Exception:
            # don't crash loop
            pass

        await asyncio.sleep(20)

# ================== BEKOR (har qanday holatda) ==================
@dp.message_handler(lambda m: (m.text or "").strip() == "❌ Bekor qilish" or ("bekor" in (m.text or "").lower()), state="*")
async def cancel_anytime(message: types.Message, state: FSMContext):
    await state.finish()
    if is_admin(message.from_user.id):
        await message.answer("❎ Bekor qilindi tog'o", reply_markup=admin_menu())
    else:
        await message.answer("❎ Bekor qilindi", reply_markup=user_menu())

# ================== START ==================
@dp.message_handler(commands=["start"])
async def start_cmd(message: types.Message, state: FSMContext):
    await state.finish()

    args = (message.get_args() or "").strip()

    if args.startswith("series_"):
        code = args.replace("series_", "").strip()
        if code.isdigit():
            await send_series_to_user(message.from_user.id, code)
            return

    if args.isdigit():
        message.text = args
        await search_movie(message)
        return

    if is_admin(message.from_user.id):
        await message.answer("👑 <b>Admin panel</b>", reply_markup=admin_menu())
    else:
        await message.answer("🎬 Kino kodini yuboring", reply_markup=user_menu())

# ================== QIDIRUV ==================
@dp.message_handler(lambda m: m.text == "🎬 Qidiruv")
async def search_btn(message: types.Message):
    kb = admin_menu() if is_admin(message.from_user.id) else user_menu()
    await message.answer("🔎 Kino kodini yuboring", reply_markup=kb)

# ================== KINO QO‘SHISH (YAKKA) ==================
@dp.message_handler(lambda m: m.text == "➕ Kino qo‘shish")
async def add_movie_btn(message: types.Message):
    if message.from_user.id not in ADMINS:
        await message.answer(
            "❌ <b>Brat siz admin emassiz!</b>\n"
            "🎬 Faqat <b>Qidiruv</b> tugmasidan foydalanishingiz mumkin.",
            reply_markup=user_menu()
        )
        return
    await message.answer("📨 Rasm-pasimlarini tashang", reply_markup=admin_menu())
    await AddMovie.post.set()

@dp.message_handler(content_types=types.ContentType.PHOTO, state=AddMovie.post)
async def add_post(message: types.Message, state: FSMContext):
    db = load_db()
    code = generate_unique_code(db)

    await state.update_data(
        code=code,
        post_file_id=message.photo[-1].file_id,
        post_caption=message.caption or ""
    )

    await message.answer(f"🆔 <b>Kino kodi avtomatik berildi:</b> {code}\n\n🎥 Endi video tashang", reply_markup=admin_menu())
    await AddMovie.video.set()

@dp.message_handler(content_types=types.ContentType.VIDEO, state=AddMovie.video)
async def add_video(message: types.Message, state: FSMContext):
    db = load_db()

    if _duplicate_video_exists(db, message.video.file_unique_id):
        await message.answer("❗ Bu kino borku tog'o", reply_markup=admin_menu())
        await state.finish()
        return

    data = await state.get_data()
    code = data["code"]

    db[code] = {
        "type": "movie",
        "post_file_id": data["post_file_id"],
        "post_caption": data["post_caption"],
        "video_file_id": message.video.file_id,
        "video_unique_id": message.video.file_unique_id,
        "channel_msg_id": None
    }
    save_db(db)

    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ Kanalga jo'nataymi", callback_data=f"publish_movie:{code}"),
        types.InlineKeyboardButton("❌ Yo jo'natmayinmi?", callback_data="cancel_send")
    )

    await message.answer(f"✅ Kino saqlandi\n🆔 Kod: {code}\n\nKanalga yuboraymi?", reply_markup=kb)
    await state.finish()

# ================== SERIAL QO‘SHISH ==================
@dp.message_handler(lambda m: m.text == "➕ Serial qo‘shish")
async def add_series_btn(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer(
            "❌ <b>Brat siz admin emassiz!</b>\n"
            "🎬 Faqat <b>Qidiruv</b> tugmasidan foydalanishingiz mumkin.",
            reply_markup=user_menu()
        )
        return
    await message.answer("📨 Serial posteri (rasm + caption)ni yuboring", reply_markup=admin_menu())
    await AddSeries.poster.set()

@dp.message_handler(content_types=types.ContentType.PHOTO, state=AddSeries.poster)
async def add_series_poster(message: types.Message, state: FSMContext):
    db = load_db()
    code = generate_unique_code(db)

    await state.update_data(
        code=code,
        poster_file_id=message.photo[-1].file_id,
        poster_caption=message.caption or "",
        episodes={}
    )

    await message.answer(
        f"🆔 <b>Kino kodi avtomatik berildi:</b> {code}\n\n"
        "Endi Kanal1 (baza)dan videoni forward qiling.\n"
        "Caption misol: <b>1 Yura davri 3</b> yoki <b>7 | Forsaj: G'azablangan</b>\n\n"
        "Tugatish uchun <b>Ha</b> deb yozing.",
        reply_markup=admin_menu()
    )
    await AddSeries.episodes.set()

@dp.message_handler(lambda m: (m.text or "").strip().lower() == "ha", state=AddSeries.episodes)
async def add_series_finish(message: types.Message, state: FSMContext):
    data = await state.get_data()
    episodes = data.get("episodes", {})

    if not episodes:
        await message.answer("❗ Hech bo‘lmasa bitta qism qo‘shing.", reply_markup=admin_menu())
        return

    db = load_db()
    code = data["code"]

    db[code] = {
        "type": "series",
        "poster_file_id": data["poster_file_id"],
        "poster_caption": data["poster_caption"],
        "episodes": episodes,
        "channel_msg_id": None
    }
    save_db(db)

    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ Kanalga jo'nataymi", callback_data=f"publish_series:{code}"),
        types.InlineKeyboardButton("❌ Yo jo'natmayinmi?", callback_data="cancel_send")
    )

    await message.answer(f"✅ Kino saqlandi\n🆔 Kod: {code}\n\nKanalga yuboraymi?", reply_markup=kb)
    await state.finish()

@dp.message_handler(content_types=types.ContentType.VIDEO, state=AddSeries.episodes)
async def add_series_episode(message: types.Message, state: FSMContext):
    if not await _is_forward_from_base(message):
        await message.answer("❗ Iltimos, <b>Kanal1 (baza)</b>dan forward qiling.", reply_markup=admin_menu())
        return

    ep_num, ep_title = _parse_episode_caption(message.caption or "")
    if ep_num is None:
        await message.answer("❗ Video captionida qism raqami yo‘q.\nMasalan: <b>1 Yura davri 3</b>", reply_markup=admin_menu())
        return

    db = load_db()
    if _duplicate_video_exists(db, message.video.file_unique_id):
        await message.answer("❗ Bu kino borku tog'o", reply_markup=admin_menu())
        return

    data = await state.get_data()
    episodes: Dict[str, Any] = data.get("episodes", {})

    episodes[str(ep_num)] = {
        "video_file_id": message.video.file_id,
        "video_unique_id": message.video.file_unique_id,
        "title": (ep_title or "").strip()
    }

    await state.update_data(episodes=episodes)
    await message.answer(f"✅ Qabul qilindi: <b>{ep_num}-qisim</b>", reply_markup=admin_menu())

@dp.message_handler(state=AddSeries.episodes, content_types=types.ContentType.TEXT)
async def add_series_text_in_episodes(message: types.Message, state: FSMContext):
    await message.answer(
        "🎥 Kanal1 (baza)dan videoni forward qiling.\n"
        "Tugatish uchun <b>Ha</b> deb yozing.",
        reply_markup=admin_menu()
    )

    #=================== TREYLER QO'SHISH ===================
@dp.callback_query_handler(lambda c: c.data.startswith("add_trailer:"), state=EditFlow.choose_action)
async def add_trailer_start(call: types.CallbackQuery, state: FSMContext):
    code = call.data.split(":")[1]
    await state.update_data(code=code)
    await call.message.answer("🎞 Treyler videoni yuboring", reply_markup=admin_menu())
    await EditFlow.trailer.set()
    await call.answer()


@dp.message_handler(content_types=types.ContentType.VIDEO, state=EditFlow.trailer)
async def add_trailer_video(message: types.Message, state: FSMContext):
    data = await state.get_data()
    code = data["code"]

    msg = await bot.send_video(
        CHANNEL3_ID,
        message.video.file_id,
        caption=message.caption or ""
    )

    db = load_db()
    item = db.get(code)

    item["trailer"] = {
        "exists": True,
        "msg_id": msg.message_id
    }

    db[code] = item
    save_db(db)

    # 🔄 kanal postni yangilash
    ch_id = item.get("channel_msg_id")
    if ch_id:
        try:
            await bot.edit_message_reply_markup(
                CHANNEL2_ID,
                ch_id,
                reply_markup=channel_movie_kb(code)
            )
        except:
            pass

    await message.answer("✅ Treyler qo‘shildi", reply_markup=edited_done_kb(code))
    await state.finish()

# ================== KANALGA YUBORISH ==================
@dp.callback_query_handler(lambda c: c.data == "cancel_send")
async def cancel_send(call: types.CallbackQuery):
    await call.message.edit_text("❎ Bekor qilindi")
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("publish_movie:"))
async def publish_movie(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("❌ Brat, bu joy adminniki 😄", show_alert=True)
        return

    code = call.data.split(":", 1)[1]
    ok, msg = await publish_to_channel(code)
    await call.message.edit_text(msg if ok else msg)
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("publish_series:"))
async def publish_series(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("❌ Brat, bu joy adminniki 😄", show_alert=True)
        return

    code = call.data.split(":", 1)[1]
    ok, msg = await publish_to_channel(code)
    await call.message.edit_text(msg if ok else msg)
    await call.answer()

    #============== TREYLERNI O'CHIRISH ==================
    @dp.callback_query_handler(lambda c: c.data.startswith("del_trailer:"), state=EditFlow.choose_action)
async def delete_trailer(call: types.CallbackQuery, state: FSMContext):
    code = call.data.split(":")[1]

    db = load_db()
    item = db.get(code)

    item["trailer"] = {
        "exists": False,
        "msg_id": None
    }

    db[code] = item
    save_db(db)

    # 🔄 kanal postdan tugmani olib tashlaymiz
    ch_id = item.get("channel_msg_id")
    if ch_id:
        try:
            await bot.edit_message_reply_markup(
                CHANNEL2_ID,
                ch_id,
                reply_markup=channel_movie_kb(code)
            )
        except:
            pass

    await call.message.answer("🗑 Treyler o‘chirildi", reply_markup=edited_done_kb(code))
    await state.finish()
    await call.answer()

# ================== QIDIRISH (KOD) ==================
@dp.message_handler(lambda m: m.text and m.text.strip().isdigit())
async def search_movie(message: types.Message):
    kb = admin_menu() if is_admin(message.from_user.id) else user_menu()

    if not await check_subscription(message.from_user.id):
        await message.answer("❗ Avval kanalga obuna bo‘ling", reply_markup=subscribe_kb())
        return

    db = load_db()
    code = message.text.strip()
    item = db.get(code)

    if not item:
        await message.answer("❌ Bunday kodli kino topilmadi", reply_markup=kb)
        return

    update_stats(message.from_user.id)

    if item.get("type") == "movie":
        token = str(random.randint(100000, 999999))
        last_movie_request[message.from_user.id] = code
        last_watch_token[message.from_user.id] = token

        await message.answer_photo(
            item["post_file_id"],
            item.get("post_caption", ""),
            reply_markup=movie_watch_kb(code, token),
            protect_content=protect_for(message.from_user.id)
        )
        return

    await message.answer_photo(
        item["poster_file_id"],
        item.get("poster_caption", ""),
        reply_markup=types.InlineKeyboardMarkup().add(
            types.InlineKeyboardButton("📺 Barcha qismlari", callback_data=f"series_private:{code}")
        ),
        protect_content=protect_for(message.from_user.id)
    )

# ================== FILMNI KO‘RISH (YAKKA) ==================
@dp.callback_query_handler(lambda c: c.data.startswith("watch_"))
async def watch_old(call: types.CallbackQuery):
    await call.answer(
        "❗ Tugma eskirgan. Faqat oxirgi so'ralgan filmni ko'rishingiz mumkin. "
        "Ushbu filmni ko'rish uchun esa kod orqali qayta qidiring yoki "
        "kanaldagi bu film posti ostidagi ko'rish tugmasini bosing ",
        show_alert=True
    )

@dp.callback_query_handler(lambda c: c.data.startswith("watch2_"))
async def watch_movie(call: types.CallbackQuery):
    parts = call.data.split("_", 2)
    if len(parts) != 3:
        await call.answer("❌ Topilmadi", show_alert=True)
        return

    code = parts[1]
    token = parts[2]

    if last_movie_request.get(call.from_user.id) != code or last_watch_token.get(call.from_user.id) != token:
        await call.answer(
            "❗ Tugma eskirgan. Faqat oxirgi so'ralgan filmni ko'rishingiz mumkin. "
            "Ushbu filmni ko'rish uchun esa kod orqali qayta qidiring yoki "
            "kanaldagi bu film posti ostidagi ko'rish tugmasini bosing ",
            show_alert=True
        )
        return

    if not await check_subscription(call.from_user.id):
        await call.message.answer("❗ Avval kanalga obuna bo‘lingda", reply_markup=subscribe_kb())
        await call.answer()
        return

    db = load_db()
    item = db.get(code)
    if not item or item.get("type") != "movie":
        await call.answer("❌ Topilmadi", show_alert=True)
        return

    await bot.send_video(
        call.from_user.id,
        item["video_file_id"],
        protect_content=protect_for(call.from_user.id)
    )

    last_watch_token.pop(call.from_user.id, None)
    await call.answer()

# ================== SERIALNI USERGA YUBORISH (kanalga emas) ==================
async def send_series_to_user(user_id: int, code: str):
    if not await check_subscription(user_id):
        await bot.send_message(user_id, "❗ Avval kanalga obuna bo‘ling", reply_markup=subscribe_kb())
        return

    db = load_db()
    item = db.get(code)
    if not item or item.get("type") != "series":
        await bot.send_message(user_id, "❌ Bunday kodli kino topilmadi", reply_markup=user_menu())
        return

    ep_nums = _sorted_episode_numbers(item)
    if not ep_nums:
        await bot.send_message(user_id, "❌ Qismlar topilmadi", reply_markup=user_menu())
        return

    ch_msg_id = item.get("channel_msg_id")
    if ch_msg_id:
        await bot.copy_message(
            chat_id=user_id,
            from_chat_id=CHANNEL2_ID,
            message_id=ch_msg_id,
            reply_markup=series_eps_kb(code, ep_nums),
            protect_content=protect_for(user_id)
        )
    else:
        await bot.send_photo(
            chat_id=user_id,
            photo=item["poster_file_id"],
            caption=item.get("poster_caption", ""),
            reply_markup=series_eps_kb(code, ep_nums),
            protect_content=protect_for(user_id)
        )

@dp.callback_query_handler(lambda c: c.data.startswith("series_private:"))
async def series_private_from_bot(call: types.CallbackQuery):
    code = call.data.split(":", 1)[1]
    await send_series_to_user(call.from_user.id, code)
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("series_ep:"))
async def series_ep(call: types.CallbackQuery):
    _, code, ep_str = call.data.split(":")
    ep_num = int(ep_str)

    if not await check_subscription(call.from_user.id):
        await call.message.answer("❗ Avval kanalga obuna bo‘ling", reply_markup=subscribe_kb())
        await call.answer()
        return

    db = load_db()
    item = db.get(code)
    if not item or item.get("type") != "series":
        await call.answer("❌ Topilmadi", show_alert=True)
        return

    ep = (item.get("episodes", {}) or {}).get(str(ep_num))
    if not ep:
        await call.answer("❌ Topilmadi", show_alert=True)
        return

    cap = _episode_user_caption(ep_num, (ep or {}).get("title", ""))
    await bot.send_video(
        call.from_user.id,
        ep["video_file_id"],
        caption=cap,
        protect_content=protect_for(call.from_user.id)
    )
    await call.answer()

# ================== STATISTIKA ==================
def stats_text():
    stats = load_stats()
    db = load_db()
    movies_count = sum(1 for v in db.values() if v.get("type") == "movie")
    series_count = sum(1 for v in db.values() if v.get("type") == "series")
    unique_users = hll_estimate(stats.get("hll_regs", _hll_init()))

    return (
        "📊 <b>Bot statistikasi</b>\n\n"
        f"👥 Userlar: <b>{unique_users}</b>\n"
        f"🎬 Filmlar: <b>{movies_count}</b>\n"
        f"📺 Seriallar: <b>{series_count}</b>\n"
        f"📥 Bugun so‘rovlar: <b>{stats.get('today', {}).get('count', 0)}</b>\n"
        f"🔢 Jami so‘rovlar: <b>{stats.get('total_requests', 0)}</b>"
    )

def stats_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("🔄 Yangilash", callback_data="stats_refresh"),
        types.InlineKeyboardButton("❌ Yopish", callback_data="stats_close")
    )
    return kb

@dp.message_handler(lambda m: m.text == "📊 Statistika")
async def show_stats(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer(
            "❌ <b>Brat siz admin emassiz!</b>\n"
            "🎬 Faqat <b>Qidiruv</b> tugmasidan foydalanishingiz mumkin.",
            reply_markup=user_menu()
        )
        return
    await message.answer(stats_text(), reply_markup=stats_kb())

@dp.callback_query_handler(lambda c: c.data == "stats_refresh")
async def refresh_stats(call: types.CallbackQuery):
    await call.message.edit_text(stats_text(), reply_markup=stats_kb())
    await call.answer()

@dp.callback_query_handler(lambda c: c.data == "stats_close")
async def close_stats(call: types.CallbackQuery):
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.answer()

# ================== BACKUP ==================
@dp.message_handler(lambda m: m.text == "📦 Kino backup")
async def backup_movies(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer(
            "❌ <b>Brat siz admin emassiz!</b>\n"
            "🎬 Faqat <b>Qidiruv</b> tugmasidan foydalanishingiz mumkin.",
            reply_markup=user_menu()
        )
        return
    if not os.path.exists(MOVIES_FILE):
        await message.answer("❌ movies.json topilmadi", reply_markup=admin_menu())
        return
    await message.answer_document(types.InputFile(MOVIES_FILE), reply_markup=admin_menu())

@dp.message_handler(lambda m: m.text == "📈 Statistika backup")
async def backup_stats(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer(
            "❌ <b>Brat siz admin emassiz!</b>\n"
            "🎬 Faqat <b>Qidiruv</b> tugmasidan foydalanishingiz mumkin.",
            reply_markup=user_menu()
        )
        return
    if not os.path.exists(STATS_FILE):
        await message.answer("❌ statistics.json topilmadi", reply_markup=admin_menu())
        return
    await message.answer_document(types.InputFile(STATS_FILE), reply_markup=admin_menu())

# ================== RESTORE (ADMIN PANEL) ==================
@dp.message_handler(lambda m: m.text == "♻️ Kino restore")
async def restore_movies_btn(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer(
            "❌ <b>Brat siz admin emassiz!</b>\n"
            "🎬 Faqat <b>Qidiruv</b> tugmasidan foydalanishingiz mumkin.",
            reply_markup=user_menu()
        )
        return
    await state.finish()
    await message.answer(
        "♻️ <b>Kino restore</b>\n\n"
        "📎 Endi <b>movies.json</b> faylni shu botga yuboring (Document sifatida).",
        reply_markup=admin_menu()
    )
    await RestoreFlow.movies.set()

@dp.message_handler(lambda m: m.text == "♻️ Statistika restore")
async def restore_stats_btn(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer(
            "❌ <b>Brat siz admin emassiz!</b>\n"
            "🎬 Faqat <b>Qidiruv</b> tugmasidan foydalanishingiz mumkin.",
            reply_markup=user_menu()
        )
        return
    await state.finish()
    await message.answer(
        "♻️ <b>Statistika restore</b>\n\n"
        "📎 Endi <b>statistics.json</b> faylni shu botga yuboring (Document sifatida).",
        reply_markup=admin_menu()
    )
    await RestoreFlow.stats.set()

@dp.message_handler(state=RestoreFlow.movies, content_types=types.ContentType.DOCUMENT)
async def restore_movies_file(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.finish()
        return

    doc = message.document
    if not doc or (doc.file_name or "").lower() != "movies.json":
        await message.answer("❗ Faqat <b>movies.json</b> yuboring.", reply_markup=admin_menu())
        return

    try:
        f = await bot.get_file(doc.file_id)
        _ensure_parent_dir(MOVIES_FILE)
        await bot.download_file(f.file_path, MOVIES_FILE)
        _ = load_db()
        await message.answer(f"✅ Tiklandi!\n📌 Saqlandi: <code>{MOVIES_FILE}</code>", reply_markup=admin_menu())
    except Exception:
        await message.answer("❌ Restore bo‘lmadi. Fayl buzilgan yoki ruxsat muammosi bo‘lishi mumkin.", reply_markup=admin_menu())
    finally:
        await state.finish()

@dp.message_handler(state=RestoreFlow.stats, content_types=types.ContentType.DOCUMENT)
async def restore_stats_file(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.finish()
        return

    doc = message.document
    if not doc or (doc.file_name or "").lower() != "statistics.json":
        await message.answer("❗ Faqat <b>statistics.json</b> yuboring.", reply_markup=admin_menu())
        return

    try:
        f = await bot.get_file(doc.file_id)
        _ensure_parent_dir(STATS_FILE)
        await bot.download_file(f.file_path, STATS_FILE)
        _ = load_stats()
        await message.answer(f"✅ Tiklandi!\n📌 Saqlandi: <code>{STATS_FILE}</code>", reply_markup=admin_menu())
    except Exception:
        await message.answer("❌ Restore bo‘lmadi. Fayl buzilgan yoki ruxsat muammosi bo‘lishi mumkin.", reply_markup=admin_menu())
    finally:
        await state.finish()

@dp.message_handler(state=RestoreFlow.movies)
async def restore_movies_wait(message: types.Message):
    await message.answer("📎 Iltimos, <b>movies.json</b> faylni Document qilib yuboring.", reply_markup=admin_menu())

@dp.message_handler(state=RestoreFlow.stats)
async def restore_stats_wait(message: types.Message):
    await message.answer("📎 Iltimos, <b>statistics.json</b> faylni Document qilib yuboring.", reply_markup=admin_menu())

# ================== O‘CHIRISH ==================
@dp.message_handler(lambda m: m.text == "🗑 O‘chirish")
async def del_btn(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer(
            "❌ <b>Brat siz admin emassiz!</b>\n"
            "🎬 Faqat <b>Qidiruv</b> tugmasidan foydalanishingiz mumkin.",
            reply_markup=user_menu()
        )
        return
    await state.finish()
    await message.answer("🗑 Koddi ayting tog'o", reply_markup=admin_menu())
    await DeleteFlow.code.set()

@dp.message_handler(state=DeleteFlow.code)
async def delete_item(message: types.Message, state: FSMContext):
    code = (message.text or "").strip()
    if not code.isdigit():
        await message.answer("🗑 Koddi ayting tog'o", reply_markup=admin_menu())
        return

    db = load_db()
    item = db.get(code)
    if not item:
        await message.answer("❌ Bunaqa kino o'zi yo'q tog'o", reply_markup=admin_menu())
        await state.finish()
        return

    msg_id = item.get("channel_msg_id")
    if msg_id:
        try:
            await bot.delete_message(CHANNEL2_ID, msg_id)
        except Exception:
            pass

    del db[code]
    save_db(db)

    await message.answer(f"🗑 O'chirib tashadim tog'o\n🆔 Kod: {code}", reply_markup=admin_menu())
    await state.finish()

# ================== TAHRIRLASH ==================
def edit_type_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🎬 Yakka film", callback_data="edit_type:movie"),
        types.InlineKeyboardButton("📺 Serial", callback_data="edit_type:series"),
    )
    return kb

def edit_movie_kb(code: str):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("♻️ Kanal1 postni yuboring", callback_data=f"edit_movie_post:{code}"),
        types.InlineKeyboardButton("🎥 Kanal1 video yuboring", callback_data=f"edit_movie_video:{code}"),
        types.InlineKeyboardButton("🎞 Treyler qo‘shish", callback_data=f"add_trailer:{code}"),
        types.InlineKeyboardButton("🗑 Treylerni o‘chirish", callback_data=f"del_trailer:{code}"),
        types.InlineKeyboardButton("🗑 O‘chirish", callback_data=f"edit_delete:{code}")
    )
    return kb

def edit_series_kb(code: str):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("♻️ Kanal1 postni yuboring", callback_data=f"edit_series_post:{code}"),
        types.InlineKeyboardButton("➕ Yangi qism (video yuboring)", callback_data=f"series_add:{code}"),
        types.InlineKeyboardButton("🔁 Qismni almashtirish (video yuboring)", callback_data=f"series_replace:{code}"),
        types.InlineKeyboardButton("🗑 Qismni o‘chirish", callback_data=f"series_del:{code}"),
        types.InlineKeyboardButton("🗑 Serialni o‘chirish", callback_data=f"edit_delete:{code}")
    )
    return kb

@dp.message_handler(lambda m: m.text == "✏️ Tahrirlash")
async def edit_start(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer(
            "❌ <b>Brat siz admin emassiz!</b>\n"
            "🎬 Faqat <b>Qidiruv</b> tugmasidan foydalanishingiz mumkin.",
            reply_markup=user_menu()
        )
        return
    await state.finish()
    await message.answer("Nimani tahrirlaymiz?", reply_markup=edit_type_kb())
    await EditFlow.choose_type.set()

@dp.callback_query_handler(lambda c: c.data.startswith("edit_type:"), state=EditFlow.choose_type)
async def edit_choose_type(call: types.CallbackQuery, state: FSMContext):
    typ = call.data.split(":", 1)[1]
    await state.update_data(edit_type=typ)
    await call.message.edit_text("🆔 Koddi ayting tog'o")
    await EditFlow.choose_code.set()
    await call.answer()

@dp.message_handler(state=EditFlow.choose_code)
async def edit_choose_code(message: types.Message, state: FSMContext):
    code = (message.text or "").strip()
    if not code.isdigit():
        await message.answer("🆔 Koddi ayting tog'o", reply_markup=admin_menu())
        return

    db = load_db()
    data = await state.get_data()
    typ = data.get("edit_type")
    item = db.get(code)

    if not item or item.get("type") != typ:
        await message.answer("❌ Bunaqa kino o'zi yo'q tog'o", reply_markup=admin_menu())
        await state.finish()
        return

    await state.update_data(code=code)
    if typ == "movie":
        await message.answer("🎬 Tahrirlash:", reply_markup=edit_movie_kb(code))
    else:
        await message.answer("📺 Tahrirlash:", reply_markup=edit_series_kb(code))
    await EditFlow.choose_action.set()

@dp.callback_query_handler(lambda c: c.data.startswith("edit_movie_post:"), state=EditFlow.choose_action)
async def edit_movie_post(call: types.CallbackQuery, state: FSMContext):
    code = call.data.split(":", 1)[1]
    await state.update_data(pending=("movie_post", code))
    await call.message.answer("♻️ Kanal1 (baza)dagi <b>yangilangan postni</b> forward qiling.", reply_markup=admin_menu())
    await EditFlow.await_forward.set()
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("edit_movie_video:"), state=EditFlow.choose_action)
async def edit_movie_video(call: types.CallbackQuery, state: FSMContext):
    code = call.data.split(":", 1)[1]
    await state.update_data(pending=("movie_video", code))
    await call.message.answer("🎥 Kanal1 (baza)dagi <b>yangilangan videoni</b> forward qiling.", reply_markup=admin_menu())
    await EditFlow.await_forward.set()
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("edit_series_post:"), state=EditFlow.choose_action)
async def edit_series_post(call: types.CallbackQuery, state: FSMContext):
    code = call.data.split(":", 1)[1]
    await state.update_data(pending=("series_post", code))
    await call.message.answer("♻️ Kanal1 (baza)dagi <b>yangilangan poster postni</b> forward qiling.", reply_markup=admin_menu())
    await EditFlow.await_forward.set()
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("series_add:"), state=EditFlow.choose_action)
async def edit_series_add(call: types.CallbackQuery, state: FSMContext):
    code = call.data.split(":", 1)[1]
    await state.update_data(pending=("series_add", code))
    await call.message.answer("➕ Kanal1 dan videoni forward qiling.\nMasalan: <b>1 Yura davri 3</b>", reply_markup=admin_menu())
    await EditFlow.await_forward.set()
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("series_replace:"), state=EditFlow.choose_action)
async def edit_series_replace(call: types.CallbackQuery, state: FSMContext):
    code = call.data.split(":", 1)[1]
    await state.update_data(pending=("series_replace", code))
    await call.message.answer("🔁 Kanal1 dan videoni forward qiling.\nMasalan: <b>1 Yura davri 3</b>", reply_markup=admin_menu())
    await EditFlow.await_forward.set()
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("series_del:"), state=EditFlow.choose_action)
async def edit_series_del(call: types.CallbackQuery, state: FSMContext):
    code = call.data.split(":", 1)[1]
    await state.update_data(pending=("series_del", code))
    await call.message.answer("🗑 Qaysi qisimni o‘chiramiz? (raqam yuboring, masalan: 1)", reply_markup=admin_menu())
    await EditFlow.await_ep_delete.set()
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("edit_delete:"), state=EditFlow.choose_action)
async def edit_delete(call: types.CallbackQuery, state: FSMContext):
    code = call.data.split(":", 1)[1]
    db = load_db()
    item = db.get(code)
    if not item:
        await call.answer("❌ Topilmadi", show_alert=True)
        await state.finish()
        return

    msg_id = item.get("channel_msg_id")
    if msg_id:
        try:
            await bot.delete_message(CHANNEL2_ID, msg_id)
        except Exception:
            pass

    del db[code]
    save_db(db)
    await call.message.answer(f"🗑 O'chirib tashadim tog'o\n🆔 Kod: {code}", reply_markup=admin_menu())
    await state.finish()
    await call.answer()

@dp.message_handler(state=EditFlow.await_ep_delete)
async def edit_series_del_number(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("🗑 Qaysi qisimni o‘chiramiz? (raqam yuboring, masalan: 1)", reply_markup=admin_menu())
        return

    ep_num = int(text)
    data = await state.get_data()
    pending = data.get("pending")

    if not pending or pending[0] != "series_del":
        await message.answer("❎ Bekor qilindi tog'o", reply_markup=admin_menu())
        await state.finish()
        return

    code = pending[1]
    db = load_db()
    item = db.get(code)
    if not item or item.get("type") != "series":
        await message.answer("❌ Bunaqa kino o'zi yo'q tog'o", reply_markup=admin_menu())
        await state.finish()
        return

    eps = item.get("episodes", {}) or {}
    if str(ep_num) not in eps:
        await message.answer("❌ Bunaqa qisim yo'q tog'o", reply_markup=admin_menu())
        return

    del eps[str(ep_num)]
    item["episodes"] = eps
    db[code] = item
    save_db(db)

    await message.answer("♻️ Yangilandi", reply_markup=edited_done_kb(code))
    await state.finish()

@dp.message_handler(state=EditFlow.await_forward, content_types=types.ContentType.ANY)
async def edit_receive_forward(message: types.Message, state: FSMContext):
    if not await _is_forward_from_base(message):
        await message.answer("❗ Iltimos, <b>Kanal1 (baza)</b>dan forward qiling.", reply_markup=admin_menu())
        return

    data = await state.get_data()
    pending = data.get("pending")
    if not pending:
        await message.answer("❎ Bekor qilindi tog'o", reply_markup=admin_menu())
        await state.finish()
        return

    action, code = pending
    db = load_db()
    item = db.get(code)

    if not item:
        await message.answer("❌ Bunaqa kino o'zi yo'q tog'o", reply_markup=admin_menu())
        await state.finish()
        return

    # -------- movie_post --------
    if action == "movie_post":
        if message.content_type != types.ContentType.PHOTO:
            await message.answer("❗ Rasm (photo) forward qiling.", reply_markup=admin_menu())
            return

        new_photo = message.photo[-1].file_id
        new_caption = message.caption or ""

        ch_msg_id = item.get("channel_msg_id")
        if ch_msg_id:
            old_with_code = f"{(item.get('post_caption') or '').strip()}\n\n🆔 Kod: {code}"
            final_caption = _ensure_code_line_kept(new_caption, old_with_code, code)
            final_caption = _apply_edit_banner(final_caption, MOVIE_BANNER)
            try:
                media = types.InputMediaPhoto(media=new_photo, caption=final_caption, parse_mode="HTML")
                await bot.edit_message_media(CHANNEL2_ID, ch_msg_id, media=media, reply_markup=channel_movie_kb(code))
            except Exception:
                try:
                    await bot.edit_message_caption(CHANNEL2_ID, ch_msg_id, caption=final_caption, reply_markup=channel_movie_kb(code))
                except Exception:
                    pass

        item["post_file_id"] = new_photo
        item["post_caption"] = new_caption
        db[code] = item
        save_db(db)

        await message.answer("♻️ Yangilandi", reply_markup=edited_done_kb(code))
        await state.finish()
        return

    # -------- movie_video --------
    if action == "movie_video":
        if message.content_type != types.ContentType.VIDEO:
            await message.answer("❗ Video forward qiling.", reply_markup=admin_menu())
            return

        if _duplicate_video_exists(db, message.video.file_unique_id):
            await message.answer("❗ Bu kino borku tog'o", reply_markup=admin_menu())
            return

        item["video_file_id"] = message.video.file_id
        item["video_unique_id"] = message.video.file_unique_id
        db[code] = item
        save_db(db)

        await message.answer("♻️ Yangilandi", reply_markup=edited_done_kb(code))
        await state.finish()
        return

    # -------- series_post --------
    if action == "series_post":
        if message.content_type != types.ContentType.PHOTO:
            await message.answer("❗ Rasm (photo) forward qiling.", reply_markup=admin_menu())
            return

        new_photo = message.photo[-1].file_id
        new_caption = message.caption or ""

        ch_msg_id = item.get("channel_msg_id")
        if ch_msg_id:
            old_with_code = f"{(item.get('poster_caption') or '').strip()}\n\n🆔 Kod: {code}"
            final_caption = _ensure_code_line_kept(new_caption, old_with_code, code)
            final_caption = _apply_edit_banner(final_caption, SERIES_BANNER)
            try:
                media = types.InputMediaPhoto(media=new_photo, caption=final_caption, parse_mode="HTML")
                await bot.edit_message_media(CHANNEL2_ID, ch_msg_id, media=media, reply_markup=channel_series_kb(code))
            except Exception:
                try:
                    await bot.edit_message_caption(CHANNEL2_ID, ch_msg_id, caption=final_caption, reply_markup=channel_series_kb(code))
                except Exception:
                    pass

        item["poster_file_id"] = new_photo
        item["poster_caption"] = new_caption
        db[code] = item
        save_db(db)

        await message.answer("♻️ Yangilandi", reply_markup=edited_done_kb(code))
        await state.finish()
        return

    # -------- series add/replace --------
    if action in ("series_add", "series_replace"):
        if message.content_type != types.ContentType.VIDEO:
            await message.answer("❗ Video forward qiling.", reply_markup=admin_menu())
            return

        ep_num, ep_title = _parse_episode_caption(message.caption or "")
        if ep_num is None:
            await message.answer("❗ Video captionida qism raqimi yo‘q.\nMasalan: <b>1 Yura davri 3</b>", reply_markup=admin_menu())
            return

        if _duplicate_video_exists(db, message.video.file_unique_id):
            await message.answer("❗ Bu kino borku tog'o", reply_markup=admin_menu())
            return

        eps = item.get("episodes", {}) or {}
        exists = str(ep_num) in eps

        if action == "series_add" and exists:
            await message.answer("❗ Bu qisim bor tog'o. Almashtirish tanlang.", reply_markup=admin_menu())
            return
        if action == "series_replace" and not exists:
            await message.answer("❗ Bu qisim yo'q tog'o. Yangi qisim qo‘shish tanlang.", reply_markup=admin_menu())
            return

        eps[str(ep_num)] = {
            "video_file_id": message.video.file_id,
            "video_unique_id": message.video.file_unique_id,
            "title": (ep_title or "").strip()
        }
        item["episodes"] = eps
        db[code] = item
        save_db(db)

        # Kanal postiga ham banner qo'yib qo'yamiz (agar kanalda bo'lsa)
        ch_msg_id = item.get("channel_msg_id")
        if ch_msg_id:
            try:
                old_with_code = f"{(item.get('poster_caption') or '').strip()}\n\n🆔 Kod: {code}"
                final_caption = _ensure_code_line_kept(item.get("poster_caption") or "", old_with_code, code)
                final_caption = _apply_edit_banner(final_caption, SERIES_BANNER)
                await bot.edit_message_caption(CHANNEL2_ID, ch_msg_id, caption=final_caption, reply_markup=channel_series_kb(code))
            except Exception:
                pass

        await message.answer("♻️ Yangilandi", reply_markup=edited_done_kb(code))
        await state.finish()
        return

    await message.answer("❎ Bekor qilindi tog'o", reply_markup=admin_menu())
    await state.finish()

@dp.callback_query_handler(lambda c: c.data.startswith("edit_again:"))
async def edit_again(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("❌ Brat siz admin emassiz 😄", show_alert=True)
        return

    code = call.data.split(":", 1)[1]
    db = load_db()
    item = db.get(code)
    if not item:
        await call.answer("❌ Topilmadi", show_alert=True)
        return

    if item.get("type") == "movie":
        await call.message.answer("🎬 Tahrirlash:", reply_markup=edit_movie_kb(code))
    else:
        await call.message.answer("📺 Tahrirlash:", reply_markup=edit_series_kb(code))
    await call.answer()

# ================== REPUBLISH ==================
@dp.callback_query_handler(lambda c: c.data.startswith("republish:"))
async def republish(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("❌ Brat, bu joy adminniki 😄", show_alert=True)
        return

    code = call.data.split(":", 1)[1]
    db = load_db()
    item = db.get(code)
    if not item:
        await call.answer("❌ Topilmadi", show_alert=True)
        return

    old_msg_id = item.get("channel_msg_id")
    if old_msg_id:
        try:
            await bot.delete_message(CHANNEL2_ID, old_msg_id)
        except Exception:
            pass
        item["channel_msg_id"] = None
        db[code] = item
        save_db(db)

    ok, msg = await publish_to_channel(code)
    try:
        await call.message.edit_text("♻️ Yangilandi")
    except Exception:
        pass
    await call.answer()

# ================== OBUNA TEKSHIR ==================
@dp.callback_query_handler(lambda c: c.data == "check_sub")
async def recheck(call: types.CallbackQuery):
    if await check_subscription(call.from_user.id):
        await call.message.edit_text("✅ Obuna tasdiqlandi. Kod yuboring.")
    else:
        await call.answer("❌ Hali obuna bo'lmadingizku 😕", show_alert=True)

# ================== KANALGA YUBORILMAGANLAR ==================
@dp.message_handler(lambda m: m.text == "📣 Kanalga yuborish")
async def publish_later_btn(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ <b>Brat siz admin emassiz!</b>\n🎬 Faqat <b>Qidiruv</b> tugmasidan foydalanishingiz mumkin.", reply_markup=user_menu())
        return
    await state.finish()
    await message.answer("🆔 Kodni yuboring tog'o (kanalga chiqmagan bo'lsa jo'natamiz)", reply_markup=admin_menu())
    await PublishLater.code.set()

@dp.message_handler(state=PublishLater.code)
async def publish_later_code(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.finish()
        return

    code = (message.text or "").strip()
    if not code.isdigit():
        await message.answer("🆔 Kodni yuboring tog'o", reply_markup=admin_menu())
        return

    db = load_db()
    item = db.get(code)
    if not item:
        await message.answer("❌ Bunaqa kino o'zi yo'q tog'o", reply_markup=admin_menu())
        await state.finish()
        return

    if item.get("channel_msg_id"):
        await message.answer("⚠️ Bu kino kanalda bor tog'o. Dublikat yubormaymiz.", reply_markup=admin_menu())
        await state.finish()
        return

    kb = types.InlineKeyboardMarkup()
    if item.get("type") == "movie":
        kb.add(types.InlineKeyboardButton("✅ Kanalga jo'nataymi", callback_data=f"publish_movie:{code}"),
               types.InlineKeyboardButton("❌ Yo jo'natmayinmi?", callback_data="cancel_send"))
    else:
        kb.add(types.InlineKeyboardButton("✅ Kanalga jo'nataymi", callback_data=f"publish_series:{code}"),
               types.InlineKeyboardButton("❌ Yo jo'natmayinmi?", callback_data="cancel_send"))

    await message.answer("📣 Kanalga yuboraymi tog'o?", reply_markup=kb)
    await state.finish()

# ================== AVTOPOST ==================
@dp.message_handler(lambda m: m.text == "⏰ Avtopost")
async def ap_open(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer(
            "❌ <b>Brat siz admin emassiz!</b>\n🎬 Faqat <b>Qidiruv</b> tugmasidan foydalanishingiz mumkin.",
            reply_markup=user_menu()
        )
        return
    await state.finish()
    await message.answer(
        "⏰ Avtopost bo‘limi tog'o\n\nBu yerda kinolarni vaqtga qo‘yib,\nkanalga avtomatik chiqarasiz.\n\n👇 Nimani qilamiz?",
        reply_markup=autopost_menu_kb()
    )
    await AutoPostFlow.menu.set()

@dp.message_handler(state=AutoPostFlow.menu)
async def ap_menu_router(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.finish()
        return

    txt = (message.text or "").strip()

    if txt == "➕ Rejalashtirish":
        await message.answer("📅 Qaysi vaqtga qo‘yamiz tog'o?\n\nFormat:\n2026-03-06 21:30", reply_markup=autopost_menu_kb())
        await AutoPostFlow.add_time.set()
        return

    if txt == "📋 Rejalashtirilganlar":
        data = load_autopost()
        jobs = data.get("jobs", [])
        # show only pending (future or due not done yet)
        pending = [j for j in jobs if j.get("status") in (None, "pending")]
        if not pending:
            await message.answer("📭 Hozircha rejalashtirilgan kino yo‘q tog'o", reply_markup=autopost_menu_kb())
            return

        # sort by time
        pending_sorted = sorted(pending, key=lambda j: j.get("run_at", ""))
        lines = ["📋 Rejalashtirilgan kinolar tog'o\n"]
        for j in pending_sorted[:40]:
            lines.append(f"{j.get('id')} — 🎬 {j.get('code')} — ⏰ {j.get('run_at')}")
        await message.answer("\n".join(lines), reply_markup=autopost_menu_kb())
        return

    if txt == "✏️ Tahrirlash":
        await message.answer("✏️ Qaysi avtopostni tahrirlaymiz tog'o?\n\nID ni yuboring\nMasalan: AP-1047", reply_markup=autopost_menu_kb())
        await AutoPostFlow.edit_id.set()
        return

    if txt == "🗑 O‘chirish":
        await message.answer("🗑 Qaysi avtopostni o‘chiramiz tog'o?\n\nID ni yuboring\nMasalan: AP-1047", reply_markup=autopost_menu_kb())
        await AutoPostFlow.del_id.set()
        return

    await message.answer("❌ Noto'g'ri buyruq tog'o.\n👇 Menudan foydalaning.", reply_markup=autopost_menu_kb())

@dp.message_handler(state=AutoPostFlow.add_time)
async def ap_add_time(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.finish()
        return

    s = (message.text or "").strip()
    dt = _parse_dt_local(s)
    if not dt:
        await message.answer("❌ Vaqt noto‘g‘ri tog'o\n\nMana bunday yozing:\n2026-03-06 21:30", reply_markup=autopost_menu_kb())
        return

    await state.update_data(ap_time=s)
    await message.answer("🆔 Endi kino kodini yuboring tog'o", reply_markup=autopost_menu_kb())
    await AutoPostFlow.add_code.set()

@dp.message_handler(state=AutoPostFlow.add_code)
async def ap_add_code(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.finish()
        return

    code = (message.text or "").strip()
    if not code.isdigit():
        await message.answer("🆔 Kodni to'g'ri yuboring tog'o (4 raqam)", reply_markup=autopost_menu_kb())
        return

    db = load_db()
    item = db.get(code)
    if not item:
        await message.answer("❌ Bunaqa kino o‘zi yo‘q tog'o", reply_markup=autopost_menu_kb())
        await state.finish()
        return

    if item.get("channel_msg_id"):
        await message.answer("⚠️ Bu kino kanalda bor tog'o\nDublikat chiqarmaymiz.", reply_markup=autopost_menu_kb())
        await state.finish()
        return

    st = await state.get_data()
    run_at = st.get("ap_time")

    data = load_autopost()
    jobs = data.get("jobs", [])
    apid = _ap_new_id(jobs)

    jobs.append({
        "id": apid,
        "code": code,
        "run_at": run_at,
        "created_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "status": "pending",
    })
    data["jobs"] = jobs
    save_autopost(data)

    await message.answer(
        f"✅ Avtopost saqlandi tog'o\n\n🆔 ID: {apid}\n🎬 Kod: {code}\n⏰ Vaqt: {run_at}",
        reply_markup=autopost_menu_kb()
    )
    await state.finish()

@dp.message_handler(state=AutoPostFlow.edit_id)
async def ap_edit_id(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.finish()
        return

    apid = (message.text or "").strip().upper()
    data = load_autopost()
    jobs = data.get("jobs", [])
    job = next((j for j in jobs if str(j.get("id", "")).upper() == apid and j.get("status") in (None, "pending")), None)
    if not job:
        await message.answer("❌ Bunaqa avtopost yo‘q tog'o", reply_markup=autopost_menu_kb())
        await state.finish()
        return

    await state.update_data(apid=apid)
    await message.answer("✏️ Nimani o‘zgartiramiz tog'o?", reply_markup=autopost_edit_kb())
    await AutoPostFlow.edit_choose.set()

@dp.callback_query_handler(lambda c: c.data in ("ap_edit_time", "ap_edit_code", "ap_edit_cancel"), state=AutoPostFlow.edit_choose)
async def ap_edit_choose(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("❌ Admin emas ekansiz 😄", show_alert=True)
        await state.finish()
        return

    if call.data == "ap_edit_cancel":
        await call.message.answer("❎ Bekor qilindi tog'o", reply_markup=autopost_menu_kb())
        await state.finish()
        await call.answer()
        return

    if call.data == "ap_edit_time":
        await call.message.answer("🕒 Yangi vaqtni yuboring tog'o\n\nFormat:\n2026-03-06 22:30", reply_markup=autopost_menu_kb())
        await AutoPostFlow.edit_time.set()
        await call.answer()
        return

    if call.data == "ap_edit_code":
        await call.message.answer("🎬 Yangi kino kodini yuboring tog'o", reply_markup=autopost_menu_kb())
        await AutoPostFlow.edit_code.set()
        await call.answer()
        return

@dp.message_handler(state=AutoPostFlow.edit_time)
async def ap_edit_time(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.finish()
        return

    s = (message.text or "").strip()
    dt = _parse_dt_local(s)
    if not dt:
        await message.answer("❌ Vaqt noto‘g‘ri tog'o\n\nMana bunday yozing:\n2026-03-06 21:30", reply_markup=autopost_menu_kb())
        return

    st = await state.get_data()
    apid = st.get("apid")

    data = load_autopost()
    jobs = data.get("jobs", [])
    for j in jobs:
        if str(j.get("id", "")).upper() == str(apid).upper() and j.get("status") in (None, "pending"):
            j["run_at"] = s
            save_autopost(data)
            await message.answer("♻️ Vaqt yangilandi tog'o", reply_markup=autopost_menu_kb())
            await state.finish()
            return

    await message.answer("❌ Bunaqa avtopost yo‘q tog'o", reply_markup=autopost_menu_kb())
    await state.finish()

@dp.message_handler(state=AutoPostFlow.edit_code)
async def ap_edit_code(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.finish()
        return

    code = (message.text or "").strip()
    if not code.isdigit():
        await message.answer("🆔 Kodni to'g'ri yuboring tog'o (4 raqam)", reply_markup=autopost_menu_kb())
        return

    db = load_db()
    item = db.get(code)
    if not item:
        await message.answer("❌ Bunaqa kino o‘zi yo‘q tog'o", reply_markup=autopost_menu_kb())
        await state.finish()
        return

    if item.get("channel_msg_id"):
        await message.answer("⚠️ Bu kino kanalda bor tog'o\nDublikat chiqarmaymiz.", reply_markup=autopost_menu_kb())
        await state.finish()
        return

    st = await state.get_data()
    apid = st.get("apid")

    data = load_autopost()
    jobs = data.get("jobs", [])
    for j in jobs:
        if str(j.get("id", "")).upper() == str(apid).upper() and j.get("status") in (None, "pending"):
            j["code"] = code
            save_autopost(data)
            await message.answer("♻️ Kino almashtirildi tog'o", reply_markup=autopost_menu_kb())
            await state.finish()
            return

    await message.answer("❌ Bunaqa avtopost yo‘q tog'o", reply_markup=autopost_menu_kb())
    await state.finish()

@dp.message_handler(state=AutoPostFlow.del_id)
async def ap_delete(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.finish()
        return

    apid = (message.text or "").strip().upper()
    data = load_autopost()
    jobs = data.get("jobs", [])
    for j in jobs:
        if str(j.get("id", "")).upper() == apid and j.get("status") in (None, "pending"):
            j["status"] = "cancelled"
            j["done_at"] = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
            j["result"] = "cancelled by admin"
            save_autopost(data)
            await message.answer(f"🗑 O‘chirib tashadim tog'o\n\n🆔 {apid}", reply_markup=autopost_menu_kb())
            await state.finish()
            return

    await message.answer("❌ Bunaqa avtopost yo‘q tog'o", reply_markup=autopost_menu_kb())
    await state.finish()

# ================== FALLBACK (hech qachon jim emas) ==================
@dp.message_handler(content_types=types.ContentType.ANY, state="*")
async def fallback_all(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer(
            "❌ <b>Brat siz admin emassiz!</b>\n"
            "🎬 Faqat <b>Qidiruv</b> tugmasidan foydalanishingiz mumkin.",
            reply_markup=user_menu()
        )
    else:
        await message.answer("❌ Noto'g'ri buyruq tog'o.\n👇 Menudan foydalaning.", reply_markup=admin_menu())

# ================== STARTUP ==================
async def on_startup(dp):
    await bot.delete_webhook(drop_pending_updates=True)
    # start autopost watchdog
    asyncio.get_event_loop().create_task(autopost_loop())

if __name__ == "__main__":
    executor.start_polling(
        dp,
        skip_updates=True,
        on_startup=on_startup
    )
