"""
Планировщик Family Assistant.

Решает ТОЛЬКО когда и что отправить. Никакого LLM: расписание —
детерминированная работа, и модель здесь не нужна ни для выбора
момента, ни для формулировок. Модель разбирает язык на входе,
планировщик считает время на выходе.

Каждую функцию можно запустить руками, не дожидаясь 08:00:

    python3 app/scheduler.py morning
    python3 app/scheduler.py evening
    python3 app/scheduler.py timed
    python3 app/scheduler.py daypart evening
"""

import asyncio
import logging
import os
import sys
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))

from db import (record_reminder, reminder_sent, tasks_daypart_now,
                tasks_timed_today)
from ui import DAYPART_RU, build_evening, build_morning, build_reminder, who
from parser import TZ

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

log = logging.getLogger("scheduler")

# ------------------------------------------------------------------
# Настройки времени — те, что зафиксировали при проектировании
# ------------------------------------------------------------------

DIGEST_MORNING = time(8, 0)
DIGEST_EVENING = time(21, 0)

# Вне этого окна бот молчит. Задача с напоминанием на 23:30 останется
# в базе и попадёт в сводку, но ночного пинга не будет.
QUIET_FROM = time(23, 0)
QUIET_TO = time(6, 0)

# Начало частей дня. Утро не пингуем отдельно: в 08:00 уходит сводка,
# которая эти задачи и так показывает — второе сообщение было бы шумом.
DAYPART_START = {"afternoon": time(12, 0), "evening": time(17, 0)}

# ------------------------------------------------------------------
# Лестница напоминаний
# ------------------------------------------------------------------
#
# Четыре ступени на задачу: за час, за полчаса, за десять минут
# и в момент начала. Порядок от дальней к ближней — due_stage() идёт
# по списку сверху вниз и останавливается на первом совпадении.
#
# Второй элемент пары — значение reminder_kind в базе. Виды разные
# намеренно: защита от дублей стоит на ключе (task_id, kind, sent_date),
# и под общим 'timed' вторая ступень дня не прошла бы.
REMINDER_LADDER = [
    (60, "lead_60"),
    (30, "lead_30"),
    (10, "lead_10"),
    (0,  "start"),
]

# Как часто планировщик проверяет задачи, в секундах. Было 300.
# При пятиминутном опросе ступень пришлось бы ловить окном той же
# ширины, и «через 30 минут» уезжало бы на пять минут в любую сторону.
# Запрос индексированный и лёгкий, минута обходится дёшево.
POLL_INTERVAL = 60

# Ширина окна ступени в минутах. Попасть ровно в 60.000 невозможно,
# поэтому ступень 60 ловится при остатке от 58 до 60 включительно.
# Две минуты при опросе раз в минуту гарантируют, что окно не проскочит
# между проверками. Повтор внутри окна отсекает reminder_sent().
STAGE_WINDOW = 2


def is_quiet(now: datetime) -> bool:
    """Ночное окно пересекает полночь, поэтому проверка через ИЛИ."""
    t = now.time()
    return t >= QUIET_FROM or t < QUIET_TO


# ------------------------------------------------------------------
# Сводки
# ------------------------------------------------------------------

async def morning_digest(bot, chat_id: int, now: Optional[datetime] = None) -> None:
    """
    Одно сообщение со всем, что нужно знать про день.

    Вместо семи отдельных пингов — сводка. Если каждая задача станет
    отдельным сообщением, группу отключат в первую же неделю.
    """
    now = now or datetime.now(TZ)
    text, kb = build_morning(now.date())
    await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)
    log.info("утренняя сводка отправлена")


async def evening_digest(bot, chat_id: int, now: Optional[datetime] = None) -> None:
    """
    Что осталось незакрытым. Кнопки те же, что в утренней сводке:
    вечером можно закрыть или перенести прямо отсюда.
    """
    now = now or datetime.now(TZ)
    text, kb = build_evening(now.date())
    await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)
    log.info("вечерняя сверка отправлена")


# ------------------------------------------------------------------
# Точечные напоминания
# ------------------------------------------------------------------

def due_stage(left_min: float) -> Optional[str]:
    """
    Какая ступень наступила при таком остатке до начала, в минутах.

    Возвращает вид напоминания или None, если сейчас ни одна ступень
    не подошла. Остаток отрицательный означает, что событие уже началось:
    ступень 'start' ловится в окне от 0 до −2 минут.

    Если бот был выключен и окно ступени прошло — она не сработает
    вовсе. Это осознанно: написать «через час» за сорок минут до начала
    хуже, чем промолчать, а следующая ступень всё равно придёт.
    """
    for offset, kind in REMINDER_LADDER:
        if offset - STAGE_WINDOW < left_min <= offset:
            return kind
    return None


