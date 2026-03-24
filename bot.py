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

# Kanal IDlar
CHANNEL1_ID = int(os.getenv("BASE_CHANNEL_ID", "0"))        # 🔒 baza
CHANNEL2_ID = int(os.getenv("BUSINESS_CHANNEL_ID", "0"))    # 📣 asosiy
TRAILER_CHANNEL_ID = int(os.getenv("TRAILER_CHANNEL_ID", "0"))  # 🎬 treyler

# Majburiy obuna
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

# ================== BOT SETTINGS ==================

BOT_USERNAME = (os.getenv("BOT_USERNAME") or "").lstrip("@").strip()

MOVIES_FILE = os.getenv("MOVIES_FILE", "movies.json")
STATS_FILE = os.getenv("STATS_FILE", "statistics.json")
AUTOPOST_FILE = os.getenv("AUTOPOST_FILE", "autopost.json")

# Timezone
TZ_NAME = os.getenv("TZ", "Asia/Tashkent")
TZ = pytz.timezone(TZ_NAME)

# Adminlar
ADMINS = {ADMIN_ID}

# ================== BOT ==================

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN topilmadi! .env faylni tekshiring")

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot, storage=MemoryStorage())

# ================== BANNER ==================

MOVIE_BANNER = "♻️ Yangilandi"
SERIES_BANNER = "♻️ Yangi qismi qo'shildi yoki sifatli formatga almashtirildi"

# Eski bannerlarni tozalash (takror qo‘shilib ketmasligi uchun)
BANNER_RE = re.compile(r"^♻️ .*?\n\n", re.IGNORECASE)

def _apply_edit_banner(caption: str, banner_text: str) -> str:
    try:
        cap = (caption or "").strip()

        # Eski bannerlarni olib tashlaymiz
        cap = BANNER_RE.sub("", cap).strip()

        # Agar caption bo‘sh bo‘lsa faqat banner qaytariladi
        if not cap:
            return banner_text

        return f"{banner_text}\n\n{cap}"

    except Exception:
        # Xatoda ham bot yiqilmasin
        return caption or ""

# ================== PATH HELPERS ==================

def _ensure_parent_dir(path: str) -> None:
    try:
        if not path:
            return

        parent = os.path.dirname(path)

        # Agar parent mavjud bo‘lsa va bo‘sh bo‘lmasa
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)

    except Exception:
        # Xatoda bot yiqilmasin
        pass

# ================== JSON (atomic) ==================

def _atomic_write_json(path: str, data: Any) -> None:
    try:
        _ensure_parent_dir(path)

        tmp = f"{path}.tmp"

        # Vaqtinchalik faylga yozamiz
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        # Asosiy fayl bilan almashtiramiz (atomic)
        os.replace(tmp, path)

    except Exception as e:
        # Xatoda bot yiqilmasin
        print(f"[JSON WRITE ERROR] {e}")

# ================== DB ==================

def load_db() -> Dict[str, Any]:
    try:
        # Fayl yo‘q bo‘lsa avtomatik yaratamiz
        if not os.path.exists(MOVIES_FILE):
            _atomic_write_json(MOVIES_FILE, {})
            return {}

        with open(MOVIES_FILE, "r", encoding="utf-8") as f:
            db = json.load(f)

        # Agar JSON buzilgan bo‘lsa
        if not isinstance(db, dict):
            return {}

    except Exception as e:
        print(f"[DB LOAD ERROR] {e}")
        return {}

    # ================== BACKWARD COMPATIBILITY ==================
    fixed: Dict[str, Any] = {}

    for code, item in (db or {}).items():
        try:
            if not isinstance(item, dict):
                continue

            # Eski format → yangi formatga o‘tkazish
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

        except Exception:
            continue

    return fixed


def save_db(data: Dict[str, Any]) -> None:
    try:
        if not isinstance(data, dict):
            return
        _atomic_write_json(MOVIES_FILE, data)
    except Exception as e:
        print(f"[DB SAVE ERROR] {e}")

# ================== STATISTIKA ==================

def load_stats() -> Dict[str, Any]:
    try:
        if not os.path.exists(STATS_FILE):
            data = {
                "movies": 0,
                "series": 0,
                "trailers": 0
            }
            _atomic_write_json(STATS_FILE, data)
            return data

        with open(STATS_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)

        if not isinstance(st, dict):
            return {"movies": 0, "series": 0, "trailers": 0}

        # Default qiymatlar
        st.setdefault("movies", 0)
        st.setdefault("series", 0)
        st.setdefault("trailers", 0)

        return st

    except Exception as e:
        print(f"[STATS LOAD ERROR] {e}")
        return {"movies": 0, "series": 0, "trailers": 0}


def save_stats(data: Dict[str, Any]) -> None:
    try:
        if not isinstance(data, dict):
            return
        _atomic_write_json(STATS_FILE, data)
    except Exception as e:
        print(f"[STATS SAVE ERROR] {e}")


def update_stats(db: Dict[str, Any]) -> None:
    try:
        movies = 0
        series = 0
        trailers = 0

        for item in db.values():
            if not isinstance(item, dict):
                continue

            if item.get("type") == "movie":
                movies += 1
            elif item.get("type") == "series":
                series += 1

            # Treyler borligini tekshirish
            if item.get("trailer") and item["trailer"].get("file_id"):
                trailers += 1

        stats = {
            "movies": movies,
            "series": series,
            "trailers": trailers
        }

        save_stats(stats)

    except Exception as e:
        print(f"[STATS UPDATE ERROR] {e}")

# ================== AVTOKOD ==================

def generate_unique_code(db: Dict[str, Any]) -> str:
    try:
        # Maksimal urinishlar (cheksiz loop oldini olish)
        for _ in range(10000):
            code = str(random.randint(1000, 9999))
            if code not in db:
                return code

        # Agar tasodifan hammasi band bo‘lsa → fallback (uzunroq kod)
        while True:
            code = str(random.randint(10000, 99999))
            if code not in db:
                return code

    except Exception:
        # Xatoda oddiy random qaytaradi
        return str(random.randint(1000, 9999))

# ================== OBUNA ==================

