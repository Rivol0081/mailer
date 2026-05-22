"""Telegram bot (aiogram 3) — UI for the mailer."""
from __future__ import annotations

import asyncio
import logging
from html import escape as h

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    TelegramObject,
)

from . import autoconfig, filters as filt, imap_client, sieve_client
from .config import load_settings
from .crypto import CredCipher
from .filters import FilterDaemon
from .storage import Account, Storage


log = logging.getLogger(__name__)
router = Router()


# ---------- Access control ----------

class AllowlistMiddleware(BaseMiddleware):
    """Block updates from non-whitelisted users.

    Admins listed in ADMIN_IDS env var are always allowed and are added to the
    DB allowlist on first contact. If the allowlist is empty, the FIRST user to
    /start gets bootstrapped as an admin (handy for fresh deployments).
    """

    def __init__(self, storage: Storage, admin_ids: frozenset[int]):
        self.storage = storage
        self.admin_ids = admin_ids

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        uid = user.id

        if uid in self.admin_ids:
            if not await self.storage.is_allowed(uid):
                await self.storage.allow_user(uid, is_admin=True, note=user.username or user.full_name)
            return await handler(event, data)

        if await self.storage.is_allowed(uid):
            return await handler(event, data)

        # Bootstrap: empty allowlist + no configured admins → first /start becomes admin.
        if not self.admin_ids and await self.storage.count_allowed() == 0:
            text = getattr(event, "text", None) if isinstance(event, Message) else None
            if isinstance(event, Message) and text and text.startswith("/start"):
                await self.storage.allow_user(uid, is_admin=True, note=user.username or user.full_name)
                return await handler(event, data)

        # Reject politely.
        if isinstance(event, Message):
            await event.answer(
                f"⛔ Доступ закрыт. Твой Telegram ID: <code>{uid}</code>\n"
                "Попроси администратора добавить тебя командой /allow."
            )
        elif isinstance(event, CallbackQuery):
            await event.answer("Доступ закрыт", show_alert=True)
        return None


# ---------- FSM ----------

class AddAccount(StatesGroup):
    waiting_email = State()
    waiting_password = State()
    waiting_label = State()


class KeywordInput(StatesGroup):
    waiting_keyword = State()


# ---------- Keyboards ----------

def main_menu_kb(has_active: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="➕ Добавить ящик"), KeyboardButton(text="📂 Мои ящики")],
    ]
    if has_active:
        rows.append([KeyboardButton(text="🎛 Активный ящик"), KeyboardButton(text="📨 Письма")])
        rows.append([KeyboardButton(text="🔌 Проверить доступность")])
    rows.append([KeyboardButton(text="ℹ️ Помощь")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def accounts_kb(accounts: list[Account], active_id: int | None) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for acc in accounts:
        mark = "✅ " if active_id == acc.id else ""
        flt = " 🟢" if acc.filter_on else ""
        buttons.append([
            InlineKeyboardButton(
                text=f"{mark}{acc.label} ({acc.email}){flt}",
                callback_data=f"select:{acc.id}",
            ),
            InlineKeyboardButton(text="🗑", callback_data=f"forget:{acc.id}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def account_actions_kb(acc: Account) -> InlineKeyboardMarkup:
    toggle = "🔴 Выключить фильтр" if acc.filter_on else "🟢 Включить фильтр"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle, callback_data=f"toggle:{acc.id}")],
        [InlineKeyboardButton(text="➕ Ключевое слово", callback_data=f"kwadd:{acc.id}")],
        [InlineKeyboardButton(text="📋 Список слов", callback_data=f"kwlist:{acc.id}")],
        [InlineKeyboardButton(text=f"⚙️ Действие: {acc.filter_action}", callback_data=f"action:{acc.id}")],
        [InlineKeyboardButton(text="📁 Папки", callback_data=f"folders:{acc.id}")],
        [InlineKeyboardButton(text="📨 Последние письма", callback_data=f"mails:{acc.id}:INBOX")],
        [InlineKeyboardButton(text="🔌 Проверить доступность", callback_data=f"ping:{acc.id}")],
        [InlineKeyboardButton(text="🧹 Очистить всю почту", callback_data=f"wipe:{acc.id}")],
        [InlineKeyboardButton(text="🔑 Сменить пароль", callback_data=f"chpwd:{acc.id}")],
        [InlineKeyboardButton(text="🗑 Удалить ящик у провайдера", callback_data=f"nuke:{acc.id}")],
        [InlineKeyboardButton(text="❌ Забыть в боте", callback_data=f"forget:{acc.id}")],
    ])


def confirm_kb(action: str, account_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, я уверен", callback_data=f"{action}!:{account_id}"),
        InlineKeyboardButton(text="↩️ Отмена", callback_data="cancel"),
    ]])