async def timed_reminders(bot, chat_id: int, now: Optional[datetime] = None) -> int:
    """
    Задачи с конкретным временем — четыре ступени на каждую.

    Запускается раз в POLL_INTERVAL секунд, перебирает все задачи дня
    и для каждой считает, какая ступень подошла. reminder_sent()
    обязателен: окно ступени шире шага опроса, задача попадёт в него
    дважды.

    Кнопки приходят только с последней ступенью — это решает
    build_reminder(), здесь клавиатура просто передаётся дальше
    и может быть None.
    """
    now = now or datetime.now(TZ)
    if is_quiet(now):
        return 0

    sent = 0
    for t in tasks_timed_today(now):
        left = (datetime.combine(now.date(), t["time_start"]) -
                datetime.combine(now.date(), now.time()))
        stage = due_stage(left.total_seconds() / 60)
        if stage is None:
            continue
        if reminder_sent(t["id"], stage, now.date()):
            continue

        text, kb = build_reminder(t, stage)
        msg = await bot.send_message(chat_id, text, parse_mode="HTML",
                                     reply_markup=kb)
        # Запись ПОСЛЕ успешной отправки: если сделать раньше и отправка
        # упадёт, напоминание потеряется молча.
        record_reminder(t["id"], chat_id, msg.message_id, stage, now.date())
        sent += 1
        log.info("напоминание %s по задаче #%s", stage, t["id"])

    return sent


async def daypart_reminders(bot, chat_id: int, daypart_name: str,
                            now: Optional[datetime] = None) -> int:
    """Один пинг в начале части дня для задач без точного времени."""
    now = now or datetime.now(TZ)
    if is_quiet(now):
        return 0

    tasks = [t for t in tasks_daypart_now(now, daypart_name)
             if not reminder_sent(t["id"], "timed", now.date())]
    if not tasks:
        return 0

    from ui import task_buttons
    from html import escape

    label = DAYPART_RU.get(daypart_name, daypart_name)
    lines = [f"⏰ На {label}:"] + [
        f"  {who(t)} — {escape(t['title'])}" for t in tasks]

    msg = await bot.send_message(chat_id, "\n".join(lines),
                                 parse_mode="HTML",
                                 reply_markup=task_buttons(tasks))
    for t in tasks:
        record_reminder(t["id"], chat_id, msg.message_id, "timed", now.date())

    log.info("напоминание на %s: %d задач", label, len(tasks))
    return len(tasks)


# ------------------------------------------------------------------
# Регистрация в боте
# ------------------------------------------------------------------

def register_jobs(app, chat_id: int) -> None:
    """
    Вешает задания на JobQueue из python-telegram-bot.

    JobQueue живёт внутри процесса бота — отдельный системный cron не
    нужен. Обратная сторона: упал процесс, встали и напоминания.
    Поэтому на сервере нужен systemd, который поднимает его обратно.

    Времена передаются с tzinfo, иначе JobQueue возьмёт время сервера.
    На Oracle это UTC, и летом сводка ушла бы на час раньше.
    """
    jq = app.job_queue

    # JobQueue поставляется отдельным дополнением к python-telegram-bot.
    # Без него app.job_queue равен None, и обращение к нему валит бота
    # стектрейсом про NoneType. Проверяем явно и говорим, что делать.
    if jq is None:
        log.error("JobQueue не установлен — расписание не запущено.\n"
                  '  Поставь: pip3 install "python-telegram-bot[job-queue]"\n'
                  "  Бот продолжит отвечать на сообщения, но сводок не будет.")
        return

    jq.run_daily(lambda ctx: morning_digest(ctx.bot, chat_id),
                 time=DIGEST_MORNING.replace(tzinfo=TZ), name="morning")

    jq.run_daily(lambda ctx: evening_digest(ctx.bot, chat_id),
                 time=DIGEST_EVENING.replace(tzinfo=TZ), name="evening")

    jq.run_repeating(lambda ctx: timed_reminders(ctx.bot, chat_id),
                     interval=POLL_INTERVAL, first=30, name="timed")

    for name, start in DAYPART_START.items():
        jq.run_daily(
            lambda ctx, n=name: daypart_reminders(ctx.bot, chat_id, n),
            time=start.replace(tzinfo=TZ), name=f"daypart_{name}")

    log.info("расписание: сводки %s и %s, проверка задач каждые %d сек, "
             "ступени %s",
             DIGEST_MORNING.strftime("%H:%M"), DIGEST_EVENING.strftime("%H:%M"),
             POLL_INTERVAL,
             ", ".join(str(o) for o, _ in REMINDER_LADDER))


# ------------------------------------------------------------------
# Ручной запуск
# ------------------------------------------------------------------

async def _main() -> None:
    from telegram import Bot

    if len(sys.argv) < 2:
        sys.exit("Использование: python3 app/scheduler.py "
                 "morning|evening|timed|daypart <утро|день|вечер>")

    token = os.environ.get("TELEGRAM_TOKEN_DEV")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID_DEV")
    if not token or not chat_id:
        sys.exit("Нужны TELEGRAM_TOKEN_DEV и TELEGRAM_CHAT_ID_DEV в .env")

    bot = Bot(token)
    chat_id = int(chat_id)
    cmd = sys.argv[1]

    if cmd == "morning":
        await morning_digest(bot, chat_id)
    elif cmd == "evening":
        await evening_digest(bot, chat_id)
    elif cmd == "timed":
        n = await timed_reminders(bot, chat_id)
        print(f"отправлено: {n}")
    elif cmd == "daypart":
        dp = sys.argv[2] if len(sys.argv) > 2 else "evening"
        n = await daypart_reminders(bot, chat_id, dp)
        print(f"отправлено задач: {n}")
    else:
        sys.exit(f"неизвестная команда: {cmd}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-7s %(message)s")
    # httpx логирует полный URL запроса, а токен бота — часть этого URL.
    # Без этой строки токен попадает в вывод терминала и в файлы логов
    # на сервере. Ровно так он и утёк на скриншоте.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(_main())
