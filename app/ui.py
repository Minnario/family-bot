"""
Отрисовка сообщений и кнопок.

Вынесено отдельным модулем, потому что одни и те же сводки собирают
двое: планировщик при первой отправке и обработчик нажатий при
перерисовке. Если бы функции жили в scheduler.py, bot.py импортировал
бы планировщик ради вёрстки — связь не по делу.

Разметка HTML, не Markdown. Причина конкретная: в Markdown одиночное
подчёркивание или звёздочка в тексте задачи ломает отправку целиком
(мы на этом уже спотыкались на слове chat_id). В HTML достаточно
экранировать три символа, и текст пользователя становится безопасным.
"""

from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sys
sys.path.insert(0, str(Path(__file__).parent))

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from db import tasks_backlog, tasks_daily, tasks_for_date, tasks_overdue, deadlines_soon

ASSIGNEE_RU = {
    "seva": "Сева", "gleb": "Глеб", "kamilla": "Камилла",
    "vova": "Вова", "sasha": "Саша",
}

DAYPART_RU = {"morning": "утром", "afternoon": "днём", "evening": "вечером"}

WEEKDAYS_SHORT = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]

MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря"]


# ------------------------------------------------------------------
# Мелкая вёрстка
# ------------------------------------------------------------------

def who(task: Dict[str, Any]) -> str:
    return ASSIGNEE_RU.get(task.get("assignee")) or "дом"


def time_label(task: Dict[str, Any]) -> str:
    if task.get("time_start"):
        s = task["time_start"].strftime("%H:%M")
        if task.get("time_end"):
            return f"{s}–{task['time_end'].strftime('%H:%M')}"
        return s
    if task.get("daypart"):
        return DAYPART_RU.get(task["daypart"], "")
    return ""


def day_label(d: Optional[date], today: date) -> str:
    if not d:
        return ""
    delta = (d - today).days
    if delta == 0:
        return "сегодня"
    if delta == 1:
        return "завтра"
    if 2 <= delta <= 6:
        return WEEKDAYS_SHORT[d.weekday()]
    return f"{WEEKDAYS_SHORT[d.weekday()]} {d.strftime('%d.%m')}"


def task_line(t: Dict[str, Any]) -> str:
    """Одна строка задачи. Текст задачи экранируется — он от пользователя."""
    tl = time_label(t)
    prefix = f"{tl}  " if tl else ""
    return f"{prefix}{who(t)} — {escape(t['title'])}"


# ------------------------------------------------------------------
# Кнопки
# ------------------------------------------------------------------
# Формат callback_data: действие:id[:параметр]
# Telegram ограничивает поле 64 байтами, здесь укладываемся с запасом.

def task_buttons(tasks: List[Dict[str, Any]]) -> Optional[InlineKeyboardMarkup]:
    """
    По ряду кнопок на задачу. Больше восьми задач — кнопок не будет:
    список станет длиннее самого сообщения и потеряет смысл.

    Время в ярлыке обязательно. Без него два «Сева · теннис» на разное
    время выглядят одинаково, и человек не знает, какую закрывает.
    """
    if not tasks or len(tasks) > 8:
        return None
    rows = []
    for t in tasks:
        tl = time_label(t)
        label = f"{tl} {who(t)} · {t['title']}" if tl else f"{who(t)} · {t['title']}"
        # Telegram обрезает длинные ярлыки по-своему, лучше сделать это
        # самим — так видно, что текст сокращён.
        if len(label) > 30:
            label = label[:29] + "…"
        rows.append([
            InlineKeyboardButton(f"✅ {label}", callback_data=f"done:{t['id']}"),
            InlineKeyboardButton("⏰", callback_data=f"post:{t['id']}"),
        ])
    return InlineKeyboardMarkup(rows)