async def check_subscription(user_id: int) -> bool:
    if not FORCE_SUB_ENABLED:
        return True

    try:
        ok1 = True
        ok2 = True

        # 1-kanal tekshirish
        if FORCE_SUB_1_ID:
            try:
                member1 = await bot.get_chat_member(FORCE_SUB_1_ID, user_id)
                ok1 = member1.status in ("member", "administrator", "creator")
            except Exception:
                # Kanal ishlamasa → bot to‘xtamasin
                ok1 = True

        # 2-kanal tekshirish
        if FORCE_SUB_2_ID:
            try:
                member2 = await bot.get_chat_member(FORCE_SUB_2_ID, user_id)
                ok2 = member2.status in ("member", "administrator", "creator")
            except Exception:
                ok2 = True

        return ok1 and ok2

    except Exception:
        return True  # umumiy xatoda ham bot bloklamaydi


def subscribe_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)

    # 1-kanal tugma
    if FORCE_SUB_1_LINK:
        kb.add(types.InlineKeyboardButton("🔔 1-kanalga obuna bo‘lish", url=FORCE_SUB_1_LINK))

    # 2-kanal tugma
    if FORCE_SUB_2_LINK:
        kb.add(types.InlineKeyboardButton("🔔 2-kanalga obuna bo‘lish", url=FORCE_SUB_2_LINK))

    # Tekshirish tugmasi
    kb.add(types.InlineKeyboardButton("✅ Tekshirish", callback_data="check_sub"))

    return kb

# ================== MENULAR ==================

def user_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)

    # Doimiy panel
    kb.row("🎬 Qidiruv", "❌ Bekor qilish")

    return kb


def admin_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)

    kb.row("➕ Kino qo‘shish", "➕ Serial qo‘shish")
    kb.row("✏️ Tahrirlash", "🗑 O‘chirish")

    kb.row("🎬 Qidiruv", "📊 Statistika")

    kb.row("📦 Kino backup", "📈 Statistika backup")
    kb.row("♻️ Kino restore", "♻️ Statistika restore")

    kb.row("📣 Kanalga yuborish", "⏰ Avtopost")

    # Doimiy cancel
    kb.row("❌ Bekor qilish")

    return kb


def is_admin(uid: int) -> bool:
    try:
        return uid in ADMINS
    except Exception:
        return False


def protect_for(uid: int) -> bool:
    # oddiy user yopiq, admin ochiq
    try:
        return not is_admin(uid)
    except Exception:
        return True

# ================== FSM ==================

class AddMovie(StatesGroup):
    post = State()
    video = State()
    trailer = State()   # 🎬 treyler


class AddSeries(StatesGroup):
    poster = State()
    episodes = State()
    trailer = State()   # 📺 treyler ham qo‘shildi


class EditFlow(StatesGroup):
    choose_type = State()    # movie / series
    choose_code = State()
    choose_action = State()
    await_forward = State()
    await_ep_delete = State()


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

CODE_LINE_RE = re.compile(r"(🆔\s*Kod:\s*([0-9]{4,5}))", re.IGNORECASE)


def _ensure_code_line_kept(new_caption: str, old_caption_with_code: str, code: str) -> str:
    try:
        m = CODE_LINE_RE.search(old_caption_with_code or "")
        code_line = m.group(1) if m else f"🆔 Kod: {code}"

        cleaned = CODE_LINE_RE.sub("", (new_caption or "")).strip()

        return f"{cleaned}\n\n{code_line}".strip() if cleaned else code_line

    except Exception:
        return f"🆔 Kod: {code}"


def _duplicate_video_exists(db: Dict[str, Any], video_unique_id: str) -> bool:
    try:
        for it in db.values():
            if not isinstance(it, dict):
                continue

            if it.get("type") == "movie":
                if it.get("video_unique_id") == video_unique_id:
                    return True

            elif it.get("type") == "series":
                for epv in (it.get("episodes", {}) or {}).values():
                    if isinstance(epv, dict) and epv.get("video_unique_id") == video_unique_id:
                        return True

        return False

    except Exception:
        return False


async def _is_forward_from_base(message: types.Message) -> bool:
    try:
        return bool(
            message.forward_from_chat and
            int(message.forward_from_chat.id) == int(CHANNEL1_ID)
        )
    except Exception:
        return False


def _parse_episode_caption(caption: str) -> Tuple[Optional[int], str]:
    """
    QOIDALAR:
    - Birinchi uchragan raqam -> qism raqami
    - Qolgan matn -> nom
    """
    try:
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

    except Exception:
        return None, ""


def _episode_user_caption(ep: int, title: str) -> str:
    try:
        title = (title or "").strip()
        if title:
            return f"{ep}-qisim({title})"
        return f"{ep}-qisim"
    except Exception:
        return f"{ep}-qisim"


def _sorted_episode_numbers(item: Dict[str, Any]) -> List[int]:
    try:
        eps = item.get("episodes", {}) or {}
        nums: List[int] = []

        for k in eps.keys():
            if str(k).isdigit():
                nums.append(int(k))

        return sorted(nums)

    except Exception:
        return []

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

    # ================== AUTPOST STORAGE ==================

