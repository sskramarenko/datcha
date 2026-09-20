# -*- coding: utf-8 -*-
"""
Дачный бот для чата «Дачные истории».

Умеет:
  • собирать выезд: голосование за дату, перекличка, напоминания
  • вести стол: кто что везёт, ловит дубли, показывает пробелы
  • считать кассу: кто сколько потратил и кто кому должен
  • логистику: у кого машина и сколько мест
  • игротеку: список настолок, случайный выбор, оценки
  • светофор: погода на даче и заметка о состоянии дома

Запуск: токен в token.txt рядом с файлом либо переменная BOT_TOKEN.
"""

import asyncio
import json
import logging
import os
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, ErrorEvent, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

# ---------------------------------------------------------------- настройки

DATE_OPTIONS = 4        # сколько ближайших выходных предлагать
PING_DAYS_BEFORE = 3    # за сколько дней пинговать молчунов
DACHA_LAT = float(os.getenv("DACHA_LAT", "56.003470"))
DACHA_LON = float(os.getenv("DACHA_LON", "38.213553"))
DACHA_NAME = os.getenv("DACHA_NAME", "Поместье Сергея")

MSK = timezone(timedelta(hours=3))
BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "dacha_data.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dacha-bot")

MONTHS = ("января февраля марта апреля мая июня июля августа сентября "
          "октября ноября декабря").split()
WEEKDAYS = ("понедельник вторник среда четверг пятница суббота воскресенье").split()

# категории стола: как понять, чего не хватает
CATEGORIES = {
    "мясо и гриль": "мясо шашлык курица свинина баранина стейк колбас сосиск шампур гриль рыба",
    "салаты": "салат овощ помидор огурц капуст зелен винегрет",
    "гарнир": "картош рис гречк паста макарон гарнир",
    "закуски": "сыр хлеб лаваш соленья грибы паштет намазк закуск оливк",
    "сладкое": "торт пирог шарлотк печень конфет десерт пирожн мороженое",
    "напитки": "вода сок морс лимонад газировк чай кофе компот напит вино пиво настойк",
}

# ---------------------------------------------------------------- хранилище

DEFAULT_DATA = {
    "events": {},   # chat_id -> событие
    "games": {},    # chat_id -> [{name, votes:{user:score}}]
    "users": {},    # user_id -> имя
    "house": {},    # chat_id -> заметка о доме
}
data: dict = dict(DEFAULT_DATA)


def now_msk() -> datetime:
    return datetime.now(MSK)


def load_data() -> None:
    global data
    if DATA_FILE.exists():
        try:
            data = {**DEFAULT_DATA, **json.loads(DATA_FILE.read_text(encoding="utf-8"))}
            return
        except Exception as e:
            log.warning("Не прочитать данные: %s", e)
    data = dict(DEFAULT_DATA)


def save_data() -> None:
    try:
        DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        log.warning("Не сохранить данные: %s", e)


def uname(message_or_call) -> str:
    u = message_or_call.from_user
    return u.first_name or u.username or "Гость"


def remember(obj) -> None:
    u = obj.from_user
    if u and not u.is_bot:
        data["users"][str(u.id)] = u.first_name or u.username or "Гость"


def event_of(chat_id: int) -> dict | None:
    return data["events"].get(str(chat_id))


def fmt_date(iso: str) -> str:
    d = datetime.strptime(iso, "%Y-%m-%d")
    return f"{d.day} {MONTHS[d.month - 1]}, {WEEKDAYS[d.weekday()]}"


def next_weekends(count: int) -> list[str]:
    """Ближайшие субботы и воскресенья."""
    today = now_msk().date()
    out = []
    d = today + timedelta(days=1)
    while len(out) < count * 2:
        if d.weekday() in (5, 6):
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out

# ---------------------------------------------------------------- тексты


