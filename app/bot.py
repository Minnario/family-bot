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
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (Application, CommandHandler, ContextTypes,
                          MessageHandler, filters)

sys.path.insert(0, str(Path(__file__).parent))

from handle import handle_message
from scheduler import register_jobs

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

    log.info("ответ: %r", reply.replace("\n", " | "))
    await msg.reply_text(reply)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Ошибки самого Telegram-слоя: сеть, лимиты, битые апдейты."""
    log.error("ошибка Telegram", exc_info=ctx.error)


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