def load_autopost() -> Dict[str, Any]:
    try:
        if not os.path.exists(AUTOPOST_FILE):
            data = {"meta": {"daily_done_sent": {}}, "jobs": []}
            _atomic_write_json(AUTOPOST_FILE, data)
            return data

        with open(AUTOPOST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Eski format (list) → yangi format (dict)
        if isinstance(data, list):
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

    except Exception as e:
        print(f"[AUTOPOST LOAD ERROR] {e}")
        return {"meta": {"daily_done_sent": {}}, "jobs": []}


def save_autopost(data: Dict[str, Any]) -> None:
    try:
        if not isinstance(data, dict):
            return
        _atomic_write_json(AUTOPOST_FILE, data)
    except Exception as e:
        print(f"[AUTOPOST SAVE ERROR] {e}")


def _ap_new_id(jobs: List[Dict[str, Any]]) -> str:
    try:
        for _ in range(10000):
            x = random.randint(1000, 9999)
            apid = f"AP-{x}"
            if all(j.get("id") != apid for j in jobs):
                return apid

        # fallback
        while True:
            x = random.randint(10000, 99999)
            apid = f"AP-{x}"
            if all(j.get("id") != apid for j in jobs):
                return apid

    except Exception:
        return f"AP-{random.randint(1000,9999)}"


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

def _safe_caption_html(text: str) -> str:
    try:
        if not text:
            return ""

        # Minimal tozalash (linkni buzmaymiz)
        text = text.replace("\r", "").strip()

        return text
    except Exception:
        return text or ""


async def publish_to_channel(code: str) -> Tuple[bool, str]:
    try:
        db = load_db()
        item = db.get(code)

        if not item:
            return False, "❌ Bunaqa kino o'zi yo'q tog'o"

        if item.get("channel_msg_id"):
            return False, "⚠️ Bu kino kanalda bor tog'o. Dublikat chiqarmaymiz."

        # ================== TREYLER (3K kanal) ==================
        trailer = item.get("trailer") or {}
        if trailer.get("file_id") and CHANNEL1_ID:
            try:
                await bot.send_video(
                    CHANNEL1_ID,
                    trailer["file_id"],
                    caption=_safe_caption_html(trailer.get("caption", "")),
                    parse_mode="HTML"
                )
            except Exception as e:
                print(f"[TRAILER SEND ERROR] {e}")

        # ================== MOVIE ==================
        if item.get("type") == "movie":
            caption = f"{(item.get('post_caption') or '').strip()}\n\n🆔 Kod: {code}".strip()
            caption = _safe_caption_html(caption)

            msg = await bot.send_photo(
                CHANNEL2_ID,
                item["post_file_id"],
                caption=caption,
                parse_mode="HTML",
                reply_markup=channel_movie_kb(code),
            )

            item["channel_msg_id"] = msg.message_id
            db[code] = item
            save_db(db)

            return True, "🚀 Kanalga keeetti tog'o"

        # ================== SERIES ==================
        if item.get("type") == "series":
            caption = f"{(item.get('poster_caption') or '').strip()}\n\n🆔 Kod: {code}".strip()
            caption = _safe_caption_html(caption)

            msg = await bot.send_photo(
                CHANNEL2_ID,
                item["poster_file_id"],
                caption=caption,
                parse_mode="HTML",
                reply_markup=channel_series_kb(code),
            )

            item["channel_msg_id"] = msg.message_id
            db[code] = item
            save_db(db)

            return True, "🚀 Kanalga keeetti tog'o"

        return False, "❌ Topilmadi"

    except Exception as e:
        print(f"[PUBLISH ERROR] {e}")
        return False, "❌ Xatolik yuz berdi"

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

                    # 👉 FLOOD OLDINI OLISH
                    await asyncio.sleep(2)

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

            # ================== KUNLIK YAKUN ==================
            dates = set()

            for job in jobs:
                run_at = _parse_dt_local(job.get("run_at", ""))
                if run_at:
                    dates.add(run_at.strftime("%Y-%m-%d"))

            for d in sorted(dates):
                if str(daily_done_sent.get(d, "")).lower() == "true":
                    continue

                day_jobs = [
                    j for j in jobs
                    if (_parse_dt_local(j.get("run_at", "")) and
                        _parse_dt_local(j.get("run_at", "")).strftime("%Y-%m-%d") == d)
                ]

                if not day_jobs:
                    continue

                if all(j.get("status") in ("done", "skipped", "cancelled") for j in day_jobs):
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

            # ================== CLEANUP ==================
            if len(jobs) > 200:
                jobs_sorted = sorted(jobs, key=lambda j: j.get("created_at", ""))
                data["jobs"] = jobs_sorted[-200:]
                changed = True

            if changed:
                save_autopost(data)

        except Exception as e:
            print(f"[AUTOPOST LOOP ERROR] {e}")

        await asyncio.sleep(20)

# ================== BEKOR (har qanday holatda) ==================

@dp.message_handler(
    lambda m: (
        (m.text or "").strip() == "❌ Bekor qilish" or
        "bekor" in (m.text or "").lower()
    ),
    state="*"
)
async def cancel_anytime(message: types.Message, state: FSMContext):
    try:
        # State tozalash
        await state.finish()

        # Qo‘shimcha: vaqtinchalik user data tozalash (agar ishlatilsa)
        uid = message.from_user.id
        last_movie_request.pop(uid, None)
        last_watch_token.pop(uid, None)

        if is_admin(uid):
            await message.answer(
                "❎ Bekor qilindi tog'o",
                reply_markup=admin_menu()
            )
        else:
            await message.answer(
                "❎ Bekor qilindi",
                reply_markup=user_menu()
            )

    except Exception as e:
        print(f"[CANCEL ERROR] {e}")

# ================== START ==================

@dp.message_handler(commands=["start"])
async def start_cmd(message: types.Message, state: FSMContext):
    try:
        await state.finish()

        uid = message.from_user.id
        args = (message.get_args() or "").strip()

        # ================== OBUNA TEKSHIRISH ==================
        if protect_for(uid):
            is_sub = await check_subscription(uid)
            if not is_sub:
                await message.answer(
                    "❗ Botdan foydalanish uchun kanallarga obuna bo‘ling:",
                    reply_markup=subscribe_kb()
                )
                return

        # ================== DEEP LINK (SERIES) ==================
        if args.startswith("series_"):
            code = args.replace("series_", "").strip()
            if code.isdigit():
                await send_series_to_user(uid, code)
                return

        # ================== DEEP LINK (MOVIE) ==================
        if args.isdigit():
            message.text = args
            await search_movie(message)
            return

        # ================== DEFAULT ==================
        if is_admin(uid):
            await message.answer(
                "👑 <b>Admin panel</b>",
                reply_markup=admin_menu()
            )
        else:
            await message.answer(
                "🎬 Kino kodini yuboring",
                reply_markup=user_menu()
            )

    except Exception as e:
        print(f"[START ERROR] {e}")

@dp.message_handler(lambda m: (m.text or "").strip() == "🎬 Qidiruv")
async def search_btn(message: types.Message):
    try:
        uid = message.from_user.id

        # ================== OBUNA TEKSHIRISH ==================
        if protect_for(uid):
            is_sub = await check_subscription(uid)
            if not is_sub:
                await message.answer(
                    "❗ Kino qidirish uchun kanallarga obuna bo‘ling:",
                    reply_markup=subscribe_kb()
                )
                return

        kb = admin_menu() if is_admin(uid) else user_menu()

        await message.answer(
            "🔎 Kino kodini yuboring",
            reply_markup=kb
        )

    except Exception as e:
        print(f"[SEARCH BTN ERROR] {e}")

# ================== KINO QO‘SHISH (YAKKA) ==================

@dp.message_handler(lambda m: (m.text or "").strip() == "➕ Kino qo‘shish")
async def add_movie_btn(message: types.Message):
    try:
        if message.from_user.id not in ADMINS:
            await message.answer(
                "❌ <b>Brat siz admin emassiz!</b>\n"
                "🎬 Faqat <b>Qidiruv</b> tugmasidan foydalanishingiz mumkin.",
                reply_markup=user_menu()
            )
            return

        await message.answer("📨 Rasm-pasimlarini tashang", reply_markup=admin_menu())
        await AddMovie.post.set()

    except Exception as e:
        print(f"[ADD MOVIE BTN ERROR] {e}")


@dp.message_handler(content_types=types.ContentType.PHOTO, state=AddMovie.post)
async def add_post(message: types.Message, state: FSMContext):
    try:
        db = load_db()
        code = generate_unique_code(db)

        await state.update_data(
            code=code,
            post_file_id=message.photo[-1].file_id,
            post_caption=(message.caption or "")
        )

        await message.answer(
            f"🆔 <b>Kino kodi avtomatik berildi:</b> {code}\n\n🎥 Endi video tashang",
            reply_markup=admin_menu()
        )

        await AddMovie.video.set()

    except Exception as e:
        print(f"[ADD POST ERROR] {e}")


@dp.message_handler(content_types=types.ContentType.VIDEO, state=AddMovie.video)
async def add_video(message: types.Message, state: FSMContext):
    try:
        db = load_db()

        if _duplicate_video_exists(db, message.video.file_unique_id):
            await message.answer("❗ Bu kino borku tog'o", reply_markup=admin_menu())
            await state.finish()
            return

        data = await state.get_data()
        code = data["code"]

        # ================== SAQLASH (TREYLERSIZ HOZIRCHA) ==================
        await state.update_data(
            video_file_id=message.video.file_id,
            video_unique_id=message.video.file_unique_id
        )

        await message.answer(
            "🎥 Agar treyler bo‘lsa yuboring (video)\n\n⏭ Yoki o'tkazib yuborish uchun /skip yozing",
            reply_markup=admin_menu()
        )

        await AddMovie.trailer.set()

    except Exception as e:
        print(f"[ADD VIDEO ERROR] {e}")


# ================== TREYLER QABUL ==================

@dp.message_handler(content_types=types.ContentType.VIDEO, state=AddMovie.trailer)
async def add_trailer(message: types.Message, state: FSMContext):
    try:
        data = await state.get_data()
        db = load_db()
        code = data["code"]

        db[code] = {
            "type": "movie",
            "post_file_id": data["post_file_id"],
            "post_caption": data["post_caption"],
            "video_file_id": data["video_file_id"],
            "video_unique_id": data["video_unique_id"],
            "channel_msg_id": None,
            "trailer": {
                "file_id": message.video.file_id,
                "caption": message.caption or ""
            }
        }

        save_db(db)

        kb = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton("✅ Kanalga jo'nataymi", callback_data=f"publish_movie:{code}"),
            types.InlineKeyboardButton("❌ Yo jo'natmayinmi?", callback_data="cancel_send")
        )

        await message.answer(f"✅ Kino saqlandi\n🆔 Kod: {code}\n\nKanalga yuboraymi?", reply_markup=kb)
        await state.finish()

    except Exception as e:
        print(f"[ADD TRAILER ERROR] {e}")