def render_dates(ev: dict) -> str:
    lines = ["📅 <b>Когда собираемся?</b>", "Отметьте все даты, когда можете — передумать можно в любой момент.\n"]
    for iso in ev["dates"]:
        voters = [v for v, ds in ev["date_votes"].items() if iso in ds]
        names = ", ".join(data["users"].get(v, "кто-то") for v in voters)
        lines.append(f"<b>{fmt_date(iso)}</b> — {len(voters)}" + (f" ({names})" if names else ""))
    lines.append("\nКогда определитесь — кнопка «Зафиксировать дату».")
    return "\n".join(lines)


def dates_keyboard(ev: dict) -> InlineKeyboardMarkup:
    rows = []
    for i, iso in enumerate(ev["dates"]):
        count = sum(1 for ds in ev["date_votes"].values() if iso in ds)
        mark = f" · {count}" if count else ""
        rows.append([InlineKeyboardButton(text=f"{fmt_date(iso)}{mark}", callback_data=f"d:{i}")])
    rows.append([InlineKeyboardButton(text="✅ Зафиксировать дату", callback_data="d:fix")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def render_event(ev: dict) -> str:
    going = [v["name"] for v in ev["rsvp"].values() if v["answer"] == "еду"]
    maybe = [v["name"] for v in ev["rsvp"].values() if v["answer"] == "может"]
    no = [v["name"] for v in ev["rsvp"].values() if v["answer"] == "не еду"]

    lines = [f"🏡 <b>Выезд на дачу: {fmt_date(ev['date'])}</b>"]
    if ev.get("time"):
        lines.append(f"Сбор в <b>{ev['time']}</b>")
    lines.append("")
    lines.append(f"✅ Едут ({len(going)}): {', '.join(going) or '—'}")
    if maybe:
        lines.append(f"🤔 Под вопросом: {', '.join(maybe)}")
    if no:
        lines.append(f"❌ Не смогут: {', '.join(no)}")

    if ev["table"]:
        lines.append("\n🍽 <b>Стол</b>")
        for item in ev["table"]:
            lines.append(f"  • {item['what']} — <i>{item['name']}</i>")
        missing = missing_categories(ev)
        if missing:
            lines.append(f"<i>Не хватает: {', '.join(missing)}</i>")

    cars = [c for c in ev["cars"].values()]
    if cars or ev["need_ride"]:
        lines.append("\n🚗 <b>Машины</b>")
        for c in cars:
            lines.append(f"  • {c['name']} — свободно мест: {c['seats']}")
        if ev["need_ride"]:
            lines.append(f"  Нужно место: {', '.join(ev['need_ride'].values())}")

    if ev["spends"]:
        total = sum(s["amount"] for s in ev["spends"])
        lines.append(f"\n💰 Потрачено: <b>{total:.0f} ₽</b> · /касса — расчёт")

    lines.append("\n<code>/везу салат</code> · <code>/машина 3</code> · <code>/потратил 3500 мясо</code>")
    return "\n".join(lines)


def rsvp_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Еду", callback_data="r:еду"),
        InlineKeyboardButton(text="🤔 Может", callback_data="r:может"),
        InlineKeyboardButton(text="❌ Не еду", callback_data="r:не еду"),
    ]])


def missing_categories(ev: dict) -> list[str]:
    have = " ".join(i["what"].lower() for i in ev["table"])
    missing = []
    for cat, words in CATEGORIES.items():
        if not any(w in have for w in words.split()):
            missing.append(cat)
    return missing


def find_duplicate(ev: dict, what: str) -> dict | None:
    key = re.sub(r"[^а-яёa-z]", "", what.lower())[:5]
    if len(key) < 3:
        return None
    for item in ev["table"]:
        if re.sub(r"[^а-яёa-z]", "", item["what"].lower())[:5] == key:
            return item
    return None

# ---------------------------------------------------------------- хэндлеры

router = Router()

