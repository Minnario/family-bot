"""
Обработчик сообщений: соединяет парсер и базу.

Единственная точка входа — handle_message(). Бот будет вызывать её и
отправлять полученную строку в чат. Вся логика «что делать с разобранной
фразой» живёт здесь, а не в коде Telegram: когда понадобится другой
канал ввода, менять придётся только его.

Проверка из командной строки:
    python3 app/handle.py "Севе теннис во вторник в 3"
"""

import sys
from collections import namedtuple
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))

from db import (cancel_task, complete_task, create_tasks, get_task,
                list_open_tasks, log_message, reschedule_task)
from parser import ParseError, parse, TZ
# Константы и вёрстка живут в ui.py: их используют и подтверждения,
# и сводки. Держать имена членов семьи в двух файлах — гарантия того,
# что однажды они разъедутся.
from ui import ASSIGNEE_RU, DAYPART_RU, WEEKDAYS_SHORT, day_label

# Тестовый chat_id для запусков из командной строки.
# У настоящих групп Telegram он отрицательный и приходит из апдейта.
CLI_CHAT_ID = 0

# Ответ обработчика: текст плюс варианты переспроса, если они есть.
# Раньше возвращалась просто строка, но боту нужно знать, надо ли
# рисовать кнопки — из текста это не вытащить.
Reply = namedtuple("Reply", ["text", "options"])

# ------------------------------------------------------------------
# Форматирование подтверждения
# ------------------------------------------------------------------

def _fmt_day(value: Optional[str], today: date) -> str:
    """
    Дата человеческим языком. «завтра» понятнее чем «2026-09-04»,
    а «ВТ 08.09» понятнее чем «2026-09-08».
    """
    if not value:
        return ""
    d = datetime.strptime(value, "%Y-%m-%d").date()
    delta = (d - today).days
    if delta == 0:
        return "сегодня"
    if delta == 1:
        return "завтра"
    if 2 <= delta <= 6:
        return WEEKDAYS_SHORT[d.weekday()]
    return f"{WEEKDAYS_SHORT[d.weekday()]} {d.strftime('%d.%m')}"


def _fmt_time(task: Dict[str, Any]) -> str:
    """Время в том же режиме, в каком его назвал человек."""
    mode = task.get("time_mode")
    if mode == "exact":
        return task.get("time_start") or ""
    if mode == "range":
        return f"{task.get('time_start')}–{task.get('time_end')}"
    if mode == "daypart":
        return DAYPART_RU.get(task.get("daypart"), "")
    return ""


def format_task(task: Dict[str, Any], today: date) -> str:
    """
    Одна строка подтверждения: «Сева — теннис, ВТ 15:00».

    Основная часть собирается через запятую, служебные пометки
    приписываются через пробел — иначе получается «газон, (отдельные дела)»
    с запятой перед скобкой.
    """
    main = []

    who = ASSIGNEE_RU.get(task.get("assignee"))
    main.append(f"{who} — {task['title']}" if who else task["title"])

    when = " ".join(x for x in (_fmt_day(task.get("date"), today),
                                _fmt_time(task)) if x)
    if when:
        main.append(when)

    if task.get("deadline"):
        main.append(f"⏳ до {_fmt_day(task['deadline'], today)}")

    line = ", ".join(main)

    # Пометка о списке. Для ежедневных она же означает повтор,
    # поэтому отдельный маркер «повторяется» не добавляем — иначе
    # выходит «(ежедневно) · повторяется» об одном и том же.
    if task.get("list") == "daily":
        line += "  (ежедневно)"
    elif task.get("list") == "backlog" and not task.get("deadline"):
        line += "  (отдельные дела)"
    elif task.get("recurring"):
        line += "  (каждую неделю)"

    return line


# ------------------------------------------------------------------
# Обработка
# ------------------------------------------------------------------