# ================== TREYLER SKIP ==================

@dp.message_handler(lambda m: (m.text or "").lower() == "/skip", state=AddMovie.trailer)
async def skip_trailer(message: types.Message, state: FSMContext):
    try:
        data = await state.get_data()
        db = load_db()
        code = data["code"]

        db[code] = {
            "type": "movie",
            "post_file_id": data["post_file_id"],
            "post_caption": data["post_caption"],
            "video_file_id": data["video_file_id"],
            "video_unique_id": data["video_unique_id"],
            "channel_msg_id": None,
            "trailer": None
        }

        save_db(db)

        kb = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton("✅ Kanalga jo'nataymi", callback_data=f"publish_movie:{code}"),
            types.InlineKeyboardButton("❌ Yo jo'natmayinmi?", callback_data="cancel_send")
        )

        await message.answer(f"✅ Kino saqlandi\n🆔 Kod: {code}\n\nKanalga yuboraymi?", reply_markup=kb)
        await state.finish()

    except Exception as e:
        print(f"[SKIP TRAILER ERROR] {e}")

# ================== SERIAL QO‘SHISH ==================

@dp.message_handler(lambda m: (m.text or "").strip() == "➕ Serial qo‘shish")
async def add_series_btn(message: types.Message):
    try:
        if not is_admin(message.from_user.id):
            await message.answer(
                "❌ <b>Brat siz admin emassiz!</b>\n"
                "🎬 Faqat <b>Qidiruv</b> tugmasidan foydalanishingiz mumkin.",
                reply_markup=user_menu()
            )
            return

        await message.answer(
            "📨 Serial posteri (rasm + caption)ni yuboring",
            reply_markup=admin_menu()
        )
        await AddSeries.poster.set()

    except Exception as e:
        print(f"[ADD SERIES BTN ERROR] {e}")


@dp.message_handler(content_types=types.ContentType.PHOTO, state=AddSeries.poster)
async def add_series_poster(message: types.Message, state: FSMContext):
    try:
        db = load_db()
        code = generate_unique_code(db)

        await state.update_data(
            code=code,
            poster_file_id=message.photo[-1].file_id,
            poster_caption=(message.caption or ""),
            episodes={}
        )

        await message.answer(
            f"🆔 <b>Kino kodi avtomatik berildi:</b> {code}\n\n"
            "Endi Kanal1 (baza)dan videoni forward qiling.\n"
            "Caption misol: <b>1 Yura davri 3</b> yoki <b>7 | Forsaj</b>\n\n"
            "Tugatish uchun <b>Ha</b> deb yozing.",
            reply_markup=admin_menu()
        )

        await AddSeries.episodes.set()

    except Exception as e:
        print(f"[SERIES POSTER ERROR] {e}")


@dp.message_handler(lambda m: (m.text or "").strip().lower() == "ha", state=AddSeries.episodes)
async def add_series_finish(message: types.Message, state: FSMContext):
    try:
        data = await state.get_data()
        episodes = data.get("episodes", {})

        if not episodes:
            await message.answer("❗ Hech bo‘lmasa bitta qism qo‘shing.", reply_markup=admin_menu())
            return

        await message.answer(
            "🎥 Agar treyler bo‘lsa yuboring\n\n⏭ O‘tkazish uchun /skip yozing",
            reply_markup=admin_menu()
        )

        await AddSeries.trailer.set()

    except Exception as e:
        print(f"[SERIES FINISH ERROR] {e}")


# ================== EPISODE QO‘SHISH ==================