HELP = (
    "🏡 <b>Дачный бот</b>\n\n"
    "<b>Выезд</b>\n"
    "/выезд — собрать новый выезд (голосование за дату → перекличка)\n"
    "/кто — кто едет, что везут, кто на машине\n"
    "/сбор 11:00 — назначить время\n"
    "/отбой — отменить выезд\n\n"
    "<b>Стол</b>\n"
    "/везу шарлотку — записать за собой блюдо\n"
    "/стол — список и чего не хватает\n"
    "/неВезу шарлотку — передумал\n\n"
    "<b>Деньги</b>\n"
    "/потратил 3500 мясо — записать трату\n"
    "/касса — кто кому сколько должен\n\n"
    "<b>Машины</b>\n"
    "/машина 3 — еду за рулём, свободно 3 места\n"
    "/подвезите — нужно место\n\n"
    "<b>Игры</b>\n"
    "/игра — выбрать настолку на вечер\n"
    "/игры — список с оценками\n"
    "/новаяИгра Фиеста — добавить\n"
    "/оценка Фиеста 5 — поставить оценку\n\n"
    "<b>Дача</b>\n"
    "/погода — прогноз на выходные\n"
    "/дом вода есть, дорога чистая — заметка хозяина\n"
    "/дом — прочитать заметку"
)


@router.message(CommandStart())
@router.message(Command("help", "помощь"))
async def cmd_help(message: Message):
    remember(message)
    save_data()
    await message.answer(HELP)


# ---- выезд

@router.message(Command("выезд", "vyezd", "dacha"))
async def cmd_vyezd(message: Message):
    remember(message)
    ev = event_of(message.chat.id)
    if ev and ev.get("date"):
        await message.answer("Выезд уже собран 🙂\n\n" + render_event(ev), reply_markup=rsvp_keyboard())
        return
    if ev and not ev.get("date"):
        await message.answer(render_dates(ev), reply_markup=dates_keyboard(ev))
        return

    ev = {
        "dates": next_weekends(DATE_OPTIONS),
        "date_votes": {},
        "date": None,
        "time": None,
        "rsvp": {},
        "table": [],
        "cars": {},
        "need_ride": {},
        "spends": [],
        "pinged": False,
        "reminded": False,
        "author": uname(message),
    }
    data["events"][str(message.chat.id)] = ev
    save_data()
    await message.answer(render_dates(ev), reply_markup=dates_keyboard(ev))


@router.callback_query(F.data.startswith("d:"))
async def on_date_vote(call: CallbackQuery):
    remember(call)
    ev = event_of(call.message.chat.id)
    if not ev or ev.get("date"):
        await call.answer("Голосование уже закрыто")
        return

    arg = call.data.split(":", 1)[1]
    uid = str(call.from_user.id)

    if arg == "fix":
        counts = {iso: sum(1 for ds in ev["date_votes"].values() if iso in ds) for iso in ev["dates"]}
        if not any(counts.values()):
            await call.answer("Сначала кто-нибудь должен проголосовать")
            return
        best = max(counts, key=lambda k: counts[k])
        ev["date"] = best
        # кто голосовал за эту дату — сразу «еду»
        for voter, ds in ev["date_votes"].items():
            if best in ds:
                ev["rsvp"][voter] = {"name": data["users"].get(voter, "Гость"), "answer": "еду"}
        save_data()
        await call.message.edit_text(
            f"📅 Дата зафиксирована: <b>{fmt_date(best)}</b>\n"
            f"За неё голосовали {counts[best]} чел.\n\nОтметьтесь, кто едет 👇"
        )
        await call.message.answer(render_event(ev), reply_markup=rsvp_keyboard())
        await call.answer("Готово")
        return

    iso = ev["dates"][int(arg)]
    chosen = set(ev["date_votes"].get(uid, []))
    chosen.symmetric_difference_update({iso})
    ev["date_votes"][uid] = sorted(chosen)
    save_data()
    await call.message.edit_text(render_dates(ev), reply_markup=dates_keyboard(ev))
    await call.answer("Отмечено" if iso in chosen else "Убрано")