# ---------- Handlers ----------

HELP = (
    "<b>Mailer-бот</b>\n\n"
    "Подключение IMAP-ящиков, авто-определение серверов, фильтры по ключевым словам "
    "(server-side Sieve или клиентский цикл).\n\n"
    "<b>Что бот может</b>:\n"
    "• Добавить несколько ящиков (вкладки)\n"
    "• Просматривать папки и последние письма\n"
    "• Проверять доступность IMAP\n"
    "• Включать/выключать фильтр удаления входящих по словам\n"
    "• Действия фильтра: <code>trash</code> (в корзину) / <code>delete</code> (без следа) / <code>flag</code> (пометить)\n"
    "• Полная очистка всех писем во всех папках\n\n"
    "<b>Чего IMAP не умеет</b>:\n"
    "• Удалить сам аккаунт у провайдера — только через сайт провайдера\n"
    "• Сменить пароль — тоже только через веб\n\n"
    "<b>Команды</b>:\n"
    "/whoami — мой ID, /check — проверить все ящики\n"
    "<b>Админ</b>: /allow &lt;id&gt;, /admin &lt;id&gt;, /revoke &lt;id&gt;, /allowed\n\n"
    "Пароли в БД хранятся зашифрованными (Fernet)."
)


@router.message(CommandStart())
async def start(msg: Message, storage: Storage):
    active = await storage.get_active(msg.from_user.id)
    await msg.answer(
        "Привет! " + HELP,
        reply_markup=main_menu_kb(has_active=active is not None),
    )


@router.message(Command("help"))
@router.message(F.text == "ℹ️ Помощь")
async def help_cmd(msg: Message, storage: Storage):
    active = await storage.get_active(msg.from_user.id)
    await msg.answer(HELP, reply_markup=main_menu_kb(has_active=active is not None))


# ----- Add account flow -----