@dp.message_handler(content_types=types.ContentType.VIDEO, state=AddSeries.episodes)
async def add_series_episode(message: types.Message, state: FSMContext):
    try:
        if not await _is_forward_from_base(message):
            await message.answer("❗ Kanal1 (baza)dan forward qiling.", reply_markup=admin_menu())
            return

        ep_num, ep_title = _parse_episode_caption(message.caption or "")
        if ep_num is None:
            await message.answer("❗ Captionda qism raqami yo‘q.", reply_markup=admin_menu())
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

    except Exception as e:
        print(f"[ADD EP ERROR] {e}")


@dp.message_handler(state=AddSeries.episodes, content_types=types.ContentType.TEXT)
async def add_series_text_in_episodes(message: types.Message, state: FSMContext):
    await message.answer(
        "🎥 Kanal1 (baza)dan videoni forward qiling.\n"
        "Tugatish uchun <b>Ha</b> deb yozing.",
        reply_markup=admin_menu()
    )


# ================== TREYLER ==================

@dp.message_handler(content_types=types.ContentType.VIDEO, state=AddSeries.trailer)
async def add_series_trailer(message: types.Message, state: FSMContext):
    try:
        data = await state.get_data()
        db = load_db()
        code = data["code"]

        db[code] = {
            "type": "series",
            "poster_file_id": data["poster_file_id"],
            "poster_caption": data["poster_caption"],
            "episodes": data["episodes"],
            "channel_msg_id": None,
            "trailer": {
                "file_id": message.video.file_id,
                "caption": message.caption or ""
            }
        }

        save_db(db)

        kb = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton("✅ Kanalga jo'nataymi", callback_data=f"publish_series:{code}"),
            types.InlineKeyboardButton("❌ Yo jo'natmayinmi?", callback_data="cancel_send")
        )

        await message.answer(f"✅ Serial saqlandi\n🆔 Kod: {code}\n\nKanalga yuboraymi?", reply_markup=kb)
        await state.finish()

    except Exception as e:
        print(f"[SERIES TRAILER ERROR] {e}")


@dp.message_handler(lambda m: (m.text or "").lower() == "/skip", state=AddSeries.trailer)
async def skip_series_trailer(message: types.Message, state: FSMContext):
    try:
        data = await state.get_data()
        db = load_db()
        code = data["code"]

        db[code] = {
            "type": "series",
            "poster_file_id": data["poster_file_id"],
            "poster_caption": data["poster_caption"],
            "episodes": data["episodes"],
            "channel_msg_id": None,
            "trailer": None
        }

        save_db(db)

        kb = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton("✅ Kanalga jo'nataymi", callback_data=f"publish_series:{code}"),
            types.InlineKeyboardButton("❌ Yo jo'natmayinmi?", callback_data="cancel_send")
        )

        await message.answer(f"✅ Serial saqlandi\n🆔 Kod: {code}\n\nKanalga yuboraymi?", reply_markup=kb)
        await state.finish()

    except Exception as e:
        print(f"[SERIES SKIP ERROR] {e}")

# ================== KANALGA YUBORISH ==================

@dp.callback_query_handler(lambda c: c.data == "cancel_send")
async def cancel_send(call: types.CallbackQuery):
    try:
        await call.message.edit_text("❎ Bekor qilindi")
        await call.answer()
    except Exception as e:
        print(f"[CANCEL SEND ERROR] {e}")


@dp.callback_query_handler(lambda c: (c.data or "").startswith("publish_movie:"))
async def publish_movie(call: types.CallbackQuery):
    try:
        if not is_admin(call.from_user.id):
            await call.answer("❌ Brat, bu joy adminniki 😄", show_alert=True)
            return

        code = call.data.split(":", 1)[1]

        ok, msg = await publish_to_channel(code)

        await call.message.edit_text(msg)
        await call.answer()

    except Exception as e:
        print(f"[PUBLISH MOVIE ERROR] {e}")
        await call.answer("❌ Xatolik yuz berdi", show_alert=True)


@dp.callback_query_handler(lambda c: (c.data or "").startswith("publish_series:"))
async def publish_series(call: types.CallbackQuery):
    try:
        if not is_admin(call.from_user.id):
            await call.answer("❌ Brat, bu joy adminniki 😄", show_alert=True)
            return

        code = call.data.split(":", 1)[1]

        ok, msg = await publish_to_channel(code)

        await call.message.edit_text(msg)
        await call.answer()

    except Exception as e:
        print(f"[PUBLISH SERIES ERROR] {e}")
        await call.answer("❌ Xatolik yuz berdi", show_alert=True)

# ================== QIDIRISH (KOD) ==================

@dp.message_handler(lambda m: (m.text or "").strip().isdigit())
async def search_movie(message: types.Message):
    try:
        uid = message.from_user.id
        kb = admin_menu() if is_admin(uid) else user_menu()

        # ================== OBUNA ==================
        if protect_for(uid):
            if not await check_subscription(uid):
                await message.answer(
                    "❗ Avval kanalga obuna bo‘ling",
                    reply_markup=subscribe_kb()
                )
                return

        db = load_db()
        code = (message.text or "").strip()
        item = db.get(code)

        if not item:
            await message.answer("❌ Bunday kodli kino topilmadi", reply_markup=kb)
            return

        update_stats(db)

        # ================== MOVIE ==================
        if item.get("type") == "movie":
            token = str(random.randint(100000, 999999))

            last_movie_request[uid] = code
            last_watch_token[uid] = token

            caption = item.get("post_caption", "") or ""

            await message.answer_photo(
                item["post_file_id"],
                caption,
                parse_mode="HTML",
                reply_markup=_merge_kb(
                    movie_watch_kb(code, token),
                    trailer_kb(code) if item.get("trailer") else None
                ),
                protect_content=protect_for(uid)
            )
            return

        # ================== SERIES ==================
        caption = item.get("poster_caption", "") or ""

        kb_inline = types.InlineKeyboardMarkup()
        kb_inline.add(
            types.InlineKeyboardButton(
                "📺 Barcha qismlari",
                callback_data=f"series_private:{code}"
            )
        )

        # Treyler qo‘shamiz agar bor bo‘lsa
        if item.get("trailer"):
            kb_inline.add(
                types.InlineKeyboardButton(
                    "🎬 Treyler",
                    callback_data=f"trailer:{code}"
                )
            )

        await message.answer_photo(
            item["poster_file_id"],
            caption,
            parse_mode="HTML",
            reply_markup=kb_inline,
            protect_content=protect_for(uid)
        )

    except Exception as e:
        print(f"[SEARCH ERROR] {e}")


