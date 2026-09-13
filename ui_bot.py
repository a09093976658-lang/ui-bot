"""
Бот сбора УИ (предложений по улучшению) для мессенджера MAX. CYBERSTEEL.

Прототип для запуска с обычного ПК: long polling, без сервера, без HTTPS и домена.
Данные пишутся в SQLite (ui_bot.db), фото скачиваются в папку photos/,
выгрузка для Excel — командой /export.

Запуск:
    py -m pip install maxapi
    py ui_bot.py
"""

from __future__ import annotations

import asyncio
import csv
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from maxapi import Bot, Dispatcher, F
from maxapi.context import MemoryContext, State, StatesGroup
from maxapi.types import (
    BotCommand,
    BotStarted,
    ButtonsPayload,
    CallbackButton,
    Command,
    InputMedia,
    MessageCallback,
    MessageCreated,
)
from maxapi.types.attachments import AttachmentButton

# ---------------------------------------------------------------------------
# НАСТРОЙКИ — правится только этот блок
# ---------------------------------------------------------------------------

# Токен бота из MAX (после регистрации и модерации).
BOT_TOKEN = os.getenv("MAX_UI_BOT_TOKEN", "ВСТАВЬ_ТОКЕН_СЮДА")

# Твой user_id в MAX — на него приходят новые заявки.
# Проще: напиши боту команду «/setadmin cybersteel» — он сам запомнит,
# куда слать заявки, и этот параметр можно не трогать.
ADMIN_USER_ID = int(os.getenv("MAX_UI_ADMIN_ID", "0"))

# Кодовое слово для команды /setadmin. Смени на своё и никому не говори.
ADMIN_SECRET = "cybersteel"

# Статусы заявки. ВРЕМЕННЫЕ — переписать после встречи, где их согласуете.
STATUSES = [
    "Новое",
    "На рассмотрении",
    "Принято к внедрению",
    "Отклонено",
    "Внедрено",
]

# Какие статусы считаются закрытыми (не попадают в «Открытые УИ»).
CLOSED_STATUSES = ["Отклонено", "Внедрено"]

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "ui_bot.db"
PHOTO_DIR = BASE_DIR / "photos"
EXPORT_PATH = BASE_DIR / "ui_export.csv"

# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("ui_bot")

VERSION = "1.4"

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

TOTAL_STEPS = 8


class Form(StatesGroup):
    """Шаги заполнения заявки."""

    fio = State()
    uchastok = State()
    position = State()
    tab_num = State()
    problem = State()
    proposal = State()
    photo_before = State()
    photo_after = State()
    confirm = State()


