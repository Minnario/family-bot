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

from db import (tasks_backlog, tasks_daily, tasks_for_date, tasks_for_range,
                tasks_overdue, deadlines_soon)

ASSIGNEE_RU = {
    "seva": "Сева", "gleb": "Глеб", "kamilla": "Камилла",
    "vova": "Вова", "sasha": "Саша",
}

DAYPART_RU = {"morning": "утром", "afternoon": "днём", "evening": "вечером"}

# Тот же набор, но в форме для заголовка: «На вечер», а не «На вечером».
DAYPART_HEADING = {"morning": "утро", "afternoon": "день", "evening": "вечер"}

WEEKDAYS_SHORT = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]

MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря"]


# ------------------------------------------------------------------
# Мелкая вёрстка
# ------------------------------------------------------------------

def who(task: Dict[str, Any]) -> str:
    """
    Имя исполнителя или пустая строка для общих дел.

    Раньше возвращалось «дом». Смысла в этом оказалось мало: строка
    «дом — косить траву» ничего не добавляет к «косить траву», а в
    сводке из десяти дел половина начиналась одинаково и мешала читать.
    Отсутствие имени и так означает, что дело общее.
    """
    return ASSIGNEE_RU.get(task.get("assignee")) or ""


def name_prefix(task: Dict[str, Any], sep: str = " — ") -> str:
    """
    Имя с разделителем или пустая строка.

    Нужен, чтобы у общих дел не оставалось висящее тире: без имени
    строка начинается сразу с названия.
    """
    name = who(task)
    return f"{name}{sep}" if name else ""


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
    return f"{prefix}{name_prefix(t)}{escape(t['title'])}"


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

    Ежедневные пропускаются. Причина та же, по которой у них нет галочки
    в списке привычек: complete_task() ставит done навсегда, механизма
    сброса по дням нет, и нажатие означало бы «удалить привычку»,
    а не «сделал сегодня».

    Раньше это было неважно — ежедневные нигде не встречались с кнопками.
    Теперь встречаются: напоминание в момент начала несёт ✅, и без этой
    проверки первое же нажатие тихо закрыло бы привычку навсегда.

    Если после отсева ничего не осталось, клавиатуры нет вовсе.
    """
    if not tasks or len(tasks) > 8:
        return None
    rows = []
    for t in tasks:
        if t.get("list") == "daily":
            continue
        tl = time_label(t)
        label = (f"{tl} {name_prefix(t, ' · ')}{t['title']}" if tl
                 else f"{name_prefix(t, ' · ')}{t['title']}")
        # Telegram обрезает длинные ярлыки по-своему, лучше сделать это
        # самим — так видно, что текст сокращён.
        if len(label) > 30:
            label = label[:29] + "…"
        rows.append([
            InlineKeyboardButton(f"✅ {label}", callback_data=f"done:{t['id']}"),
            InlineKeyboardButton("⏰", callback_data=f"post:{t['id']}"),
        ])
    return InlineKeyboardMarkup(rows) if rows else None


def period_buttons(tasks: List[Dict[str, Any]],
                   today: date) -> Optional[InlineKeyboardMarkup]:
    """
    То же, что task_buttons, но с днём в ярлыке.

    Без дня «Сева · теннис» во вторник и в четверг выглядят одинаково,
    и человек закроет не ту. В дневной сводке этой проблемы нет — там
    день один на всё сообщение.

    Ежедневные пропускаются — как и в task_buttons, см. пояснение там.
    Если после отсева ничего не осталось, клавиатуры нет вовсе.
    """
    if not tasks or len(tasks) > 8:
        return None
    rows = []
    for t in tasks:
        if t.get("list") == "daily":
            continue
        parts = [day_label(t.get("date"), today), time_label(t),
                 f"{name_prefix(t, ' · ')}{t['title']}"]
        label = " ".join(x for x in parts if x)
        if len(label) > 30:
            label = label[:29] + "…"
        rows.append([
            InlineKeyboardButton(f"✅ {label}", callback_data=f"done:{t['id']}"),
            InlineKeyboardButton("⏰", callback_data=f"post:{t['id']}"),
        ])
    return InlineKeyboardMarkup(rows) if rows else None


def daily_buttons(tasks: List[Dict[str, Any]]) -> Optional[InlineKeyboardMarkup]:
    """
    Только отмена. Галочки для ежедневных нет намеренно.

    complete_task() ставит статус done навсегда, а механизма ежедневного
    сброса в проекте пока нет: одна строка в tasks служит и правилом,
    и экземпляром. Галочка здесь означала бы «удалить привычку»,
    а не «сделал сегодня» — до появления отметок по дням её быть не должно.

    Переноса тоже нет: ежедневные не переносятся по решению из README.
    """
    if not tasks or len(tasks) > 8:
        return None
    rows = []
    for t in tasks:
        label = f"{name_prefix(t, ' · ')}{t['title']}"
        if len(label) > 28:
            label = label[:27] + "…"
        rows.append([
            InlineKeyboardButton(f"🗑 {label}", callback_data=f"cancel:{t['id']}")
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


def postpone_options(task_id: int,
                     task: Optional[Dict[str, Any]] = None
                     ) -> InlineKeyboardMarkup:
    """
    Второй уровень: куда переносим. Показывается после нажатия ⏰.

    Первая строка называет задачу. Без неё меню висит под всей сводкой
    и выглядит как общие кнопки неизвестно к чему: в месячной сводке
    два десятка задач, а «Завтра» относится к одной из них.
    """
    rows = []
    if task:
        title = f"{name_prefix(task, ' · ')}{task['title']}"
        if len(title) > 30:
            title = title[:29] + "…"
        rows.append([InlineKeyboardButton(f"↓ переношу: {title}",
                                          callback_data="noop")])
    rows += [
        [InlineKeyboardButton("Завтра", callback_data=f"pto:{task_id}:1"),
         InlineKeyboardButton("Послезавтра", callback_data=f"pto:{task_id}:2")],
        [InlineKeyboardButton("Через неделю", callback_data=f"pto:{task_id}:7"),
         InlineKeyboardButton("Убрать дату", callback_data=f"pto:{task_id}:x")],
        [InlineKeyboardButton("Отменить задачу", callback_data=f"cancel:{task_id}"),
         InlineKeyboardButton("← Назад", callback_data="back")],
    ]
    return InlineKeyboardMarkup(rows)


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
            lines.append(f"{mark} дедлайн: {name_prefix(t)}{escape(t['title'])}")

    if overdue:
        lines.append("")
        lines.append("🔴 <b>Просрочено:</b>")
        for t in overdue:
            lines.append(f"  {name_prefix(t)}{escape(t['title'])} "
                         f"({day_label(t['date'], today)})")

    if daily:
        lines.append("")
        lines.append("☑️ Ежедневно: " + " · ".join(
            f"{name_prefix(t, ' ')}{escape(t['title'])}" for t in daily))

    return "\n".join(lines), task_buttons(today_tasks)


def build_evening(today: date) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    """Вечерняя сверка: что осталось незакрытым."""
    left = tasks_for_date(today)
    backlog = tasks_backlog(today)
    daily = tasks_daily()

    lines = [f"🌙 <b>Итоги дня, {today.day} {MONTHS_RU[today.month - 1]}</b>", ""]

    if left:
        lines.append("Не отмечено сегодня:")
        lines += [f"  {name_prefix(t)}{escape(t['title'])}" for t in left]
    else:
        lines.append("Всё на сегодня закрыто. ✅")

    if daily:
        lines.append("")
        lines.append("Ежедневные: " + " · ".join(
            f"{name_prefix(t, ' ')}{escape(t['title'])}" for t in daily))

    if backlog:
        lines.append("")
        lines.append("📌 <b>Отдельные дела:</b>")
        for t in backlog:
            row = f"  {name_prefix(t)}{escape(t['title'])}"
            if t.get("deadline"):
                row += f"  ⏳ до {day_label(t['deadline'], today)}"
            # Счётчик виден с четвёртого переноса: задача, которую двигают
            # месяц, скорее всего не будет сделана никогда.
            if (t.get("postponed_count") or 0) >= 4:
                row += f"  ⚠️×{t['postponed_count']}"
            lines.append(row)

    return "\n".join(lines), task_buttons(left)


def _build_period(today: date, days: int, icon: str, heading: str
                  ) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    """
    Общая вёрстка для сводок на неделю и на месяц. Различаются только
    длиной отрезка и заголовком, поэтому тело одно: два почти одинаковых
    цикла разъехались бы при первой же правке.

    Сознательно короче дневной сводки: только день, время, кто и что.
    Дедлайны, просроченное и ежедневные сюда не попадают — они
    одинаковы каждый день, и тридцать повторов сделали бы сообщение
    нечитаемым. За ними идти в дневную сводку.

    Пустые дни пропускаются: строка «ЧТ 11.09 — ничего» занимает место
    и ничего не сообщает.
    """
    last = today + timedelta(days=days - 1)
    tasks = tasks_for_range(today, last)

    head = (f"{icon} <b>{heading}: {today.day} {MONTHS_RU[today.month - 1]} — "
            f"{last.day} {MONTHS_RU[last.month - 1]}</b>")

    if not tasks:
        return f"{head}\n\nНичего не запланировано.", None

    lines = [head]
    current: Optional[date] = None
    for t in tasks:
        if t["date"] != current:
            current = t["date"]
            # Заголовок дня собирается здесь, а не через day_label():
            # та для дальних дат уже включает число, и на месячной
            # сводке получалось «ВТ 06.10 06.10».
            delta = (current - today).days
            dm = current.strftime("%d.%m")
            if delta == 0:
                label = f"сегодня {dm}"
            elif delta == 1:
                label = f"завтра {dm}"
            else:
                label = f"{WEEKDAYS_SHORT[current.weekday()]} {dm}"
            lines.append("")
            lines.append(f"<b>{label}</b>")
        lines.append(f"  {task_line(t)}")

    return "\n".join(lines), period_buttons(tasks, today)


def build_week(today: date) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    """Неделя вперёд, сгруппированная по дням."""
    return _build_period(today, 7, "📅", "Неделя")


def build_month(today: date) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    """
    Месяц вперёд. Значок отличается от недельного намеренно: bot.py
    определяет по нему, какую сводку перерисовать после нажатия кнопки.
    Одинаковые значки означали бы, что месяц после галочки схлопнется
    в неделю.
    """
    return _build_period(today, 30, "🗓", "Месяц")


def build_backlog(today: date) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    """
    Отдельные дела — те, у которых нет даты.

    В сводки по дням они не попадают по определению, поэтому до сих пор
    их было видно только вечером в 21:00.
    """
    backlog = tasks_backlog(today)

    if not backlog:
        return "📌 <b>Отдельные дела</b>\n\nПусто.", None

    lines = ["📌 <b>Отдельные дела</b>", ""]
    for t in backlog:
        row = f"  {name_prefix(t)}{escape(t['title'])}"
        if t.get("deadline"):
            row += f"  ⏳ до {day_label(t['deadline'], today)}"
        if (t.get("postponed_count") or 0) >= 4:
            row += f"  ⚠️×{t['postponed_count']}"
        lines.append(row)

    return "\n".join(lines), task_buttons(backlog)


def build_all(today: date) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    """
    Всё сразу: месяц по дням, отдельные дела, ежедневные.

    Отличается от месячной сводки тем, что не отбрасывает списки без
    даты. Месячная показывает только запланированное по дням — отдельные
    дела и привычки в неё не попадают по определению, и человеку
    приходится спрашивать три раза.

    Кнопок нет намеренно. Задач тут обычно больше восьми, а частичная
    клавиатура (кнопки к первым восьми из двадцати) хуже, чем никакой:
    непонятно, почему у одних дел кнопка есть, а у других нет.
    Закрывать и переносить — из дневной или недельной сводки.
    """
    last = today + timedelta(days=29)
    scheduled = tasks_for_range(today, last)
    overdue = tasks_overdue(today)
    backlog = tasks_backlog(today)
    daily = tasks_daily()

    lines = [f"🧾 <b>Всё сразу: {today.day} {MONTHS_RU[today.month - 1]} — "
             f"{last.day} {MONTHS_RU[last.month - 1]}</b>"]

    if overdue:
        lines += ["", "🔴 <b>Просрочено:</b>"]
        lines += [f"  {day_label(t['date'], today)} {name_prefix(t)}"
                  f"{escape(t['title'])}" for t in overdue]

    if scheduled:
        current = None
        for t in scheduled:
            if t["date"] != current:
                current = t["date"]
                lines += ["", f"<b>{day_label(current, today)}</b>"]
            tl = time_label(t)
            lines.append(f"  {tl + '  ' if tl else ''}"
                         f"{name_prefix(t)}{escape(t['title'])}")
    else:
        lines += ["", "На ближайший месяц ничего не запланировано."]

    if backlog:
        lines += ["", "📌 <b>Отдельные дела:</b>"]
        for t in backlog:
            row = f"  {name_prefix(t)}{escape(t['title'])}"
            if t.get("deadline"):
                row += f"  ⏳ до {day_label(t['deadline'], today)}"
            lines.append(row)

    if daily:
        lines += ["", "🔁 <b>Ежедневные:</b>"]
        for t in daily:
            tl = time_label(t)
            lines.append(f"  {tl + '  ' if tl else ''}"
                         f"{name_prefix(t)}{escape(t['title'])}")

    return "\n".join(lines), None


def build_daily() -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    """
    Ежедневные дела. Кнопка одна — убрать привычку, когда стала не нужна.

    Аргумент today не нужен: у ежедневных нет даты.
    """
    daily = tasks_daily()

    if not daily:
        return ("🔁 <b>Ежедневные дела</b>\n\nПусто.\n"
                "Чтобы завести, напиши «Севе читать 20 минут каждый день».",
                None)

    lines = ["🔁 <b>Ежедневные дела</b>", ""]
    lines += [f"  {name_prefix(t)}{escape(t['title'])}" for t in daily]
    lines.append("")
    lines.append("<i>Кнопка убирает дело насовсем.</i>")

    return "\n".join(lines), daily_buttons(daily)


def build_help() -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    """
    Короткая инструкция для того, кто зашёл в чат впервые.

    Живёт здесь, а не в bot.py, потому что нужна двоим: команде
    «инструкция» и обработчику /start. В двух местах текст однажды
    разъехался бы.

    Сознательно коротко. Подробности — в README проекта; человеку
    в чате нужно понять за полминуты, что писать.
    """
    text = (
        "🤖 <b>Семейный помощник</b>\n"
        "\n"
        "Пиши обычными фразами, команды со слэшем не нужны.\n"
        "\n"
        "<b>Записать дело</b>\n"
        "  Севе теннис во вторник в 3\n"
        "  купить цемент до пятницы\n"
        "  Глебу зарядка каждый день\n"
        "\n"
        "<b>Закрыть, перенести, отменить</b>\n"
        "  сделал уроки\n"
        "  перенеси теннис на четверг\n"
        "  отмени тренировку\n"
        "\n"
        "<b>Посмотреть</b>\n"
        "  Сводка дня · Сводка недели · Сводка месяца\n"
        "  Список дел — то, что без даты\n"
        "  Ежедневные — привычки\n"
        "\n"
        "<b>Про время</b>\n"
        "Числа читаются как дневные: «в 3» это 15:00.\n"
        "Нужно утро — скажи словом: «в 7 утра», «утром в 8».\n"
        "\n"
        "Имя в начале — задача на человека, без имени — общая.\n"
        "Сводки приходят сами в 08:00 и 21:00."
    )
    return text, None


# Подписи ступеней напоминания.
#
# Текст фиксированный, а не посчитанный из разницы во времени. Раньше
# считался: планировщик просыпался по своему циклу, в окно попадал
# на несколько десятков секунд позже ровной отметки, остаток округлялся
# вниз — и вместо «через 10 мин» приходило «через 9 мин».
#
# Отдельный значок у последней ступени не для красоты: только под ней
# есть кнопки, и глазом это должно быть видно до нажатия.
REMINDER_LABELS = {
    "lead_60": "⏰ через 1 час",
    "lead_30": "⏰ через 30 минут",
    "lead_10": "⏰ через 10 минут",
    "start":   "🔔 сейчас",
}


def build_reminder(task: Dict[str, Any], stage: str
                   ) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    """
    Напоминание одной ступени.

    Кнопки — только у ступени 'start'. До начала события отмечать
    «сделано» нечего, а случайное нажатие закрывает задачу молча:
    напоминаний по ней больше не придёт, и человек узнает об этом,
    когда событие уже прошло.
    """
    label = REMINDER_LABELS[stage]
    text = (f"{label}: {name_prefix(task)}"
            f"<b>{escape(task['title'])}</b>  ({time_label(task)})")
    return text, (task_buttons([task]) if stage == "start" else None)