# ================== KB MERGE ==================

def _merge_kb(kb1, kb2):
    try:
        if not kb2:
            return kb1

        kb = types.InlineKeyboardMarkup()

        for row in kb1.inline_keyboard:
            kb.row(*row)

        for row in kb2.inline_keyboard:
            kb.row(*row)

        return kb

    except Exception:
        return kb1

# ================== FILMNI KO‘RISH (YAKKA) ==================

@dp.callback_query_handler(lambda c: (c.data or "").startswith("watch_"))
async def watch_old(call: types.CallbackQuery):
    await call.answer(
        "❗ Tugma eskirgan. Faqat oxirgi so'ralgan filmni ko'rishingiz mumkin. "
        "Ushbu filmni ko'rish uchun esa kod orqali qayta qidiring yoki "
        "kanaldagi bu film posti ostidagi ko'rish tugmasini bosing ",
        show_alert=True
    )


@dp.callback_query_handler(lambda c: (c.data or "").startswith("watch2_"))
async def watch_movie(call: types.CallbackQuery):
    try:
        parts = call.data.split("_", 2)

        if len(parts) != 3:
            await call.answer("❌ Topilmadi", show_alert=True)
            return

        code = parts[1]
        token = parts[2]
        uid = call.from_user.id

        # ================== TOKEN TEKSHIRISH ==================
        if (
            last_movie_request.get(uid) != code or
            last_watch_token.get(uid) != token or
            token in used_tokens
        ):
            await call.answer(
        "❗ Tugma eskirgan. Faqat oxirgi so'ralgan filmni ko'rishingiz mumkin. "
        "Ushbu filmni ko'rish uchun esa kod orqali qayta qidiring yoki "
        "kanaldagi bu film posti ostidagi ko'rish tugmasini bosing ",
                show_alert=True
            )
            return

        # ================== OBUNA ==================
        if protect_for(uid):
            if not await check_subscription(uid):
                await call.message.answer(
                    "❗ Avval kanalga obuna bo‘ling",
                    reply_markup=subscribe_kb()
                )
                await call.answer()
                return

        db = load_db()
        item = db.get(code)

        if not item or item.get("type") != "movie":
            await call.answer("❌ Topilmadi", show_alert=True)
            return

        # ================== VIDEO YUBORISH ==================
        await bot.send_video(
            uid,
            item["video_file_id"],
            protect_content=protect_for(uid)
        )

        # ================== TOKENNI O‘CHIRISH ==================
        used_tokens.add(token)
        last_watch_token.pop(uid, None)

        await call.answer()

    except Exception as e:
        print(f"[WATCH MOVIE ERROR] {e}")
        await call.answer("❌ Xatolik yuz berdi", show_alert=True)

# ================== SERIALNI USERGA YUBORISH (kanalga emas) ==================

async def send_series_to_user(user_id: int, code: str):
    try:
        if protect_for(user_id):
            if not await check_subscription(user_id):
                await bot.send_message(
                    user_id,
                    "❗ Avval kanalga obuna bo‘ling",
                    reply_markup=subscribe_kb()
                )
                return

        db = load_db()
        item = db.get(code)

        if not item or item.get("type") != "series":
            await bot.send_message(
                user_id,
                "❌ Bunday kodli kino topilmadi",
                reply_markup=user_menu()
            )
            return

        ep_nums = _sorted_episode_numbers(item)
        if not ep_nums:
            await bot.send_message(
                user_id,
                "❌ Qismlar topilmadi",
                reply_markup=user_menu()
            )
            return

        # ================== INLINE KB ==================
        kb = series_eps_kb(code, ep_nums)

        # Treyler tugma qo‘shamiz
        if item.get("trailer"):
            kb.add(
                types.InlineKeyboardButton(
                    "🎬 Treyler",
                    callback_data=f"trailer:{code}"
                )
            )

        ch_msg_id = item.get("channel_msg_id")

        if ch_msg_id:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=CHANNEL2_ID,
                message_id=ch_msg_id,
                reply_markup=kb,
                protect_content=protect_for(user_id)
            )
        else:
            await bot.send_photo(
                chat_id=user_id,
                photo=item["poster_file_id"],
                caption=item.get("poster_caption", ""),
                parse_mode="HTML",
                reply_markup=kb,
                protect_content=protect_for(user_id)
            )

    except Exception as e:
        print(f"[SEND SERIES ERROR] {e}")


@dp.callback_query_handler(lambda c: (c.data or "").startswith("series_private:"))
async def series_private_from_bot(call: types.CallbackQuery):
    try:
        code = call.data.split(":", 1)[1]
        await send_series_to_user(call.from_user.id, code)
        await call.answer()
    except Exception as e:
        print(f"[SERIES PRIVATE ERROR] {e}")


@dp.callback_query_handler(lambda c: (c.data or "").startswith("series_ep:"))
async def series_ep(call: types.CallbackQuery):
    try:
        _, code, ep_str = call.data.split(":")
        ep_num = int(ep_str)
        uid = call.from_user.id

        # ================== OBUNA ==================
        if protect_for(uid):
            if not await check_subscription(uid):
                await call.message.answer(
                    "❗ Avval kanalga obuna bo‘ling",
                    reply_markup=subscribe_kb()
                )
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
            uid,
            ep["video_file_id"],
            caption=cap,
            protect_content=protect_for(uid)
        )

        await call.answer()

    except Exception as e:
        print(f"[SERIES EP ERROR] {e}")
        await call.answer("❌ Xatolik yuz berdi", show_alert=True)

# ================== STATISTIKA ==================

def stats_text():
    try:
        stats = load_stats()

        movies = stats.get("movies", 0)
        series = stats.get("series", 0)
        trailers = stats.get("trailers", 0)

        return (
            "📊 <b>Bot statistikasi</b>\n\n"
            f"🎬 Filmlar: <b>{movies}</b>\n"
            f"📺 Seriallar: <b>{series}</b>\n"
            f"🎥 Treylerlar: <b>{trailers}</b>"
        )

    except Exception as e:
        print(f"[STATS TEXT ERROR] {e}")
        return "❌ Statistikani yuklab bo‘lmadi"


def stats_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("🔄 Yangilash", callback_data="stats_refresh"),
        types.InlineKeyboardButton("❌ Yopish", callback_data="stats_close")
    )
    return kb