def clarify_buttons(options: List[str]) -> Optional[InlineKeyboardMarkup]:
    """
    Кнопки для переспроса. В callback_data идёт номер варианта, а не
    его текст: варианты бывают длинными («Сева, понедельник 15:00»),
    а поле ограничено 64 байтами, и кириллица занимает два байта
    на символ. Сам текст хранится в памяти бота до нажатия.
    """
    if not options:
        return None
    rows = [[InlineKeyboardButton(o[:40], callback_data=f"cl:{i}")]
            for i, o in enumerate(options[:6])]
    return InlineKeyboardMarkup(rows)


def postpone_options(task_id: int) -> InlineKeyboardMarkup:
    """Второй уровень: куда переносим. Показывается после нажатия ⏰."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Завтра", callback_data=f"pto:{task_id}:1"),
         InlineKeyboardButton("Послезавтра", callback_data=f"pto:{task_id}:2")],
        [InlineKeyboardButton("Через неделю", callback_data=f"pto:{task_id}:7"),
         InlineKeyboardButton("Убрать дату", callback_data=f"pto:{task_id}:x")],
        [InlineKeyboardButton("Отменить задачу", callback_data=f"cancel:{task_id}"),
         InlineKeyboardButton("← Назад", callback_data="back")],
    ])


# ------------------------------------------------------------------
# Сводки
# ------------------------------------------------------------------

def build_morning(today: date) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    """Утренняя сводка. Возвращает готовый текст и кнопки."""
    today_tasks = tasks_for_date(today)
    overdue = tasks_overdue(today)
    deadlines = deadlines_soon(today, days=1)
    daily = tasks_daily()

    lines = [f"☀️ <b>{WEEKDAYS_SHORT[today.weekday()]}, "
             f"{today.day} {MONTHS_RU[today.month - 1]}</b>", ""]

    if today_tasks:
        lines += [f"  {task_line(t)}" for t in today_tasks]
    else:
        lines.append("На сегодня ничего не запланировано.")

    if deadlines:
        lines.append("")
        for t in deadlines:
            mark = "🔴 сегодня" if t["deadline"] == today else "⚡ завтра"
            lines.append(f"{mark} дедлайн: {who(t)} — {escape(t['title'])}")

    if overdue:
        lines.append("")
        lines.append("🔴 <b>Просрочено:</b>")
        for t in overdue:
            lines.append(f"  {who(t)} — {escape(t['title'])} "
                         f"({day_label(t['date'], today)})")

    if daily:
        lines.append("")
        lines.append("☑️ Ежедневно: " + " · ".join(
            f"{who(t)} {escape(t['title'])}" for t in daily))

    return "\n".join(lines), task_buttons(today_tasks)


def build_evening(today: date) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    """Вечерняя сверка: что осталось незакрытым."""
    left = tasks_for_date(today)
    backlog = tasks_backlog(today)
    daily = tasks_daily()

    lines = [f"🌙 <b>Итоги дня, {today.day} {MONTHS_RU[today.month - 1]}</b>", ""]

    if left:
        lines.append("Не отмечено сегодня:")
        lines += [f"  {who(t)} — {escape(t['title'])}" for t in left]
    else:
        lines.append("Всё на сегодня закрыто. ✅")

    if daily:
        lines.append("")
        lines.append("Ежедневные: " + " · ".join(
            f"{who(t)} {escape(t['title'])}" for t in daily))

    if backlog:
        lines.append("")
        lines.append("📌 <b>Отдельные дела:</b>")
        for t in backlog:
            row = f"  {who(t)} — {escape(t['title'])}"
            if t.get("deadline"):
                row += f"  ⏳ до {day_label(t['deadline'], today)}"
            # Счётчик виден с четвёртого переноса: задача, которую двигают
            # месяц, скорее всего не будет сделана никогда.
            if (t.get("postponed_count") or 0) >= 4:
                row += f"  ⚠️×{t['postponed_count']}"
            lines.append(row)

    return "\n".join(lines), task_buttons(left)


def build_reminder(task: Dict[str, Any], minutes: int
                   ) -> Tuple[str, InlineKeyboardMarkup]:
    """Точечное напоминание с кнопками."""
    text = (f"⏰ через {minutes} мин: {who(task)} — "
            f"<b>{escape(task['title'])}</b>  ({time_label(task)})")
    return text, task_buttons([task])