@router.message(F.text == "➕ Добавить ящик")
@router.message(Command("add"))
async def add_start(msg: Message, state: FSMContext):
    await state.set_state(AddAccount.waiting_email)
    await msg.answer(
        "Введи email ящика:",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(AddAccount.waiting_email)
async def add_email(msg: Message, state: FSMContext):
    email = (msg.text or "").strip().lower()
    if "@" not in email or " " in email:
        await msg.answer("Не похоже на email. Попробуй ещё раз.")
        return
    await state.update_data(email=email)
    await state.set_state(AddAccount.waiting_password)
    await msg.answer(
        "Теперь пароль для IMAP (для Gmail/Outlook — application-specific password).\n"
        "Сообщение с паролем удалю сразу после получения.",
    )


@router.message(AddAccount.waiting_password)
async def add_password(msg: Message, state: FSMContext, storage: Storage, cipher: CredCipher):
    password = msg.text or ""
    # Best-effort: delete the user's password message from chat history.
    try:
        await msg.delete()
    except Exception:
        pass

    data = await state.get_data()
    email = data["email"]

    notice = await msg.answer(f"Ищу IMAP-сервер для <code>{h(email)}</code>…")
    info = await autoconfig.discover(email)
    if not info:
        await notice.edit_text(
            f"Не нашёл IMAP-сервер для домена <code>{h(autoconfig.domain_of(email))}</code>. "
            "Попробуй другой email или задай сервер вручную (этого пока нет в боте — напиши /cancel)."
        )
        return

    await notice.edit_text(
        f"Сервер: <code>{h(info.imap_host)}:{info.imap_port}</code>. Проверяю логин…"
    )
    ok = await imap_client.check_credentials(
        info.imap_host, info.imap_port, info.imap_ssl, email, password
    )
    if not ok:
        await notice.edit_text(
            "IMAP-логин не прошёл. Проверь пароль (для Gmail/Outlook нужен app-password) и попробуй снова через ➕."
        )
        await state.clear()
        return

    sieve_host = info.sieve_host
    if sieve_host:
        try:
            if not await sieve_client.probe(sieve_host, info.sieve_port, email, password):
                sieve_host = None
        except Exception:
            sieve_host = None

    await state.update_data(
        password=password,
        imap_host=info.imap_host,
        imap_port=info.imap_port,
        imap_ssl=info.imap_ssl,
        sieve_host=sieve_host,
        sieve_port=info.sieve_port,
    )
    await state.set_state(AddAccount.waiting_label)
    sieve_state = "доступен ✅" if sieve_host else "недоступен — буду фильтровать клиентом"
    await notice.edit_text(
        f"Логин ок. ManageSieve: {sieve_state}.\n"
        f"Придумай короткую подпись для вкладки (например, «Личная» или «Работа»):"
    )


@router.message(AddAccount.waiting_label)
async def add_label(msg: Message, state: FSMContext, storage: Storage, cipher: CredCipher):
    label = (msg.text or "").strip() or "Ящик"
    data = await state.get_data()
    enc = cipher.encrypt(data["password"])
    acc_id = await storage.add_account(
        tg_user_id=msg.from_user.id,
        label=label[:40],
        email=data["email"],
        password_enc=enc,
        imap_host=data["imap_host"],
        imap_port=data["imap_port"],
        imap_ssl=data["imap_ssl"],
        sieve_host=data.get("sieve_host"),
        sieve_port=data.get("sieve_port", 4190),
    )
    await storage.set_active(msg.from_user.id, acc_id)
    await state.clear()
    await msg.answer(
        f"Готово: <b>{h(label)}</b> ({h(data['email'])}) — активная вкладка.",
        reply_markup=main_menu_kb(has_active=True),
    )


@router.message(Command("cancel"))
async def cancel(msg: Message, state: FSMContext, storage: Storage):
    await state.clear()
    active = await storage.get_active(msg.from_user.id)
    await msg.answer("Окей, отменил.", reply_markup=main_menu_kb(has_active=active is not None))


# ----- Accounts list / tabs -----

@router.message(F.text == "📂 Мои ящики")
@router.message(Command("accounts"))
async def list_accounts(msg: Message, storage: Storage):
    accounts = await storage.list_accounts(msg.from_user.id)
    if not accounts:
        await msg.answer("Пока нет ящиков. Нажми ➕ Добавить ящик.")
        return
    active = await storage.get_active(msg.from_user.id)
    active_id = active.id if active else None
    await msg.answer(
        "Твои ящики (нажми, чтобы сделать активным; 🗑 — забыть в боте):",
        reply_markup=accounts_kb(accounts, active_id),
    )


@router.callback_query(F.data.startswith("select:"))
async def cb_select(call: CallbackQuery, storage: Storage):
    acc_id = int(call.data.split(":", 1)[1])
    acc = await storage.get_account(acc_id)
    if not acc or acc.tg_user_id != call.from_user.id:
        await call.answer("Не найден", show_alert=True)
        return
    await storage.set_active(call.from_user.id, acc_id)
    await call.answer(f"Активный: {acc.label}")
    await call.message.edit_reply_markup(
        reply_markup=accounts_kb(await storage.list_accounts(call.from_user.id), acc_id)
    )


@router.callback_query(F.data.startswith("forget:"))
async def cb_forget(call: CallbackQuery, storage: Storage, cipher: CredCipher):
    acc_id = int(call.data.split(":", 1)[1])
    acc = await storage.get_account(acc_id)
    if not acc or acc.tg_user_id != call.from_user.id:
        await call.answer("Не найден", show_alert=True)
        return
    await filt.remove_sieve_safe(acc, cipher)
    await storage.delete_account(acc_id)
    await call.answer("Ящик забыт")
    accounts = await storage.list_accounts(call.from_user.id)
    active = await storage.get_active(call.from_user.id)
    active_id = active.id if active else None
    if accounts:
        await call.message.edit_reply_markup(reply_markup=accounts_kb(accounts, active_id))
    else:
        await call.message.edit_text("Все ящики удалены из бота.")


# ----- Active account view -----

@router.message(F.text == "🎛 Активный ящик")
@router.message(F.text == "🧪 Фильтры")
async def active_account(msg: Message, storage: Storage):
    acc = await storage.get_active(msg.from_user.id)
    if not acc:
        await msg.answer("Нет активного ящика. Выбери в 📂 Мои ящики.")
        return
    keywords = await storage.list_keywords(acc.id)
    flt_state = "🟢 включён" if acc.filter_on else "🔴 выключен"
    sieve_info = "Sieve" if acc.sieve_host else "клиентский цикл"
    text = (
        f"<b>{h(acc.label)}</b> — <code>{h(acc.email)}</code>\n"
        f"IMAP: <code>{h(acc.imap_host)}:{acc.imap_port}</code>\n"
        f"Режим фильтра: {sieve_info}\n"
        f"Фильтр: {flt_state} (действие: <code>{acc.filter_action}</code>)\n"
        f"Ключевых слов: <b>{len(keywords)}</b>"
    )
    if keywords:
        text += "\n\n• " + "\n• ".join(h(k) for k in keywords[:30])
        if len(keywords) > 30:
            text += f"\n…и ещё {len(keywords) - 30}"
    await msg.answer(text, reply_markup=account_actions_kb(acc))


# ----- Filter ops -----

@router.callback_query(F.data.startswith("toggle:"))
async def cb_toggle(call: CallbackQuery, storage: Storage, cipher: CredCipher):
    acc_id = int(call.data.split(":", 1)[1])
    acc = await storage.get_account(acc_id)
    if not acc or acc.tg_user_id != call.from_user.id:
        await call.answer("Не найден", show_alert=True)
        return
    new_state = not acc.filter_on
    keywords = await storage.list_keywords(acc.id)
    if new_state and not keywords:
        await call.answer("Сначала добавь хоть одно ключевое слово.", show_alert=True)
        return
    if new_state:
        mode, err = await filt.install_or_skip_sieve(acc, cipher, keywords)
        await storage.set_filter_state(acc.id, True)
        await call.answer(
            f"Фильтр включён ({mode})" + (f". Sieve: {err[:50]}" if err else "")
        )
    else:
        await filt.remove_sieve_safe(acc, cipher)
        await storage.set_filter_state(acc.id, False)
        await call.answer("Фильтр выключен")
    acc = await storage.get_account(acc_id)
    await call.message.edit_reply_markup(reply_markup=account_actions_kb(acc))


@router.callback_query(F.data.startswith("action:"))
async def cb_action(call: CallbackQuery, storage: Storage, cipher: CredCipher):
    acc_id = int(call.data.split(":", 1)[1])
    acc = await storage.get_account(acc_id)
    if not acc or acc.tg_user_id != call.from_user.id:
        await call.answer("Не найден", show_alert=True)
        return
    cycle = {"trash": "delete", "delete": "flag", "flag": "trash"}
    new_action = cycle[acc.filter_action]
    await storage.set_filter_state(acc.id, acc.filter_on, new_action)
    # If filter was on, reinstall the Sieve script with the new action.
    if acc.filter_on:
        keywords = await storage.list_keywords(acc.id)
        acc = await storage.get_account(acc_id)
        await filt.install_or_skip_sieve(acc, cipher, keywords)
    acc = await storage.get_account(acc_id)
    await call.answer(f"Действие: {new_action}")
    await call.message.edit_reply_markup(reply_markup=account_actions_kb(acc))


@router.callback_query(F.data.startswith("kwadd:"))
async def cb_kwadd(call: CallbackQuery, state: FSMContext):
    acc_id = int(call.data.split(":", 1)[1])
    await state.set_state(KeywordInput.waiting_keyword)
    await state.update_data(account_id=acc_id)
    await call.message.answer(
        "Введи ключевое слово (одно сообщение = одно слово/фраза). /cancel — отмена."
    )
    await call.answer()


@router.message(KeywordInput.waiting_keyword)
async def kw_input(msg: Message, state: FSMContext, storage: Storage, cipher: CredCipher):
    data = await state.get_data()
    acc_id = int(data["account_id"])
    kw = (msg.text or "").strip()
    if not kw:
        await msg.answer("Пустое слово, попробуй ещё.")
        return
    await storage.add_keyword(acc_id, kw)
    await state.clear()
    acc = await storage.get_account(acc_id)
    if acc and acc.filter_on:
        keywords = await storage.list_keywords(acc.id)
        await filt.install_or_skip_sieve(acc, cipher, keywords)
    await msg.answer(
        f"Добавил: <code>{h(kw)}</code>",
        reply_markup=main_menu_kb(has_active=True),
    )


@router.callback_query(F.data.startswith("kwlist:"))
async def cb_kwlist(call: CallbackQuery, storage: Storage):
    acc_id = int(call.data.split(":", 1)[1])
    acc = await storage.get_account(acc_id)
    if not acc or acc.tg_user_id != call.from_user.id:
        await call.answer("Не найден", show_alert=True)
        return
    keywords = await storage.list_keywords(acc.id)
    if not keywords:
        await call.message.answer("Ключевых слов нет.")
        await call.answer()
        return
    rows = [[InlineKeyboardButton(text=f"❌ {kw}", callback_data=f"kwrm:{acc.id}:{i}")] for i, kw in enumerate(keywords[:30])]
    await call.message.answer(
        "Нажми на слово, чтобы удалить:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await call.answer()


@router.callback_query(F.data.startswith("kwrm:"))
async def cb_kwrm(call: CallbackQuery, storage: Storage, cipher: CredCipher):
    _, acc_id_s, idx_s = call.data.split(":")
    acc_id = int(acc_id_s)
    idx = int(idx_s)
    acc = await storage.get_account(acc_id)
    if not acc or acc.tg_user_id != call.from_user.id:
        await call.answer("Не найден", show_alert=True)
        return
    keywords = await storage.list_keywords(acc.id)
    if 0 <= idx < len(keywords):
        kw = keywords[idx]
        await storage.remove_keyword(acc.id, kw)
        if acc.filter_on:
            new_kws = await storage.list_keywords(acc.id)
            await filt.install_or_skip_sieve(acc, cipher, new_kws)
        await call.answer(f"Удалил: {kw}")
        await call.message.edit_text("Слово удалено.")
    else:
        await call.answer("Уже нет")


# ----- Wipe / nuke / chpwd -----

@router.callback_query(F.data.startswith("wipe:"))
async def cb_wipe(call: CallbackQuery):
    acc_id = int(call.data.split(":", 1)[1])
    await call.message.answer(
        "⚠️ Это удалит ВСЕ письма во ВСЕХ папках безвозвратно. Подтверди:",
        reply_markup=confirm_kb("wipe", acc_id),
    )
    await call.answer()


@router.callback_query(F.data.startswith("wipe!:"))
async def cb_wipe_confirm(call: CallbackQuery, storage: Storage, cipher: CredCipher):
    acc_id = int(call.data.split(":", 1)[1])
    acc = await storage.get_account(acc_id)
    if not acc or acc.tg_user_id != call.from_user.id:
        await call.answer("Не найден", show_alert=True)
        return
    await call.message.edit_text("Чищу… это может занять время.")
    password = cipher.decrypt(acc.password_enc)
    try:
        async with imap_client.imap_session(
            acc.imap_host, acc.imap_port, acc.imap_ssl, acc.email, password
        ) as client:
            report = await imap_client.wipe_all(client)
    except Exception as e:
        await call.message.edit_text(f"Не получилось: {h(str(e))}")
        return
    lines = [f"<code>{h(name)}</code>: {n}" for name, n in report.items()]
    await call.message.edit_text("Готово.\n" + "\n".join(lines[:50]))


@router.callback_query(F.data.startswith("nuke:"))
async def cb_nuke(call: CallbackQuery):
    await call.message.answer(
        "❗ IMAP не умеет удалять сам аккаунт у провайдера. Это можно сделать только в "
        "веб-интерфейсе провайдера (например, myaccount.google.com → Data & privacy → Delete "
        "your Google Account; для других провайдеров — в настройках аккаунта).\n\n"
        "Если хочешь стереть всё содержимое — используй «🧹 Очистить всю почту»."
    )
    await call.answer()


@router.callback_query(F.data.startswith("chpwd:"))
async def cb_chpwd(call: CallbackQuery):
    await call.message.answer(
        "🔑 Протокол IMAP не поддерживает смену пароля. Сменить пароль можно только через "
        "веб-интерфейс провайдера. После смены — забудь ящик в боте и добавь заново."
    )
    await call.answer()


@router.callback_query(F.data == "cancel")
async def cb_cancel(call: CallbackQuery):
    try:
        await call.message.edit_text("Отменено.")
    except Exception:
        pass
    await call.answer()


# ----- Connectivity check, folders, mails -----

async def _check_account(acc: Account, cipher: CredCipher) -> str:
    password = cipher.decrypt(acc.password_enc)
    try:
        async with imap_client.imap_session(
            acc.imap_host, acc.imap_port, acc.imap_ssl, acc.email, password
        ) as client:
            total, unseen = await imap_client.folder_stats(client, "INBOX")
            return (
                f"✅ <b>{h(acc.label)}</b> ({h(acc.email)})\n"
                f"IMAP <code>{h(acc.imap_host)}:{acc.imap_port}</code> — ок.\n"
                f"INBOX: всего {total}, непрочитано {unseen}."
            )
    except PermissionError:
        return f"⚠️ <b>{h(acc.label)}</b> ({h(acc.email)}): неверный пароль/логин."
    except Exception as e:
        return f"❌ <b>{h(acc.label)}</b> ({h(acc.email)}): {h(str(e)[:120])}"


@router.message(F.text == "🔌 Проверить доступность")
@router.message(Command("check"))
async def cmd_check(msg: Message, storage: Storage, cipher: CredCipher):
    accounts = await storage.list_accounts(msg.from_user.id)
    if not accounts:
        await msg.answer("Нет добавленных ящиков.")
        return
    note = await msg.answer("Проверяю все ящики…")
    results = await asyncio.gather(*(_check_account(a, cipher) for a in accounts))
    await note.edit_text("\n\n".join(results))


@router.callback_query(F.data.startswith("ping:"))
async def cb_ping(call: CallbackQuery, storage: Storage, cipher: CredCipher):
    acc_id = int(call.data.split(":", 1)[1])
    acc = await storage.get_account(acc_id)
    if not acc or acc.tg_user_id != call.from_user.id:
        await call.answer("Не найден", show_alert=True)
        return
    await call.answer("Проверяю…")
    text = await _check_account(acc, cipher)
    await call.message.answer(text)


@router.callback_query(F.data.startswith("folders:"))
async def cb_folders(call: CallbackQuery, storage: Storage, cipher: CredCipher):
    acc_id = int(call.data.split(":", 1)[1])
    acc = await storage.get_account(acc_id)
    if not acc or acc.tg_user_id != call.from_user.id:
        await call.answer("Не найден", show_alert=True)
        return
    await call.answer("Загружаю папки…")
    password = cipher.decrypt(acc.password_enc)
    try:
        async with imap_client.imap_session(
            acc.imap_host, acc.imap_port, acc.imap_ssl, acc.email, password
        ) as client:
            folders = await imap_client.list_folders(client)
    except Exception as e:
        await call.message.answer(f"Ошибка: {h(str(e))}")
        return
    if not folders:
        await call.message.answer("Папок не найдено.")
        return
    rows: list[list[InlineKeyboardButton]] = []
    for f in folders[:30]:
        if "\\Noselect" in f.flags:
            continue
        # callback_data has a 64-byte limit; truncate long names defensively.
        safe = f.name[:40]
        rows.append([
            InlineKeyboardButton(text=f"📁 {f.name}", callback_data=f"mails:{acc.id}:{safe}"),
        ])
    await call.message.answer(
        f"Папки ящика <b>{h(acc.label)}</b>:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.message(F.text == "📨 Письма")
async def cmd_mails_active(msg: Message, storage: Storage, cipher: CredCipher):
    acc = await storage.get_active(msg.from_user.id)
    if not acc:
        await msg.answer("Нет активного ящика. Выбери в 📂 Мои ящики.")
        return
    await _show_mails(msg, acc, "INBOX", cipher)


@router.callback_query(F.data.startswith("mails:"))
async def cb_mails(call: CallbackQuery, storage: Storage, cipher: CredCipher):
    parts = call.data.split(":", 2)
    if len(parts) < 3:
        await call.answer("Неверный запрос", show_alert=True)
        return
    acc_id = int(parts[1])
    folder = parts[2]
    acc = await storage.get_account(acc_id)
    if not acc or acc.tg_user_id != call.from_user.id:
        await call.answer("Не найден", show_alert=True)
        return
    await call.answer("Загружаю письма…")
    await _show_mails(call.message, acc, folder, cipher)


async def _show_mails(target: Message, acc: Account, folder: str, cipher: CredCipher) -> None:
    password = cipher.decrypt(acc.password_enc)
    try:
        async with imap_client.imap_session(
            acc.imap_host, acc.imap_port, acc.imap_ssl, acc.email, password
        ) as client:
            mails = await imap_client.fetch_recent(client, folder, limit=10)
    except Exception as e:
        await target.answer(f"Ошибка: {h(str(e))}")
        return
    if not mails:
        await target.answer(f"В папке <code>{h(folder)}</code> писем нет.")
        return
    lines = [f"<b>{h(folder)}</b> — последние {len(mails)}:\n"]
    for m in mails:
        marker = "•" if m.seen else "🆕"
        lines.append(
            f"{marker} <b>{h((m.subject or '(no subject)')[:80])}</b>\n"
            f"   от: {h(m.from_addr[:60])}\n"
            f"   {h(m.date[:40])}\n"
        )
    await target.answer("\n".join(lines))


# ----- Access management commands -----

@router.message(Command("whoami"))
async def cmd_whoami(msg: Message, storage: Storage):
    is_admin = await storage.is_admin(msg.from_user.id)
    role = "админ" if is_admin else "пользователь"
    await msg.answer(
        f"Твой Telegram ID: <code>{msg.from_user.id}</code>\nРоль: <b>{role}</b>"
    )


@router.message(Command("allow"))
async def cmd_allow(msg: Message, storage: Storage):
    if not await storage.is_admin(msg.from_user.id):
        await msg.answer("Только админ.")
        return
    parts = (msg.text or "").split(maxsplit=2)
    if len(parts) < 2:
        await msg.answer("Использование: <code>/allow &lt;tg_user_id&gt; [заметка]</code>")
        return
    try:
        uid = int(parts[1])
    except ValueError:
        await msg.answer("ID должен быть числом.")
        return
    note = parts[2] if len(parts) >= 3 else None
    await storage.allow_user(uid, is_admin=False, note=note)
    await msg.answer(f"Пользователь <code>{uid}</code> добавлен в whitelist.")


@router.message(Command("admin"))
async def cmd_admin(msg: Message, storage: Storage):
    if not await storage.is_admin(msg.from_user.id):
        await msg.answer("Только админ.")
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("Использование: <code>/admin &lt;tg_user_id&gt;</code>")
        return
    try:
        uid = int(parts[1])
    except ValueError:
        await msg.answer("ID должен быть числом.")
        return
    await storage.allow_user(uid, is_admin=True)
    await msg.answer(f"Пользователь <code>{uid}</code> теперь админ.")


@router.message(Command("revoke"))
async def cmd_revoke(msg: Message, storage: Storage):
    if not await storage.is_admin(msg.from_user.id):
        await msg.answer("Только админ.")
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("Использование: <code>/revoke &lt;tg_user_id&gt;</code>")
        return
    try:
        uid = int(parts[1])
    except ValueError:
        await msg.answer("ID должен быть числом.")
        return
    if uid == msg.from_user.id:
        await msg.answer("Себя забрать из whitelist не получится.")
        return
    await storage.revoke_user(uid)
    await msg.answer(f"Доступ для <code>{uid}</code> отозван.")


@router.message(Command("allowed"))
async def cmd_allowed(msg: Message, storage: Storage):
    if not await storage.is_admin(msg.from_user.id):
        await msg.answer("Только админ.")
        return
    rows = await storage.list_allowed()
    if not rows:
        await msg.answer("Whitelist пуст.")
        return
    lines = ["<b>Whitelist:</b>"]
    for uid, is_admin, note in rows:
        tag = "👑" if is_admin else "👤"
        suffix = f" — {h(note)}" if note else ""
        lines.append(f"{tag} <code>{uid}</code>{suffix}")
    await msg.answer("\n".join(lines))


# ---------- Bootstrap ----------

async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings()
    storage = Storage(settings.db_path)
    await storage.init()
    cipher = CredCipher(settings.fernet_key)

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp["storage"] = storage
    dp["cipher"] = cipher

    allowlist_mw = AllowlistMiddleware(storage, settings.admin_ids)
    dp.message.middleware(allowlist_mw)
    dp.callback_query.middleware(allowlist_mw)

    dp.include_router(router)

    daemon = FilterDaemon(storage, cipher, settings.filter_interval)
    daemon.start()

    try:
        await dp.start_polling(bot)
    finally:
        await daemon.stop()


if __name__ == "__main__":
    asyncio.run(main())
