"""
Telegram-бот Family Assistant.

Сознательно тонкий слой: принять сообщение → вызвать handle_message()
→ отправить ответ. Вся содержательная логика живёт в handle.py и уже
проверена из командной строки. Если бот замолчит, искать причину надо
здесь; если ответит неправильно — в handle.py.

Запуск:
    python3 app/bot.py          тестовый бот (TELEGRAM_TOKEN_DEV)
    python3 app/bot.py --prod   рабочий  (TELEGRAM_TOKEN_PROD)
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

sys.path.insert(0, str(Path(__file__).parent))

from db import cancel_task, complete_task, reschedule_task
from handle import handle_message, try_command
from parser import TZ
from scheduler import register_jobs
from ui import (build_backlog, build_daily, build_evening, build_month,
                build_morning, build_week, clarify_buttons, postpone_options)

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

logging.basicConfig(
    format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
    level=logging.INFO,
)
# httpx на каждый запрос к Telegram пишет строку в лог. При long polling
# это запрос каждые несколько секунд — полезные сообщения утонут.
logging.getLogger("httpx").setLevel(logging.WARNING)

log = logging.getLogger("familybot")


# ------------------------------------------------------------------
# Команды
# ------------------------------------------------------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Записываю семейные дела.\n\n"
        "Просто напиши, что нужно сделать:\n"
        "  Севе теннис во вторник в 3\n"
        "  утром пилатес\n"
        "  надо газон подстричь\n\n"
        "Посмотреть, что записано:\n"
        "  Сводка на день\n"
        "  Сводка на неделю\n"
        "  Сводка на месяц\n"
        "  Список дел\n"
        "  Ежедневные\n\n"
        "/chatid — номер этого чата"
    )


async def cmd_chatid(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Номер чата. Понадобится, чтобы планировщик знал, куда слать сводки.
    У групп он отрицательный, у личной переписки — положительный.

    Без parse_mode намеренно. С Markdown слово «chat_id» ломало отправку:
    одиночное подчёркивание Telegram считает открывающим маркером курсива,
    не находит парного и отклоняет сообщение целиком. Разметка здесь
    ничего не даёт, а сломать может.
    """
    chat = update.effective_chat
    await update.message.reply_text(
        f"chat id: {chat.id}\n"
        f"тип: {chat.type}\n"
        f"название: {chat.title or '—'}"
    )


async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Эхо. Проверяет, что privacy mode отключён: если бот отвечает на
    /ping, но молчит на обычный текст — значит настройка не применилась.
    """
    await update.message.reply_text("жив")


# ------------------------------------------------------------------
# Обычные сообщения
# ------------------------------------------------------------------

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.text:
        return

    text = msg.text.strip()
    if not text:
        return

    log.info("вход: chat=%s %r", msg.chat_id, text)

    # Разбор занимает секунду-две. Без индикатора человек не понимает,
    # дошло сообщение или нет, и пишет его повторно.
    await ctx.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)

    # handle_message синхронная: внутри сетевой вызов к Anthropic и
    # запросы к Postgres. Прямой вызов заблокировал бы весь бот на это
    # время — второе сообщение не начало бы обрабатываться, пока не
    # закончится первое. to_thread уводит её в отдельный поток.
    try:
        reply = await asyncio.to_thread(
            handle_message, text, msg.chat_id, msg.message_id
        )
    except Exception:
        # Полный стектрейс в лог, короткая фраза человеку. Показывать
        # внутреннюю ошибку в семейном чате бессмысленно.
        log.exception("сбой обработки")
        await msg.reply_text("Что-то сломалось. Попробуй ещё раз.")
        return

    log.info("ответ: %r", reply.text.replace("\n", " | "))

    # Сводки приходят с уже собранной разметкой и HTML внутри текста.
    # Обычные ответы — с options (или без) и без разметки: там текст
    # задачи не экранирован, и parse_mode="HTML" сломал бы отправку
    # на первом же символе < в названии.
    kb = reply.markup or (clarify_buttons(reply.options)
                          if reply.options else None)
    sent = await msg.reply_text(reply.text, reply_markup=kb,
                                parse_mode="HTML" if reply.html else None)

    # Варианты переспроса храним в памяти бота, привязав к отправленному
    # сообщению: в callback_data влезает только номер варианта.
    # Перезапуск бота их теряет — тогда на нажатие честно ответим,
    # что уточнение устарело.
    if reply.options:
        ctx.bot_data.setdefault("clarify", {})[sent.message_id] = {
            "original": text,
            "options": reply.options,
        }


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Ошибки самого Telegram-слоя: сеть, лимиты, битые апдейты."""
    log.error("ошибка Telegram", exc_info=ctx.error)


# ------------------------------------------------------------------
# Нажатия на кнопки
# ------------------------------------------------------------------