# ---------------------------------------------------------------------------
# База данных
# ---------------------------------------------------------------------------


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS requests (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                number          TEXT UNIQUE,
                created_at      TEXT,
                author_user_id  INTEGER,
                author_chat_id  INTEGER,
                fio             TEXT,
                uchastok        TEXT,
                position        TEXT,
                tab_num         TEXT,
                problem         TEXT,
                proposal        TEXT,
                photo_before    TEXT,
                photo_after     TEXT,
                status          TEXT,
                status_changed  TEXT
            )
            """
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)"
        )
        # миграция со старой версии (было поле cex, не было position)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(requests)")}
        if "cex" in cols and "uchastok" not in cols:
            conn.execute("ALTER TABLE requests RENAME COLUMN cex TO uchastok")
            log.info("Миграция: поле «цех» переименовано в «участок»")
        if "position" not in cols:
            conn.execute("ALTER TABLE requests ADD COLUMN position TEXT")
            log.info("Миграция: добавлено поле «должность»")


def get_setting(key: str) -> str | None:
    with db_connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def next_number() -> str:
    """Номер вида УИ-2026-0001, нумерация с начала каждого года."""
    year = datetime.now().year
    with db_connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM requests WHERE number LIKE ?", (f"УИ-{year}-%",)
        ).fetchone()
    return f"УИ-{year}-{row['n'] + 1:04d}"


def save_request(data: dict, user_id: int, chat_id: int) -> str:
    number = next_number()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO requests (
                number, created_at, author_user_id, author_chat_id,
                fio, uchastok, position, tab_num, problem, proposal,
                photo_before, photo_after, status, status_changed
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                number, now, user_id, chat_id,
                data.get("fio", ""),
                data.get("uchastok", ""),
                data.get("position", ""),
                data.get("tab_num", ""),
                data.get("problem", ""),
                data.get("proposal", ""),
                data.get("photo_before", ""),
                data.get("photo_after", ""),
                STATUSES[0], now,
            ),
        )
    log.info("Сохранена заявка %s от user_id=%s", number, user_id)
    return number


def get_request(number: str) -> sqlite3.Row | None:
    with db_connect() as conn:
        return conn.execute("SELECT * FROM requests WHERE number = ?", (number,)).fetchone()


def get_user_requests(user_id: int, limit: int = 20) -> list[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute(
            "SELECT number, created_at, status, problem FROM requests "
            "WHERE author_user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


def get_open_requests(limit: int = 10) -> list[sqlite3.Row]:
    """Заявки, которые ещё в работе (статус не из закрытых)."""
    marks = ",".join("?" * len(CLOSED_STATUSES))
    with db_connect() as conn:
        return conn.execute(
            f"SELECT number, created_at, uchastok, status, problem FROM requests "
            f"WHERE status NOT IN ({marks}) ORDER BY id DESC LIMIT ?",
            (*CLOSED_STATUSES, limit),
        ).fetchall()


def count_all() -> int:
    with db_connect() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM requests").fetchone()["n"]


def set_status(number: str, status: str) -> sqlite3.Row | None:
    """Меняет статус. Возвращает заявку, если статус реально изменился."""
    row = get_request(number)
    if row is None or row["status"] == status:
        return None
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with db_connect() as conn:
        conn.execute(
            "UPDATE requests SET status = ?, status_changed = ? WHERE number = ?",
            (status, now, number),
        )
    return get_request(number)


def export_csv() -> Path:
    """Выгрузка для Excel (utf-8-sig, чтобы кириллица открылась без плясок)."""
    with db_connect() as conn:
        rows = conn.execute("SELECT * FROM requests ORDER BY id").fetchall()
    headers = [
        ("number", "Номер"),
        ("created_at", "Дата подачи"),
        ("fio", "ФИО"),
        ("uchastok", "Участок"),
        ("position", "Должность"),
        ("tab_num", "Табельный номер"),
        ("problem", "Проблема"),
        ("proposal", "Предложение"),
        ("photo_before", "Фото «как было»"),
        ("photo_after", "Фото «как станет»"),
        ("status", "Статус"),
        ("status_changed", "Статус изменён"),
    ]
    with open(EXPORT_PATH, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow([title for _, title in headers])
        for row in rows:
            writer.writerow([row[key] if key in row.keys() else "" for key, _ in headers])
    return EXPORT_PATH


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------


def keyboard(*buttons: tuple[str, str]) -> AttachmentButton:
    """Клавиатура из пар (текст, payload) — по одной кнопке в ряд."""
    return AttachmentButton(
        type="inline_keyboard",
        payload=ButtonsPayload(
            buttons=[[CallbackButton(text=t, payload=p)] for t, p in buttons]
        ),
    )


def main_menu(user_id: int | None = None) -> AttachmentButton:
    """Кнопки навигации. Админу добавляются его пункты."""
    buttons = [("Подать УИ", "new_ui"), ("Мои УИ", "my_ui")]
    _, admin_user = admin_target()
    if user_id and admin_user and user_id == admin_user:
        buttons += [("Открытые УИ", "open_ui"), ("Выгрузка в Excel", "export_ui")]
    return keyboard(*buttons)


def user_id_of(event) -> int:
    """user_id отправителя — одинаково для сообщений и нажатий кнопок."""
    if isinstance(event, MessageCallback):
        return event.callback.user.user_id
    user = getattr(event, "from_user", None)
    return getattr(user, "user_id", 0) or 0


def menu_for(event) -> AttachmentButton:
    return main_menu(user_id_of(event))


def skip_menu() -> AttachmentButton:
    return keyboard(("Пропустить", "skip_photo"))


def confirm_menu() -> AttachmentButton:
    return keyboard(("Отправить", "confirm_ui"), ("Отменить", "cancel_ui"))


def status_menu(number: str) -> AttachmentButton:
    """Кнопки смены статуса под заявкой (для админа). Payload: st:<номер>:<индекс>."""
    pairs = [(s, f"st:{number}:{i}") for i, s in enumerate(STATUSES) if i > 0]
    pairs += [("← К открытым", "open_ui"), ("Меню", "menu_ui")]
    return keyboard(*pairs)


async def clear_buttons(event: MessageCallback, text: str | None = None) -> None:
    """Убирает кнопки у сообщения, на котором нажали (чтобы не жали повторно)."""
    try:
        await event.edit(text=text, attachments=[])
    except Exception as e:
        log.debug("Не удалось убрать кнопки: %r", e)


def nav_menu() -> AttachmentButton:
    return keyboard(("← К открытым", "open_ui"), ("Меню", "menu_ui"))


async def update_card(event: MessageCallback, text: str, number: str) -> None:
    """Обновляет карточку заявки у админа, оставляя кнопки статусов."""
    try:
        await event.edit(text=text, attachments=[status_menu(number)])
    except Exception as e:
        log.debug("Не удалось обновить карточку: %r", e)
        await say(event, text, [status_menu(number)])


async def say(event, text: str, attachments=None):
    """Отправка ответа: работает и для обычных сообщений, и для нажатий кнопок."""
    if isinstance(event, MessageCallback):
        return await event.send(text, attachments=attachments)
    return await event.message.answer(text, attachments=attachments)


def photo_url(event: MessageCreated) -> str | None:
    """Достаёт ссылку на первое изображение в сообщении, если оно есть."""
    attachments = getattr(event.message.body, "attachments", None) or []
    for att in attachments:
        att_type = getattr(att, "type", None)
        att_type = getattr(att_type, "value", att_type)
        if att_type == "image":
            payload = getattr(att, "payload", None)
            url = getattr(payload, "url", None)
            if url:
                return url
    return None


async def save_photo(url: str, hint: str) -> str:
    """Скачивает фото рядом с ботом. Если не вышло — оставляем ссылку."""
    PHOTO_DIR.mkdir(exist_ok=True)
    try:
        return str(await bot.download_file(url, PHOTO_DIR, filename=f"{hint}.jpg"))
    except Exception as e:  # ссылки MAX живут не вечно, но заявку терять нельзя
        log.warning("Не удалось скачать фото: %s", e)
        return url


def format_request(data, number: str | None = None, status: str | None = None) -> str:
    def get(key):
        try:
            return data[key] or ""
        except (KeyError, IndexError):
            return ""

    lines = []
    if number:
        lines.append(f"Заявка {number}")
    if status:
        lines.append(f"Статус: {status}")
    lines += [
        f"ФИО: {get('fio')}",
        f"Участок: {get('uchastok')}",
        f"Должность: {get('position')}",
        f"Табельный номер: {get('tab_num')}",
        "",
        f"Проблема: {get('problem')}",
        f"Предложение: {get('proposal')}",
    ]
    if get("photo_before"):
        lines.append(f"Фото «как было»: {get('photo_before')}")
    if get("photo_after"):
        lines.append(f"Фото «как станет»: {get('photo_after')}")
    return "\n".join(lines)


def admin_target() -> tuple[int | None, int | None]:
    """Куда слать заявки: (chat_id, user_id). Приоритет — то, что задано /setadmin."""
    chat_id = get_setting("admin_chat_id")
    user_id = get_setting("admin_user_id") or (ADMIN_USER_ID or None)
    return (int(chat_id) if chat_id else None, int(user_id) if user_id else None)


def is_admin(event) -> bool:
    _, admin_user = admin_target()
    if not admin_user:
        return False
    user = getattr(event, "from_user", None)
    user_id = getattr(user, "user_id", None)
    if user_id is None and isinstance(event, MessageCallback):
        user_id = event.callback.user.user_id
    return user_id == admin_user


async def notify_admin(text: str, attachments=None) -> None:
    chat_id, user_id = admin_target()
    if not chat_id and not user_id:
        log.warning("Некому слать заявку: напиши боту «/setadmin %s»", ADMIN_SECRET)
        return
    try:
        if chat_id:
            await bot.send_message(chat_id=chat_id, text=text, attachments=attachments)
        else:
            await bot.send_message(user_id=user_id, text=text, attachments=attachments)
        log.info("Уведомление админу отправлено (chat_id=%s, user_id=%s)", chat_id, user_id)
    except Exception as e:
        log.error("НЕ УДАЛОСЬ отправить уведомление админу: %r", e)


# ---------------------------------------------------------------------------
# Старт и служебные команды
# ---------------------------------------------------------------------------

GREETING = (
    "За принятое предложение — выплата 500 ₽.\n\n"
    "Опиши проблему и своё решение, приложи фото — заявка получит номер, "
    "а о смене статуса сообщу сюда же.\n\n"
    "Выбери действие:"
)


@dp.bot_started()
async def on_bot_started(event: BotStarted):
    user_id = getattr(getattr(event, "user", None), "user_id", None)
    await event.bot.send_message(
        chat_id=event.chat_id, text=GREETING, attachments=[main_menu(user_id)]
    )


@dp.message_created(Command("start"))
async def cmd_start(event: MessageCreated, context: MemoryContext):
    await context.clear()
    await say(event, GREETING, [menu_for(event)])


@dp.message_created(Command("menu"))
async def cmd_menu(event: MessageCreated, context: MemoryContext):
    await context.clear()
    await say(event, "Главное меню:", [menu_for(event)])


@dp.message_created(Command("whoami"))
async def cmd_whoami(event: MessageCreated):
    user_id = event.from_user.user_id if event.from_user else "?"
    chat_id = event.chat.chat_id if event.chat else "?"
    await say(event, f"user_id: {user_id}\nchat_id: {chat_id}")


@dp.message_created(Command("setadmin"))
async def cmd_setadmin(event: MessageCreated):
    """/setadmin <кодовое слово> — привязать этот чат как получателя заявок."""
    parts = (event.message.body.text or "").split(maxsplit=1)
    if len(parts) < 2 or parts[1].strip() != ADMIN_SECRET:
        await say(event, "Неверное кодовое слово.")
        return
    chat_id = event.chat.chat_id if event.chat else None
    user_id = event.from_user.user_id if event.from_user else None
    if chat_id:
        set_setting("admin_chat_id", chat_id)
    if user_id:
        set_setting("admin_user_id", user_id)
    log.info("Админ назначен: chat_id=%s user_id=%s", chat_id, user_id)
    await say(event, f"Готово. Заявки будут приходить сюда.\nchat_id: {chat_id}\nuser_id: {user_id}")


@dp.message_created(Command("cancel"))
async def cmd_cancel(event: MessageCreated, context: MemoryContext):
    await context.clear()
    await say(event, "Заполнение отменено.", [menu_for(event)])


# ---------------------------------------------------------------------------
# Команды для админа
# ---------------------------------------------------------------------------


@dp.message_created(Command("list"))
async def cmd_list(event: MessageCreated):
    if not is_admin(event):
        return
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT number, created_at, uchastok, status FROM requests "
            "ORDER BY id DESC LIMIT 15"
        ).fetchall()
    if not rows:
        await say(event, "Заявок пока нет.")
        return
    text = "\n".join(
        f"{r['number']} | {r['created_at']} | {r['uchastok']} | {r['status']}" for r in rows
    )
    await say(event, "Последние заявки:\n\n" + text)


@dp.message_created(Command("card"))
async def cmd_card(event: MessageCreated):
    """/card УИ-2026-0001 — показать заявку целиком."""
    if not is_admin(event):
        return
    parts = (event.message.body.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await say(event, "Формат: /card УИ-2026-0001")
        return
    row = get_request(parts[1].strip())
    if row is None:
        await say(event, "Заявка не найдена.")
        return
    await say(
        event,
        format_request(row, row["number"], row["status"]),
        [status_menu(row["number"])],
    )


@dp.message_created(Command("status"))
async def cmd_status(event: MessageCreated):
    """/status УИ-2026-0001 Принято к внедрению"""
    if not is_admin(event):
        return
    parts = (event.message.body.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await say(
            event,
            "Формат: /status УИ-2026-0001 <статус>\n\nСтатусы:\n"
            + "\n".join(f"— {s}" for s in STATUSES),
        )
        return

    number, status = parts[1].strip(), parts[2].strip()
    if status not in STATUSES:
        await say(event, "Такого статуса нет. Доступные:\n" + "\n".join(f"— {s}" for s in STATUSES))
        return

    row = set_status(number, status)
    if row is None:
        await say(event, "Заявка не найдена или статус тот же — ничего не менял.")
        return

    await say(event, f"{number}: статус изменён на «{status}».")

    if row["author_user_id"]:  # автору пишем только при реальной смене статуса
        try:
            await bot.send_message(
                user_id=row["author_user_id"],
                text=f"Заявка {number}: статус изменён на «{status}».",
            )
        except Exception as e:
            log.error("Не удалось уведомить автора %s: %r", row["author_user_id"], e)


@dp.message_created(Command("export"))
async def cmd_export(event: MessageCreated):
    if not is_admin(event):
        return
    await send_export(event)


@dp.message_created(Command("version"))
async def cmd_version(event: MessageCreated):
    await say(event, f"Версия бота: {VERSION}")


# ---------------------------------------------------------------------------
# Открытые заявки и карточка (для админа)
# ---------------------------------------------------------------------------


async def show_open(event) -> None:
    rows = get_open_requests()
    if not rows:
        await say(event, "Открытых заявок нет.", [menu_for(event)])
        return
    lines, buttons = [], []
    for r in rows:
        problem = (r["problem"] or "")[:45]
        lines.append(f"{r['number']} · {r['status']}\n{r['uchastok']} — {problem}")
        buttons.append((f"{r['number']} · {r['status']}", f"card:{r['number']}"))
    buttons.append(("Выгрузка в Excel", "export_ui"))
    buttons.append(("← Меню", "menu_ui"))
    await say(
        event,
        f"Открытые заявки ({len(rows)}). Нажми на номер, чтобы открыть карточку:\n\n"
        + "\n\n".join(lines),
        [keyboard(*buttons)],
    )


@dp.message_created(Command("open"))
async def cmd_open(event: MessageCreated):
    if not is_admin(event):
        return
    await show_open(event)


@dp.message_callback(F.callback.payload == "open_ui")
async def cb_open(event: MessageCallback):
    if not is_admin(event):
        return
    await clear_buttons(event, "Открытые УИ")
    await show_open(event)


@dp.message_callback(F.callback.payload.startswith("card:"))
async def cb_card(event: MessageCallback):
    if not is_admin(event):
        return
    number = event.callback.payload.split(":", 1)[1]
    await clear_buttons(event, f"Открыта заявка {number}")
    row = get_request(number)
    if row is None:
        await say(event, "Заявка не найдена.", [menu_for(event)])
        return
    await say(event, format_request(row, row["number"], row["status"]), [status_menu(number)])


async def send_export(event) -> None:
    """Выгрузка всех заявок, включая закрытые — файлом в чат."""
    path = export_csv()
    total = count_all()
    try:
        await say(event, f"Выгрузка: {total} заявок, включая закрытые.",
                  [InputMedia(str(path), type="file")])
        await say(event, "Что дальше?", [menu_for(event)])
    except Exception as e:
        log.warning("Не удалось отправить файл в чат: %r", e)
        await say(event, f"Выгрузка ({total} заявок) сохранена на компьютере:\n{path}",
                  [menu_for(event)])


@dp.message_callback(F.callback.payload == "export_ui")
async def cb_export(event: MessageCallback):
    if not is_admin(event):
        return
    await clear_buttons(event, "Выгрузка в Excel")
    await send_export(event)


@dp.message_callback(F.callback.payload == "menu_ui")
async def cb_menu(event: MessageCallback, context: MemoryContext):
    await context.clear()
    await clear_buttons(event, "Меню")
    await say(event, "Главное меню:", [menu_for(event)])


# ---------------------------------------------------------------------------
# Мои заявки
# ---------------------------------------------------------------------------


async def show_my(event, user_id: int) -> None:
    rows = get_user_requests(user_id)
    if not rows:
        await say(event, "У тебя пока нет поданных УИ.", [menu_for(event)])
        return
    lines = []
    for r in rows:
        problem = (r["problem"] or "")[:40]
        lines.append(f"{r['number']} — {r['status']}\n{r['created_at']} · {problem}…")
    await say(event, "Твои заявки:\n\n" + "\n\n".join(lines), [menu_for(event)])


@dp.message_created(Command("my"))
async def cmd_my(event: MessageCreated):
    await show_my(event, event.from_user.user_id if event.from_user else 0)


@dp.message_callback(F.callback.payload == "my_ui")
async def cb_my(event: MessageCallback):
    await clear_buttons(event, "Мои УИ")
    await show_my(event, event.callback.user.user_id)


# ---------------------------------------------------------------------------
# Заполнение заявки
# ---------------------------------------------------------------------------


async def start_form(event, context: MemoryContext) -> None:
    await context.clear()
    await context.set_state(Form.fio)
    await say(event, f"Заполним заявку.\n\nШаг 1 из {TOTAL_STEPS}. Твои ФИО полностью:")


@dp.message_created(Command("new"))
async def cmd_new(event: MessageCreated, context: MemoryContext):
    await start_form(event, context)


@dp.message_callback(F.callback.payload == "new_ui")
async def cb_new(event: MessageCallback, context: MemoryContext):
    await clear_buttons(event, "Подача УИ")
    await start_form(event, context)


@dp.message_created(F.message.body.text, Form.fio)
async def step_fio(event: MessageCreated, context: MemoryContext):
    await context.update_data(fio=event.message.body.text.strip())
    await context.set_state(Form.uchastok)
    await say(event, f"Шаг 2 из {TOTAL_STEPS}. Участок:")


@dp.message_created(F.message.body.text, Form.uchastok)
async def step_uchastok(event: MessageCreated, context: MemoryContext):
    await context.update_data(uchastok=event.message.body.text.strip())
    await context.set_state(Form.position)
    await say(event, f"Шаг 3 из {TOTAL_STEPS}. Должность:")


@dp.message_created(F.message.body.text, Form.position)
async def step_position(event: MessageCreated, context: MemoryContext):
    await context.update_data(position=event.message.body.text.strip())
    await context.set_state(Form.tab_num)
    await say(event, f"Шаг 4 из {TOTAL_STEPS}. Табельный номер:")


@dp.message_created(F.message.body.text, Form.tab_num)
async def step_tab_num(event: MessageCreated, context: MemoryContext):
    await context.update_data(tab_num=event.message.body.text.strip())
    await context.set_state(Form.problem)
    await say(event, f"Шаг 5 из {TOTAL_STEPS}. Опиши проблему — что сейчас не так:")


@dp.message_created(F.message.body.text, Form.problem)
async def step_problem(event: MessageCreated, context: MemoryContext):
    await context.update_data(problem=event.message.body.text.strip())
    await context.set_state(Form.proposal)
    await say(event, f"Шаг 6 из {TOTAL_STEPS}. Что предлагаешь сделать:")


@dp.message_created(F.message.body.text, Form.proposal)
async def step_proposal(event: MessageCreated, context: MemoryContext):
    await context.update_data(proposal=event.message.body.text.strip())
    await context.set_state(Form.photo_before)
    await say(
        event,
        f"Шаг 7 из {TOTAL_STEPS}. Пришли фото «как было» — или нажми «Пропустить».",
        [skip_menu()],
    )


async def ask_photo_after(event, context: MemoryContext) -> None:
    await context.set_state(Form.photo_after)
    await say(
        event,
        f"Шаг 8 из {TOTAL_STEPS}. Пришли фото или эскиз «как станет» — или «Пропустить».",
        [skip_menu()],
    )


@dp.message_created(Form.photo_before)
async def step_photo_before(event: MessageCreated, context: MemoryContext):
    url = photo_url(event)
    if url:
        data = await context.get_data()
        hint = f"before_{data.get('tab_num', 'x')}_{datetime.now():%Y%m%d%H%M%S}"
        await context.update_data(photo_before=await save_photo(url, hint))
    await ask_photo_after(event, context)


@dp.message_created(Form.photo_after)
async def step_photo_after(event: MessageCreated, context: MemoryContext):
    url = photo_url(event)
    if url:
        data = await context.get_data()
        hint = f"after_{data.get('tab_num', 'x')}_{datetime.now():%Y%m%d%H%M%S}"
        await context.update_data(photo_after=await save_photo(url, hint))
    await show_preview(event, context)


@dp.message_callback(F.callback.payload == "skip_photo")
async def cb_skip_photo(event: MessageCallback, context: MemoryContext):
    state = await context.get_state()
    await clear_buttons(event, "Фото пропущено.")
    if state == Form.photo_before:
        await ask_photo_after(event, context)
    else:
        await show_preview(event, context)


async def show_preview(event, context: MemoryContext) -> None:
    data = await context.get_data()
    await context.set_state(Form.confirm)
    await say(
        event,
        "Проверь заявку:\n\n" + format_request(data) + "\n\nОтправляем?",
        [confirm_menu()],
    )


async def submit(event, context: MemoryContext, user_id: int, chat_id: int) -> None:
    data = await context.get_data()
    if not data:
        await say(event, "Данные заявки потерялись — начнём заново.", [menu_for(event)])
        await context.clear()
        return
    number = save_request(data, user_id, chat_id)
    await context.clear()
    await say(
        event,
        f"Заявка принята, номер {number}.\nО смене статуса сообщу сюда же.",
        [menu_for(event)],
    )
    await notify_admin(
        "Новая заявка УИ\n\n" + format_request(data, number, STATUSES[0]),
        [status_menu(number)],
    )


@dp.message_callback(F.callback.payload == "confirm_ui")
async def cb_confirm(event: MessageCallback, context: MemoryContext):
    chat_id = event.chat.chat_id if event.chat else 0
    await clear_buttons(event, "Заявка отправлена.")
    await submit(event, context, event.callback.user.user_id, chat_id)


@dp.message_callback(F.callback.payload.startswith("st:"))
async def cb_set_status(event: MessageCallback):
    """Смена статуса кнопкой под заявкой (только админ)."""
    if not is_admin(event):
        return
    try:
        _, number, idx = event.callback.payload.split(":")
        status = STATUSES[int(idx)]
    except (ValueError, IndexError):
        log.warning("Кривой payload статуса: %r", event.callback.payload)
        return

    row = set_status(number, status)
    if row is None:
        await clear_buttons(event, f"{number}: статус уже «{status}».")
        await say(event, "Что дальше?", [nav_menu()])
        return

    await clear_buttons(event, f"{number} → «{status}»")
    await say(
        event,
        f"Статус заявки {number}: «{status}». Автору отправлено уведомление.",
        [nav_menu()],
    )

    if row["author_user_id"]:
        try:
            await bot.send_message(
                user_id=row["author_user_id"],
                text=f"Заявка {number}: статус изменён на «{status}».",
            )
            log.info("Автор %s уведомлён о статусе %s", row["author_user_id"], status)
        except Exception as e:
            log.error("Не удалось уведомить автора %s: %r", row["author_user_id"], e)


@dp.message_created(Form.confirm)
async def step_confirm_text(event: MessageCreated, context: MemoryContext):
    """Подтверждение текстом — на случай, если кнопка не нажалась."""
    text = (event.message.body.text or "").strip().lower()
    if text in {"да", "отправить", "+", "/send", "ок", "ok"}:
        user_id = event.from_user.user_id if event.from_user else 0
        chat_id = event.chat.chat_id if event.chat else 0
        await submit(event, context, user_id, chat_id)
    elif text in {"нет", "отмена", "отменить", "-"}:
        await context.clear()
        await say(event, "Заявка отменена.", [menu_for(event)])
    else:
        await say(event, "Нажми «Отправить» или напиши «да».", [confirm_menu()])


@dp.message_callback(F.callback.payload == "cancel_ui")
async def cb_cancel(event: MessageCallback, context: MemoryContext):
    await context.clear()
    await clear_buttons(event, "Заявка отменена.")
    await say(event, "Заявка отменена.", [menu_for(event)])


# ---------------------------------------------------------------------------
# Диагностика: всё, что не попало в обработчики выше
# ---------------------------------------------------------------------------


@dp.message_callback()
async def cb_unknown(event: MessageCallback):
    log.warning("Неизвестная кнопка: payload=%r", event.callback.payload)
    await say(event, "Не понял кнопку. Открываю меню.", [menu_for(event)])


@dp.message_created()
async def msg_unknown(event: MessageCreated, context: MemoryContext):
    state = await context.get_state()
    log.info("Сообщение вне сценария: state=%s text=%r", state, event.message.body.text)
    await say(event, "Выбери действие:", [menu_for(event)])


# ---------------------------------------------------------------------------


@dp.on_started()
async def on_started():
    log.info("Бот УИ запущен, версия %s", VERSION)
    chat_id, user_id = admin_target()
    if chat_id or user_id:
        log.info("Заявки уходят админу: chat_id=%s user_id=%s", chat_id, user_id)
    else:
        log.warning("АДМИН НЕ ЗАДАН! Напиши боту: /setadmin %s", ADMIN_SECRET)
    try:
        await bot.set_my_commands(
            BotCommand(name="new", description="Подать УИ"),
            BotCommand(name="my", description="Мои УИ"),
            BotCommand(name="menu", description="Главное меню"),
            BotCommand(name="open", description="Открытые УИ (админ)"),
            BotCommand(name="export", description="Выгрузка в Excel (админ)"),
            BotCommand(name="cancel", description="Отменить заполнение"),
        )
    except Exception as e:
        log.warning("Не удалось задать список команд: %r", e)


async def main() -> None:
    init_db()
    log.info("База: %s", DB_PATH)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