@router.callback_query(F.data.startswith("r:"))
async def on_rsvp(call: CallbackQuery):
    remember(call)
    ev = event_of(call.message.chat.id)
    if not ev or not ev.get("date"):
        await call.answer("Выезд не собран")
        return
    answer = call.data.split(":", 1)[1]
    ev["rsvp"][str(call.from_user.id)] = {"name": uname(call), "answer": answer}
    save_data()
    try:
        await call.message.edit_text(render_event(ev), reply_markup=rsvp_keyboard())
    except Exception:
        pass
    await call.answer(f"Записал: {answer}")


@router.message(Command("кто", "kto"))
async def cmd_kto(message: Message):
    ev = event_of(message.chat.id)
    if not ev:
        await message.answer("Выезд не собран. /выезд — начать.")
    elif not ev.get("date"):
        await message.answer(render_dates(ev), reply_markup=dates_keyboard(ev))
    else:
        await message.answer(render_event(ev), reply_markup=rsvp_keyboard())


@router.message(Command("сбор", "sbor"))
async def cmd_sbor(message: Message):
    ev = event_of(message.chat.id)
    if not ev or not ev.get("date"):
        await message.answer("Сначала /выезд.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Напишите так: <code>/сбор 11:00</code>")
        return
    ev["time"] = parts[1].strip()
    save_data()
    await message.answer(f"Сбор в <b>{ev['time']}</b>, записал.")


@router.message(Command("отбой", "otboy"))
async def cmd_otboy(message: Message):
    if data["events"].pop(str(message.chat.id), None):
        save_data()
        await message.answer("Выезд отменён. /выезд — собрать заново.")
    else:
        await message.answer("Нечего отменять.")


# ---- стол

@router.message(Command("везу", "vezu"))
async def cmd_vezu(message: Message):
    remember(message)
    ev = event_of(message.chat.id)
    if not ev or not ev.get("date"):
        await message.answer("Сначала /выезд — иначе непонятно, к какому столу 🙂")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Напишите так: <code>/везу шарлотку</code>")
        return

    what = parts[1].strip()
    dup = find_duplicate(ev, what)
    if dup:
        await message.reply(
            f"«{dup['what']}» уже везёт {dup['name']} 🙂\nМожет, взять что-то другое? "
            f"Не хватает: {', '.join(missing_categories(ev)) or 'вроде всё есть'}"
        )
        return

    ev["table"].append({"user": str(message.from_user.id), "name": uname(message), "what": what})
    save_data()
    missing = missing_categories(ev)
    text = f"Записал: <b>{what}</b> — {uname(message)}"
    if missing:
        text += f"\nЕщё нет: {', '.join(missing)}"
    await message.answer(text)


@router.message(Command("неВезу", "nevezu", "невезу"))
async def cmd_nevezu(message: Message):
    ev = event_of(message.chat.id)
    if not ev:
        return
    parts = (message.text or "").split(maxsplit=1)
    uid = str(message.from_user.id)
    before = len(ev["table"])
    if len(parts) < 2:
        ev["table"] = [i for i in ev["table"] if i["user"] != uid]
    else:
        key = parts[1].strip().lower()[:5]
        ev["table"] = [i for i in ev["table"]
                       if not (i["user"] == uid and i["what"].lower().startswith(key))]
    save_data()
    await message.answer("Убрал." if len(ev["table"]) < before else "Не нашёл такой записи.")


@router.message(Command("стол", "stol"))
async def cmd_stol(message: Message):
    ev = event_of(message.chat.id)
    if not ev or not ev.get("date"):
        await message.answer("Выезд не собран. /выезд — начать.")
        return
    if not ev["table"]:
        await message.answer("Стол пока пустой. <code>/везу салат</code>")
        return
    lines = ["🍽 <b>Стол</b>\n"]
    for item in ev["table"]:
        lines.append(f"• {item['what']} — <i>{item['name']}</i>")
    missing = missing_categories(ev)
    lines.append(f"\n<i>{'Не хватает: ' + ', '.join(missing) if missing else 'Похоже, всё есть 👌'}</i>")
    await message.answer("\n".join(lines))


# ---- касса

