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

from db import (deadlines_soon, record_reminder, reminder_sent, tasks_backlog,
                tasks_daily, tasks_daypart_now, tasks_due_now, tasks_for_date,
                tasks_overdue)
from handle import ASSIGNEE_RU, DAYPART_RU, WEEKDAYS_SHORT, _fmt_day
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

MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def is_quiet(now: datetime) -> bool:
    """Ночное окно пересекает полночь, поэтому проверка через ИЛИ."""
    t = now.time()
    return t >= QUIET_FROM or t < QUIET_TO


def _who(task: Dict[str, Any]) -> str:
    return ASSIGNEE_RU.get(task.get("assignee")) or "дом"


def _time_label(task: Dict[str, Any]) -> str:
    if task.get("time_start"):
        s = task["time_start"].strftime("%H:%M")
        if task.get("time_end"):
            return f"{s}–{task['time_end'].strftime('%H:%M')}"
        return s
    if task.get("daypart"):
        return DAYPART_RU.get(task["daypart"], "")
    return ""


# ------------------------------------------------------------------
# Утренняя сводка
# ------------------------------------------------------------------

async def morning_digest(bot, chat_id: int, now: Optional[datetime] = None) -> None:
    """
    Одно сообщение со всем, что нужно знать про день.

    Вместо семи отдельных пингов — сводка. Если каждая задача станет
    отдельным сообщением, группу отключат в первую же неделю.
    """
    now = now or datetime.now(TZ)
    today = now.date()

    today_tasks = tasks_for_date(today)
    overdue = tasks_overdue(today)
    deadlines = deadlines_soon(today, days=1)
    daily = tasks_daily()

    head = f"☀️ {WEEKDAYS_SHORT[today.weekday()]}, {today.day} {MONTHS_RU[today.month - 1]}"
    lines = [head, ""]

    if today_tasks:
        for t in today_tasks:
            tl = _time_label(t)
            prefix = f"{tl:>7}  " if tl else " " * 9
            lines.append(f"{prefix}{_who(t)} — {t['title']}")
    else:
        lines.append("На сегодня ничего не запланировано.")

    if deadlines:
        lines.append("")
        for t in deadlines:
            mark = "🔴 сегодня" if t["deadline"] == today else "⚡ завтра"
            lines.append(f"{mark} дедлайн: {_who(t)} — {t['title']}")

    if overdue:
        lines.append("")
        lines.append("🔴 Просрочено:")
        for t in overdue:
            lines.append(f"   {_who(t)} — {t['title']} (было {_fmt_day(t['date'].isoformat(), today)})")

    if daily:
        lines.append("")
        lines.append("☑️ Ежедневно: " + " · ".join(
            f"{_who(t)} {t['title']}" for t in daily))

    await bot.send_message(chat_id, "\n".join(lines))
    log.info("утренняя сводка отправлена: %d задач", len(today_tasks))


# ------------------------------------------------------------------
# Вечерняя сверка
# ------------------------------------------------------------------

async def evening_digest(bot, chat_id: int, now: Optional[datetime] = None) -> None:
    """
    Что осталось незакрытым.

    Пока только показывает. Вопрос «когда перенесём» появится вместе
    с кнопками — городить временный текстовый диалог, который через
    неделю выбросим, смысла нет.
    """
    now = now or datetime.now(TZ)
    today = now.date()

    left = [t for t in tasks_for_date(today)]
    backlog = tasks_backlog(today)
    daily = tasks_daily()

    lines = [f"🌙 Итоги дня, {today.day} {MONTHS_RU[today.month - 1]}", ""]

    if left:
        lines.append("Не отмечено сегодня:")
        for t in left:
            lines.append(f"   {_who(t)} — {t['title']}")
    else:
        lines.append("Всё на сегодня закрыто. ✅")

    if daily:
        lines.append("")
        lines.append("Ежедневные: " + " · ".join(
            f"{_who(t)} {t['title']}" for t in daily))

    if backlog:
        lines.append("")
        lines.append("📌 Отдельные дела:")
        for t in backlog:
            row = f"   {_who(t)} — {t['title']}"
            if t.get("deadline"):
                row += f"  ⏳ до {_fmt_day(t['deadline'].isoformat(), today)}"
            # Счётчик переносов виден с четвёртого раза: задача, которую
            # двигают месяц, скорее всего не будет сделана никогда.
            if (t.get("postponed_count") or 0) >= 4:
                row += f"  ⚠️×{t['postponed_count']}"
            lines.append(row)

    await bot.send_message(chat_id, "\n".join(lines))
    log.info("вечерняя сверка отправлена: %d незакрытых", len(left))


# ------------------------------------------------------------------
# Точечные напоминания
# ------------------------------------------------------------------

async def timed_reminders(bot, chat_id: int, now: Optional[datetime] = None) -> int:
    """
    Задачи с конкретным временем — за reminder_lead минут до начала.

    Запускается раз в пять минут. В окно напоминания задача попадёт
    два-три раза подряд, поэтому проверка reminder_sent() обязательна.
    """
    now = now or datetime.now(TZ)
    if is_quiet(now):
        return 0

    sent = 0
    for t in tasks_due_now(now):
        if reminder_sent(t["id"], "timed", now.date()):
            continue

        left = (datetime.combine(now.date(), t["time_start"]) -
                datetime.combine(now.date(), now.time()))
        mins = max(0, int(left.total_seconds() // 60))

        text = (f"⏰ через {mins} мин: {_who(t)} — {t['title']}"
                f"  ({_time_label(t)})")

        msg = await bot.send_message(chat_id, text)
        # Запись ПОСЛЕ успешной отправки: если сделать раньше и отправка
        # упадёт, напоминание потеряется молча.
        record_reminder(t["id"], chat_id, msg.message_id, "timed", now.date())
        sent += 1

    if sent:
        log.info("точечных напоминаний отправлено: %d", sent)
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

    label = DAYPART_RU.get(daypart_name, daypart_name)
    lines = [f"⏰ На {label}:"] + [f"   {_who(t)} — {t['title']}" for t in tasks]

    msg = await bot.send_message(chat_id, "\n".join(lines))
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
                     interval=300, first=30, name="timed")

    for name, start in DAYPART_START.items():
        jq.run_daily(
            lambda ctx, n=name: daypart_reminders(ctx.bot, chat_id, n),
            time=start.replace(tzinfo=TZ), name=f"daypart_{name}")

    log.info("расписание: сводки %s и %s, точечные каждые 5 мин",
             DIGEST_MORNING.strftime("%H:%M"), DIGEST_EVENING.strftime("%H:%M"))


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