@dp.message_handler(lambda m: (m.text or "").strip() == "📊 Statistika")
async def show_stats(message: types.Message):
    try:
        if not is_admin(message.from_user.id):
            await message.answer(
                "❌ <b>Brat siz admin emassiz!</b>\n"
                "🎬 Faqat <b>Qidiruv</b> tugmasidan foydalanishingiz mumkin.",
                reply_markup=user_menu()
            )
            return

        await message.answer(stats_text(), reply_markup=stats_kb())

    except Exception as e:
        print(f"[SHOW STATS ERROR] {e}")


@dp.callback_query_handler(lambda c: (c.data or "") == "stats_refresh")
async def refresh_stats(call: types.CallbackQuery):
    try:
        await call.message.edit_text(stats_text(), reply_markup=stats_kb())
        await call.answer()
    except Exception as e:
        print(f"[REFRESH STATS ERROR] {e}")


@dp.callback_query_handler(lambda c: (c.data or "") == "stats_close")
async def close_stats(call: types.CallbackQuery):
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.answer()

# ================== BACKUP ==================

@dp.message_handler(lambda m: (m.text or "").strip() == "📦 Kino backup")
async def backup_movies(message: types.Message):
    try:
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

        await message.answer_document(
            types.InputFile(MOVIES_FILE),
            reply_markup=admin_menu()
        )

    except Exception as e:
        print(f"[BACKUP MOVIES ERROR] {e}")


@dp.message_handler(lambda m: (m.text or "").strip() == "📈 Statistika backup")
async def backup_stats(message: types.Message):
    try:
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

        await message.answer_document(
            types.InputFile(STATS_FILE),
            reply_markup=admin_menu()
        )

    except Exception as e:
        print(f"[BACKUP STATS ERROR] {e}")

# ================== RESTORE (ADMIN PANEL) ==================

@dp.message_handler(lambda m: (m.text or "").strip() == "♻️ Kino restore")
async def restore_movies_btn(message: types.Message, state: FSMContext):
    try:
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
            "📎 <b>movies.json</b> faylni yuboring (Document).",
            reply_markup=admin_menu()
        )

        await RestoreFlow.movies.set()

    except Exception as e:
        print(f"[RESTORE MOVIES BTN ERROR] {e}")


@dp.message_handler(lambda m: (m.text or "").strip() == "♻️ Statistika restore")
async def restore_stats_btn(message: types.Message, state: FSMContext):
    try:
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
            "📎 <b>statistics.json</b> faylni yuboring (Document).",
            reply_markup=admin_menu()
        )

        await RestoreFlow.stats.set()

    except Exception as e:
        print(f"[RESTORE STATS BTN ERROR] {e}")


# ================== MOVIES RESTORE ==================

@dp.message_handler(state=RestoreFlow.movies, content_types=types.ContentType.DOCUMENT)
async def restore_movies_file(message: types.Message, state: FSMContext):
    try:
        if not is_admin(message.from_user.id):
            await state.finish()
            return

        doc = message.document

        if not doc or (doc.file_name or "").lower() != "movies.json":
            await message.answer("❗ Faqat <b>movies.json</b> yuboring.", reply_markup=admin_menu())
            return

        f = await bot.get_file(doc.file_id)

        _ensure_parent_dir(MOVIES_FILE)
        await bot.download_file(f.file_path, MOVIES_FILE)

        _ = load_db()  # tekshirish

        await message.answer(
            f"✅ Tiklandi!\n📌 Saqlandi: <code>{MOVIES_FILE}</code>",
            reply_markup=admin_menu()
        )

    except Exception as e:
        print(f"[RESTORE MOVIES ERROR] {e}")
        await message.answer(
            "❌ Restore bo‘lmadi. Fayl buzilgan bo‘lishi mumkin.",
            reply_markup=admin_menu()
        )

    finally:
        await state.finish()


# ================== STATS RESTORE ==================

@dp.message_handler(state=RestoreFlow.stats, content_types=types.ContentType.DOCUMENT)
async def restore_stats_file(message: types.Message, state: FSMContext):
    try:
        if not is_admin(message.from_user.id):
            await state.finish()
            return

        doc = message.document

        if not doc or (doc.file_name or "").lower() != "statistics.json":
            await message.answer("❗ Faqat <b>statistics.json</b> yuboring.", reply_markup=admin_menu())
            return

        f = await bot.get_file(doc.file_id)

        _ensure_parent_dir(STATS_FILE)
        await bot.download_file(f.file_path, STATS_FILE)

        _ = load_stats()  # tekshirish

        await message.answer(
            f"✅ Tiklandi!\n📌 Saqlandi: <code>{STATS_FILE}</code>",
            reply_markup=admin_menu()
        )

    except Exception as e:
        print(f"[RESTORE STATS ERROR] {e}")
        await message.answer(
            "❌ Restore bo‘lmadi. Fayl buzilgan bo‘lishi mumkin.",
            reply_markup=admin_menu()
        )

    finally:
        await state.finish()


# ================== WAIT HANDLERS ==================

@dp.message_handler(state=RestoreFlow.movies)
async def restore_movies_wait(message: types.Message):
    await message.answer(
        "📎 Iltimos, <b>movies.json</b> faylni yuboring.",
        reply_markup=admin_menu()
    )


@dp.message_handler(state=RestoreFlow.stats)
async def restore_stats_wait(message: types.Message):
    await message.answer(
        "📎 Iltimos, <b>statistics.json</b> faylni yuboring.",
        reply_markup=admin_menu()
    )

# ================== O‘CHIRISH ==================

@dp.message_handler(lambda m: (m.text or "").strip() == "🗑 O‘chirish")
async def del_btn(message: types.Message, state: FSMContext):
    try:
        if not is_admin(message.from_user.id):
            await message.answer(
                "❌ <b>Brat siz admin emassiz!</b>\n"
                "🎬 Faqat <b>Qidiruv</b> tugmasidan foydalanishingiz mumkin.",
                reply_markup=user_menu()
            )
            return

        await state.finish()
        await message.answer("🗑 Kodni yuboring tog'o", reply_markup=admin_menu())
        await DeleteFlow.code.set()

    except Exception as e:
        print(f"[DELETE BTN ERROR] {e}")