async def _rerender(query, chat_id: int) -> None:
    """
    Перерисовывает сообщение, под которым нажали кнопку.

    Вместо отправки нового «Готово!» правим исходное: закрытая задача
    из него исчезает, кнопки обновляются. Чат остаётся читаемым, а
    сводка всегда показывает актуальное состояние.

    Какую сводку собрать, определяем по первому символу текста —
    хранить это отдельно негде: callback_data привязан к кнопке,
    а не к сообщению.
    """
    today = datetime.now(TZ).date()
    head = (query.message.text or "")[:2]

    if head.startswith("🌙"):
        text, kb = build_evening(today)
    elif head.startswith("☀"):
        text, kb = build_morning(today)
    elif head.startswith("📅"):
        text, kb = build_week(today)
    elif head.startswith("🗓"):
        text, kb = build_month(today)
    elif head.startswith("📌"):
        text, kb = build_backlog(today)
    elif head.startswith("🔁"):
        text, kb = build_daily()
    else:
        # Точечное напоминание или пинг на часть дня: задача закрыта,
        # перерисовывать нечего — убираем кнопки и помечаем сообщение.
        await query.edit_message_text(
            (query.message.text_html or query.message.text) + "\n✅",
            parse_mode="HTML")
        return

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    log.info("кнопка: chat=%s %r", query.message.chat_id, data)

    try:
        if data == "back":
            # Возврат из меню переноса: восстанавливаем обычные кнопки
            await _rerender(query, query.message.chat_id)
            await query.answer()
            return

        action, _, rest = data.partition(":")

        if action == "cl":
            pending = ctx.bot_data.get("clarify", {}).pop(
                query.message.message_id, None)
            if not pending:
                await query.answer("Уточнение устарело, повтори фразу")
                await query.edit_message_reply_markup(reply_markup=None)
                return
            choice = pending["options"][int(rest)]
            await query.answer(choice[:60])

            # Сначала проверяем вариант по таблице команд. «Посмотреть
            # список дел» — это запрос на чтение, и склеивать его
            # с исходной фразой нельзя: парсер снова не поймёт и снова
            # предложит тот же переспрос.
            reply = await asyncio.to_thread(try_command, choice)

            if reply is None:
                # Обычный случай: вариант уточняет исходную фразу
                # («09:00» к «пилатес в 9»). Склеиваем и разбираем.
                # Формат с отдельной строкой «Уточнение:», а не через
                # тире. Тире между числами парсер читал как диапазон:
                # «пилатес в 9 — 21:00» превращалось в 09:00–21:00.
                combined = f"{pending['original']}\nУточнение: {choice}"
                reply = await asyncio.to_thread(
                    handle_message, combined, query.message.chat_id)

            await query.edit_message_text(f"{query.message.text}\n\n→ {choice}")
            await ctx.bot.send_message(
                query.message.chat_id, reply.text,
                reply_markup=reply.markup,
                parse_mode="HTML" if reply.html else None)
            return

        if action == "done":
            task = await asyncio.to_thread(complete_task, int(rest))
            await query.answer("Закрыто" if task else "Уже закрыто")
            await _rerender(query, query.message.chat_id)

        elif action == "post":
            # Первый уровень: показываем варианты, текст не трогаем
            await query.edit_message_reply_markup(
                reply_markup=postpone_options(int(rest)))
            await query.answer()

        elif action == "pto":
            tid, _, when = rest.partition(":")
            if when == "x":
                new_date = None      # убрать дату → отдельные дела
            else:
                new_date = datetime.now(TZ).date() + timedelta(days=int(when))
            task = await asyncio.to_thread(reschedule_task, int(tid), new_date)
            await query.answer("Перенесено" if task else "Задача не найдена")
            await _rerender(query, query.message.chat_id)

        elif action == "cancel":
            task = await asyncio.to_thread(cancel_task, int(rest))
            await query.answer("Отменено" if task else "Задача не найдена")
            await _rerender(query, query.message.chat_id)

        else:
            await query.answer("Не понял кнопку")

    except Exception:
        log.exception("сбой обработки кнопки")
        # answer() обязателен: без него у человека висит «часики» на кнопке
        # до таймаута, и кажется, что бот завис.
        await query.answer("Что-то сломалось")


# ------------------------------------------------------------------

def main() -> None:
    prod = "--prod" in sys.argv
    var = "TELEGRAM_TOKEN_PROD" if prod else "TELEGRAM_TOKEN_DEV"
    token = os.environ.get(var)

    if not token or token.startswith("..."):
        sys.exit(
            f"Нет {var}.\n"
            f"Добавь в {ROOT / '.env'} строку:\n"
            f"  {var}=токен_от_BotFather"
        )

    log.info("запуск: %s", "РАБОЧИЙ бот" if prod else "тестовый бот")

    app = Application.builder().token(token).build()

    # Планировщик знает, куда слать сводки, только из настроек: в 08:00
    # никто боту не пишет, и взять chat_id из входящего сообщения неоткуда.
    chat_var = "TELEGRAM_CHAT_ID_PROD" if prod else "TELEGRAM_CHAT_ID_DEV"
    chat_id = os.environ.get(chat_var)
    if chat_id:
        register_jobs(app, int(chat_id))
    else:
        log.warning("нет %s — расписание не запущено, "
                    "бот только отвечает на сообщения", chat_var)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("ping", cmd_ping))
    # ~filters.COMMAND — всё, что не команда. Иначе обработчик перехватит
    # и /start тоже, и парсер получит «/start» как текст задачи.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)

    log.info("слушаю сообщения, Ctrl+C для остановки")

    # Long polling: бот сам опрашивает Telegram. Публичный HTTPS-адрес
    # не нужен, поэтому работает с ноутбука без ngrok и деплоя.
    # drop_pending_updates — не разгребать то, что накопилось, пока бот
    # лежал. Иначе после паузы в чат прилетит пачка ответов на старое.
    app.run_polling(allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=True)


if __name__ == "__main__":
    main()