def handle_message(text: str,
                   chat_id: int = CLI_CHAT_ID,
                   message_id: Optional[int] = None) -> str:
    """
    Принимает фразу, возвращает текст ответа для чата.

    Всегда пишет в message_log — и при успехе, и при провале разбора.
    Журнал нужен, чтобы потом разбирать ошибки парсера и пополнять
    eval-датасет реальными фразами вместо придуманных.
    """
    today = datetime.now(TZ).date()
    open_tasks = list_open_tasks()

    # --- разбор ---
    try:
        result = parse(text, open_tasks, today)
    except ParseError as exc:
        log_message(chat_id, text, None, parse_ok=False, message_id=message_id)
        return Reply(f"Не понял. Переформулируй, пожалуйста.\n({exc})", None)

    usage = result.pop("_usage", {})
    log_message(
        chat_id=chat_id,
        message_id=message_id,
        raw_text=text,
        parsed=result,
        parse_ok=True,
        model=usage.get("model"),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
    )

    action = result.get("action")

    # --- переспрос ---
    # Пока текстом. Когда появится Telegram, здесь будут кнопки.
    if action == "clarify":
        c = result.get("clarification") or {}
        q = c.get("question", "Уточни, пожалуйста")
        opts = c.get("options") or []
        return Reply(q, opts)

    # --- создание ---
    if action == "create":
        tasks = result.get("tasks") or []
        if not tasks:
            return Reply("Не понял, что записать. Переформулируй.", None)

        # Одна транзакция: если вторая задача не пройдёт проверки базы,
        # первая тоже откатится. Иначе человек получит подтверждение
        # на две задачи, а в базе окажется одна.
        try:
            ids = create_tasks(tasks)
        except Exception as exc:
            return Reply(f"Не смог записать: {exc}", None)

        lines = [f"Принял: {format_task(t, today)}  #{i}"
                 for t, i in zip(tasks, ids)]
        return Reply("\n".join(lines), None)

    # --- закрытие ---
    if action == "complete":
        tid = result.get("task_id")
        if not tid:
            return Reply("Не понял, какую задачу закрыть. Назови её точнее.", None)
        task = complete_task(tid)
        if not task:
            return Reply("Такой открытой задачи нет — возможно, уже закрыта.", None)
        return Reply(f"✅ Закрыл: {ASSIGNEE_RU.get(task['assignee']) or 'дом'} — {task['title']}", None)

    # --- перенос ---
    if action == "reschedule":
        tid = result.get("task_id")
        if not tid:
            return Reply("Не понял, что переносить. Назови задачу точнее.", None)

        new_date = result.get("new_date")
        keep_time = result.get("keep_time")
        # По умолчанию время сохраняем: «перенеси теннис на четверг»
        # обычно значит тот же час, просто другой день.
        if keep_time is None:
            keep_time = True

        d = datetime.strptime(new_date, "%Y-%m-%d").date() if new_date else None
        task = reschedule_task(tid, d, keep_time)
        if not task:
            return Reply("Такой открытой задачи нет.", None)

        where = day_label(task["date"], today) if task["date"] else "отдельные дела"
        note = ""
        # Четвёртый перенос — сигнал, что задача не будет сделана.
        # Лучше спросить сейчас, чем возить её в списке ещё месяц.
        if (task.get("postponed_count") or 0) >= 4:
            note = (f"\n⚠️ Переносится {task['postponed_count']}-й раз. "
                    f"Может, отменить?")
        return Reply(f"⏰ Перенёс: {task['title']} → {where}{note}", None)

    # --- отмена ---
    if action == "cancel":
        tid = result.get("task_id")
        if not tid:
            return Reply("Не понял, что отменить.", None)
        task = cancel_task(tid)
        if not task:
            return Reply("Такой открытой задачи нет.", None)
        return Reply(f"🗑 Отменил: {task['title']}", None)

    # --- пока не реализовано ---
    # Честный ответ вместо молчания. Молчащий бот выглядит как сломанный,
    # и человек будет повторять фразу, думая что она не дошла.
    if action == "pause":
        return Reply("Понял (паузы), но это ещё не реализовано.\n"
                "Пока умею записывать, закрывать, переносить и отменять.", None)

    return Reply(f"Неизвестное действие: {action}", None)


# ------------------------------------------------------------------
# Проверка из командной строки
# ------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit('Использование: python3 app/handle.py "фраза"')

    phrase = " ".join(sys.argv[1:])
    print(f"> {phrase}\n")
    print(handle_message(phrase).text)