@dp.message_handler(state=DeleteFlow.code)
async def delete_item(message: types.Message, state: FSMContext):
    try:
        code = (message.text or "").strip()

        if not code.isdigit():
            await message.answer("🗑 Kodni yuboring tog'o", reply_markup=admin_menu())
            return

        db = load_db()
        item = db.get(code)

        if not item:
            await message.answer("❌ Bunaqa kino yo'q tog'o", reply_markup=admin_menu())
            await state.finish()
            return

        # ================== 2K (ASOSIY KANAL) ==================
        msg_id = item.get("channel_msg_id")
        if msg_id:
            try:
                await bot.delete_message(CHANNEL2_ID, msg_id)
            except Exception:
                pass

        # ================== 3K (TREYLER KANAL) ==================
        trailer = item.get("trailer") or {}
        trailer_msg_id = trailer.get("message_id")

        if trailer_msg_id:
            try:
                await bot.delete_message(CHANNEL1_ID, trailer_msg_id)
            except Exception:
                pass

        # ================== DB DAN O‘CHIRISH ==================
        del db[code]
        save_db(db)

        update_stats(db)

        await message.answer(
            f"🗑 O'chirib tashadim tog'o\n🆔 Kod: {code}",
            reply_markup=admin_menu()
        )

        await state.finish()

    except Exception as e:
        print(f"[DELETE ERROR] {e}")
        await message.answer("❌ Xatolik yuz berdi", reply_markup=admin_menu())
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
        types.InlineKeyboardButton("🎬 Treyler qo‘shish", callback_data=f"trailer_add:{code}"),
        types.InlineKeyboardButton("🗑 Treyler o‘chirish", callback_data=f"trailer_del:{code}"),
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
        types.InlineKeyboardButton("🎬 Treyler qo‘shish", callback_data=f"trailer_add:{code}"),
        types.InlineKeyboardButton("🗑 Treyler o‘chirish", callback_data=f"trailer_del:{code}"),
        types.InlineKeyboardButton("🗑 Serialni o‘chirish", callback_data=f"edit_delete:{code}")
    )
    return kb

@dp.message_handler(lambda m: m.text == "✏️ Tahrirlash")
async def edit_start(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Admin emassiz", reply_markup=user_menu())
        return
    await state.finish()
    await message.answer("Nimani tahrirlaymiz?", reply_markup=edit_type_kb())
    await EditFlow.choose_type.set()

@dp.callback_query_handler(lambda c: c.data.startswith("edit_type:"), state=EditFlow.choose_type)
async def edit_choose_type(call: types.CallbackQuery, state: FSMContext):
    typ = call.data.split(":")[1]
    await state.update_data(edit_type=typ)
    await call.message.edit_text("🆔 Kod yuboring")
    await EditFlow.choose_code.set()
    await call.answer()

@dp.message_handler(state=EditFlow.choose_code)
async def edit_choose_code(message: types.Message, state: FSMContext):
    code = message.text.strip()
    db = load_db()
    item = db.get(code)

    if not item:
        await message.answer("❌ Topilmadi")
        return

    await state.update_data(code=code)

    if item["type"] == "movie":
        await message.answer("🎬 Tahrirlash", reply_markup=edit_movie_kb(code))
    else:
        await message.answer("📺 Tahrirlash", reply_markup=edit_series_kb(code))

    await EditFlow.choose_action.set()

# ================== TREYLER ADD ==================
@dp.callback_query_handler(lambda c: c.data.startswith("trailer_add:"), state=EditFlow.choose_action)
async def trailer_add(call: types.CallbackQuery, state: FSMContext):
    code = call.data.split(":")[1]
    await state.update_data(pending=("trailer_add", code))
    await call.message.answer("🎬 Treyler yuboring")
    await EditFlow.await_forward.set()
    await call.answer()

# ================== TREYLER DELETE ==================
@dp.callback_query_handler(lambda c: c.data.startswith("trailer_del:"), state=EditFlow.choose_action)
async def trailer_del(call: types.CallbackQuery, state: FSMContext):
    code = call.data.split(":")[1]
    db = load_db()
    item = db.get(code)

    trailer = item.get("trailer") or {}

    if trailer.get("message_id"):
        try:
            await bot.delete_message(TRAILER_CHANNEL_ID, trailer["message_id"])
        except:
            pass

    item["trailer"] = None
    db[code] = item
    save_db(db)

    await call.message.answer("🗑 Treyler o‘chirildi")
    await call.answer()

# ================== DELETE ==================
@dp.callback_query_handler(lambda c: c.data.startswith("edit_delete:"), state=EditFlow.choose_action)
async def edit_delete(call: types.CallbackQuery, state: FSMContext):
    code = call.data.split(":")[1]
    db = load_db()
    item = db.get(code)

    if item.get("channel_msg_id"):
        try:
            await bot.delete_message(CHANNEL2_ID, item["channel_msg_id"])
        except:
            pass

    trailer = item.get("trailer") or {}
    if trailer.get("message_id"):
        try:
            await bot.delete_message(TRAILER_CHANNEL_ID, trailer["message_id"])
        except:
            pass

    del db[code]
    save_db(db)
    update_stats(db)

    await call.message.answer("🗑 O‘chirildi")
    await call.answer()

# ================== FORWARD ==================
@dp.message_handler(state=EditFlow.await_forward, content_types=types.ContentType.ANY)
async def edit_forward(message: types.Message, state: FSMContext):
    data = await state.get_data()
    action, code = data.get("pending")
    db = load_db()
    item = db.get(code)

    # -------- movie_post --------
    if action == "movie_post":
        item["post_file_id"] = message.photo[-1].file_id
        item["post_caption"] = message.caption or ""

    # -------- movie_video --------
    elif action == "movie_video":
        item["video_file_id"] = message.video.file_id

    # -------- series_post --------
    elif action == "series_post":
        item["poster_file_id"] = message.photo[-1].file_id
        item["poster_caption"] = message.caption or ""

    # -------- series add/replace --------
    elif action in ("series_add", "series_replace"):
        ep, title = _parse_episode_caption(message.caption or "")
        eps = item.get("episodes", {})
        eps[str(ep)] = {
            "video_file_id": message.video.file_id,
            "title": title
        }
        item["episodes"] = eps

    # -------- trailer_add --------
    elif action == "trailer_add":
        trailer = item.get("trailer") or {}

        if trailer.get("message_id"):
            try:
                await bot.delete_message(TRAILER_CHANNEL_ID, trailer["message_id"])
            except:
                pass

        msg = await bot.send_video(
            TRAILER_CHANNEL_ID,
            message.video.file_id,
            caption=message.caption or "",
            parse_mode="HTML"
        )

        item["trailer"] = {
            "file_id": message.video.file_id,
            "caption": message.caption,
            "message_id": msg.message_id
        }

    db[code] = item
    save_db(db)

    await message.answer("♻️ Yangilandi")
    await state.finish()