@router.message(Command("потратил", "potratil"))
async def cmd_potratil(message: Message):
    remember(message)
    ev = event_of(message.chat.id)
    if not ev or not ev.get("date"):
        await message.answer("Сначала /выезд.")
        return
    m = re.match(r"^\S+\s+([\d\s.,]+)\s*(.*)$", (message.text or "").strip())
    if not m:
        await message.answer("Напишите так: <code>/потратил 3500 мясо</code>")
        return
    try:
        amount = float(m.group(1).replace(" ", "").replace(",", "."))
    except ValueError:
        await message.answer("Сумму не понял. Пример: <code>/потратил 3500 мясо</code>")
        return

    ev["spends"].append({
        "user": str(message.from_user.id), "name": uname(message),
        "amount": amount, "what": m.group(2).strip() or "без описания",
    })
    save_data()
    total = sum(s["amount"] for s in ev["spends"])
    await message.answer(f"Записал: <b>{amount:.0f} ₽</b> — {m.group(2).strip()}\nВсего потрачено: {total:.0f} ₽")


@router.message(Command("касса", "kassa"))
async def cmd_kassa(message: Message):
    ev = event_of(message.chat.id)
    if not ev or not ev["spends"]:
        await message.answer("Трат пока нет. <code>/потратил 3500 мясо</code>")
        return

    going = {uid: v["name"] for uid, v in ev["rsvp"].items() if v["answer"] == "еду"}
    for s in ev["spends"]:
        going.setdefault(s["user"], s["name"])
    if not going:
        await message.answer("Непонятно, между кем делить — отметьтесь, кто едет.")
        return

    total = sum(s["amount"] for s in ev["spends"])
    share = total / len(going)
    paid = {uid: 0.0 for uid in going}
    for s in ev["spends"]:
        paid[s["user"]] = paid.get(s["user"], 0) + s["amount"]

    balance = {uid: paid.get(uid, 0) - share for uid in going}
    debtors = sorted([(u, -b) for u, b in balance.items() if b < -1], key=lambda x: -x[1])
    creditors = sorted([(u, b) for u, b in balance.items() if b > 1], key=lambda x: -x[1])

    lines = ["💰 <b>Касса</b>\n"]
    for s in ev["spends"]:
        lines.append(f"• {s['name']}: {s['amount']:.0f} ₽ — {s['what']}")
    lines.append(f"\nИтого <b>{total:.0f} ₽</b> на {len(going)} чел. — по <b>{share:.0f} ₽</b>\n")

    i = j = 0
    moves = []
    debtors = [list(x) for x in debtors]
    creditors = [list(x) for x in creditors]
    while i < len(debtors) and j < len(creditors):
        pay = min(debtors[i][1], creditors[j][1])
        moves.append(f"{going[debtors[i][0]]} → {going[creditors[j][0]]}: <b>{pay:.0f} ₽</b>")
        debtors[i][1] -= pay
        creditors[j][1] -= pay
        if debtors[i][1] < 1:
            i += 1
        if creditors[j][1] < 1:
            j += 1

    lines.append("\n".join(moves) if moves else "Все в расчёте 👌")
    await message.answer("\n".join(lines))


# ---- машины

@router.message(Command("машина", "mashina"))
async def cmd_mashina(message: Message):
    remember(message)
    ev = event_of(message.chat.id)
    if not ev or not ev.get("date"):
        await message.answer("Сначала /выезд.")
        return
    m = re.search(r"(\d+)", message.text or "")
    seats = int(m.group(1)) if m else 0
    ev["cars"][str(message.from_user.id)] = {"name": uname(message), "seats": seats}
    save_data()
    await message.answer(f"🚗 {uname(message)} за рулём, свободных мест: <b>{seats}</b>")


@router.message(Command("подвезите", "podvezite"))
async def cmd_podvezite(message: Message):
    remember(message)
    ev = event_of(message.chat.id)
    if not ev or not ev.get("date"):
        await message.answer("Сначала /выезд.")
        return
    ev["need_ride"][str(message.from_user.id)] = uname(message)
    save_data()
    free = sum(c["seats"] for c in ev["cars"].values())
    await message.answer(
        f"Записал, {uname(message)} ищет место. Свободных мест сейчас: <b>{free}</b>"
    )


# ---- игротека

DEFAULT_GAMES = ["Уно", "Шляпа", "Фиеста", "Свинтус", "Зельеваренье", "Покер", "Дартс",
                 "Это ты (PS4)", "Безумцы (PS4)", "Знание — сила (PS4)"]


def games_of(chat_id: int) -> list:
    """Список игр чата. В первый раз заполняется нашим набором."""
    games = data["games"].setdefault(str(chat_id), [])
    if not games:
        games.extend({"name": n, "votes": {}} for n in DEFAULT_GAMES)
        save_data()
    return games


@router.message(Command("новаяИгра", "новаяигра", "newgame"))
async def cmd_newgame(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Напишите так: <code>/новаяИгра Фиеста</code>")
        return
    name = parts[1].strip()
    games = games_of(message.chat.id)
    if any(g["name"].lower() == name.lower() for g in games):
        await message.answer("Такая игра уже есть в списке.")
        return
    games.append({"name": name, "votes": {}})
    save_data()
    await message.answer(f"🎲 Добавил: <b>{name}</b>. Всего игр: {len(games)}")


@router.message(Command("игры", "igry"))
async def cmd_games(message: Message):
    games = games_of(message.chat.id)
    if not games:
        await message.answer("Список пуст. <code>/новаяИгра Фиеста</code>")
        return
    lines = ["🎲 <b>Наши игры</b>\n"]
    for g in sorted(games, key=lambda g: -(sum(g["votes"].values()) / len(g["votes"]) if g["votes"] else 0)):
        if g["votes"]:
            avg = sum(g["votes"].values()) / len(g["votes"])
            lines.append(f"• {g['name']} — {'⭐' * round(avg)} ({avg:.1f}, голосов: {len(g['votes'])})")
        else:
            lines.append(f"• {g['name']} — <i>ещё не оценивали</i>")
    await message.answer("\n".join(lines))


@router.message(Command("игра", "igra"))
async def cmd_game(message: Message):
    games = games_of(message.chat.id)
    if not games:
        await message.answer("Сначала добавьте игры: <code>/новаяИгра Фиеста</code>")
        return
    g = random.choice(games)
    await message.answer(f"🎲 Сегодня играем в <b>{g['name']}</b>\n\n<i>Оценить потом: /оценка {g['name']} 5</i>")


@router.message(Command("оценка", "ocenka"))
async def cmd_rate(message: Message):
    parts = (message.text or "").rsplit(maxsplit=1)
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Напишите так: <code>/оценка Фиеста 5</code>")
        return
    score = max(1, min(5, int(parts[1])))
    name = parts[0].split(maxsplit=1)[1].strip() if len(parts[0].split(maxsplit=1)) > 1 else ""
    games = games_of(message.chat.id)
    g = next((g for g in games if g["name"].lower().startswith(name.lower()[:4])), None)
    if not g:
        await message.answer(f"Не нашёл игру «{name}». /игры — список.")
        return
    g["votes"][str(message.from_user.id)] = score
    save_data()
    avg = sum(g["votes"].values()) / len(g["votes"])
    await message.answer(f"Записал {score}/5 за <b>{g['name']}</b>. Средняя: {avg:.1f}")


# ---- дача

@router.message(Command("дом", "dom"))
async def cmd_dom(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    chat = str(message.chat.id)
    if len(parts) < 2:
        note = data["house"].get(chat)
        if not note:
            await message.answer("Заметок о доме нет. <code>/дом вода есть, дорога чистая</code>")
        else:
            await message.answer(f"🏠 <b>Состояние дома</b>\n{note['text']}\n<i>{note['when']}, {note['name']}</i>")
        return
    data["house"][chat] = {
        "text": parts[1].strip(),
        "name": uname(message),
        "when": now_msk().strftime("%d.%m"),
    }
    save_data()
    await message.answer("Записал. Покажу перед выездом.")


async def weather_text() -> str:
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={DACHA_LAT}&longitude={DACHA_LON}"
           f"&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,weathercode"
           f"&timezone=Europe%2FMoscow&forecast_days=7")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                j = await r.json()
    except Exception as e:
        log.warning("Погода не получена: %s", e)
        return "Прогноз сейчас не получить — сервис недоступен."

    d = j.get("daily", {})
    lines = [f"🌤 <b>Погода · {DACHA_NAME}</b>\n"]
    for i, day in enumerate(d.get("time", [])):
        dt = datetime.strptime(day, "%Y-%m-%d")
        if dt.weekday() not in (4, 5, 6):
            continue
        tmax, tmin = d["temperature_2m_max"][i], d["temperature_2m_min"][i]
        rain = d["precipitation_sum"][i]
        verdict = "шашлык 👍" if rain < 1 and tmax > 10 else ("дождь ☔️" if rain >= 1 else "прохладно 🧥")
        lines.append(f"<b>{fmt_date(day)}</b>: {tmin:.0f}…{tmax:.0f}°, осадки {rain:.1f} мм — {verdict}")
    return "\n".join(lines) if len(lines) > 1 else "На ближайшие выходные прогноза пока нет."


@router.message(Command("погода", "pogoda"))
async def cmd_weather(message: Message):
    await message.answer(await weather_text())

# ---------------------------------------------------------------- расписание


async def scheduler(bot: Bot) -> None:
    while True:
        try:
            now = now_msk()
            for chat_id, ev in list(data["events"].items()):
                if not ev.get("date"):
                    continue
                event_date = datetime.strptime(ev["date"], "%Y-%m-%d").date()
                days_left = (event_date - now.date()).days

                # пинг молчунов
                if days_left == PING_DAYS_BEFORE and not ev["pinged"] and now.hour == 19:
                    ev["pinged"] = True
                    save_data()
                    silent = [n for uid, n in data["users"].items() if uid not in ev["rsvp"]]
                    text = f"⏳ До выезда {days_left} дня. " + (
                        f"Не отметились: {', '.join(silent)}" if silent else "Все отметились 👌")
                    await bot.send_message(int(chat_id), text + "\n\n" + render_event(ev),
                                           reply_markup=rsvp_keyboard())

                # напоминание накануне
                if days_left == 1 and not ev["reminded"] and now.hour == 18:
                    ev["reminded"] = True
                    save_data()
                    note = data["house"].get(chat_id)
                    parts = ["🏡 <b>Завтра едем!</b>", render_event(ev), await weather_text()]
                    if note:
                        parts.append(f"🏠 Дом: {note['text']} <i>({note['when']})</i>")
                    await bot.send_message(int(chat_id), "\n\n".join(parts))

                # убрать прошедший выезд
                if days_left < -1:
                    data["events"].pop(chat_id, None)
                    save_data()
        except Exception as e:
            log.warning("Ошибка расписания: %s", e)
        await asyncio.sleep(300)

# ---------------------------------------------------------------- запуск


def get_token() -> str:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        f = BASE_DIR / "token.txt"
        if f.exists():
            token = f.read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit("Не найден токен: положите его в token.txt или задайте BOT_TOKEN.")
    return token


def get_proxy() -> str | None:
    proxy = os.getenv("TG_PROXY", "").strip()
    if not proxy:
        f = BASE_DIR / "proxy.txt"
        if f.exists():
            proxy = f.read_text(encoding="utf-8").strip()
    return proxy or None


async def on_error(event: ErrorEvent) -> None:
    if isinstance(event.exception, TelegramNetworkError):
        log.error("Нет связи с Telegram.")
    else:
        log.exception("Ошибка: %s", event.exception)


async def main() -> None:
    load_data()
    proxy = get_proxy()
    session = AiohttpSession(proxy=proxy) if proxy else None
    bot = Bot(token=get_token(), session=session,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    dp.errors.register(on_error)
    log.info("Дачный бот запущен. Координаты: %s, %s", DACHA_LAT, DACHA_LON)
    asyncio.create_task(scheduler(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
