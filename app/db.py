"""
Слой доступа к базе данных Family Assistant.

Единственное место в проекте, где пишется SQL. Всё остальное вызывает
эти функции. Смысл разделения: когда запрос окажется неверным, искать
его нужно будет здесь, а не по всему коду.

Самопроверка:
    python3 app/db.py
"""

import json
import os
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

DSN = os.environ.get("DATABASE_URL")


def connect():
    """
    Открывает соединение с базой.

    Используется как контекстный менеджер:

        with connect() as conn:
            ...

    На выходе из блока psycopg сам делает COMMIT, а при исключении —
    ROLLBACK. Это и есть транзакция: либо все изменения внутри блока
    применились, либо ни одного. Наполовину записанного состояния
    не бывает.

    row_factory=dict_row заставляет запросы возвращать словари
    ({'id': 1, 'title': '...'}) вместо кортежей ((1, '...')).
    Обращение по имени поля переживает изменение порядка колонок,
    обращение по индексу — нет.
    """
    if not DSN:
        raise RuntimeError(
            f"Нет DATABASE_URL.\n"
            f"Добавь в {ROOT / '.env'} строку:\n"
            f"  DATABASE_URL=postgresql://ПОЛЬЗОВАТЕЛЬ@localhost/familybot"
        )
    return psycopg.connect(DSN, row_factory=dict_row)


# ------------------------------------------------------------------
# Запись
# ------------------------------------------------------------------

def create_task(task: Dict[str, Any]) -> int:
    """
    Записывает одну задачу. На вход — словарь ровно в том виде,
    в каком его возвращает парсер. Возвращает id созданной строки.

    carry_over вычисляется здесь, а не приходит от модели: ежедневные
    задачи не переносятся (не сделал — сгорело), все остальные переносятся.
    Это правило системы, и решать его должен код, а не LLM.
    """
    carry_over = task.get("list") != "daily"

    sql = """
        INSERT INTO tasks (
            title, assignee, list, template_id,
            date, deadline,
            time_mode, time_start, time_end, daypart,
            reminder_lead, carry_over
        )
        VALUES (
            %(title)s, %(assignee)s, %(list)s, %(template_id)s,
            %(date)s, %(deadline)s,
            %(time_mode)s, %(time_start)s, %(time_end)s, %(daypart)s,
            %(reminder_lead)s, %(carry_over)s
        )
        RETURNING id
    """

    # Значения передаются ОТДЕЛЬНО от текста запроса. psycopg сам их
    # экранирует. Склеивать SQL через f-строку нельзя: задача с
    # апострофом в названии сломает запрос, а враждебный текст
    # выполнит чужие команды (SQL-инъекция).
    params = {
        "title":         task["title"],
        "assignee":      task.get("assignee"),
        "list":          task["list"],
        "template_id":   task.get("template_id"),
        "date":          task.get("date"),
        "deadline":      task.get("deadline"),
        "time_mode":     task["time_mode"],
        "time_start":    task.get("time_start"),
        "time_end":      task.get("time_end"),
        "daypart":       task.get("daypart"),
        "reminder_lead": task.get("reminder_lead", 10),
        "carry_over":    carry_over,
    }

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()["id"]


def create_tasks(tasks: List[Dict[str, Any]]) -> List[int]:
    """
    Несколько задач одной транзакцией.

    Нужно для фразы с двумя именами («пилатес Саша Вова» = две задачи).
    Важно, что это ОДНА транзакция: если вторая вставка упадёт,
    первая тоже откатится. Иначе половина фразы попадёт в базу,
    а человек получит подтверждение как будто записалось всё.
    """
    ids = []
    with connect() as conn:
        with conn.cursor() as cur:
            for t in tasks:
                carry_over = t.get("list") != "daily"
                cur.execute(
                    """
                    INSERT INTO tasks (
                        title, assignee, list, date, deadline,
                        time_mode, time_start, time_end, daypart,
                        reminder_lead, carry_over
                    ) VALUES (
                        %(title)s, %(assignee)s, %(list)s, %(date)s, %(deadline)s,
                        %(time_mode)s, %(time_start)s, %(time_end)s, %(daypart)s,
                        %(reminder_lead)s, %(carry_over)s
                    ) RETURNING id
                    """,
                    {
                        "title":         t["title"],
                        "assignee":      t.get("assignee"),
                        "list":          t["list"],
                        "date":          t.get("date"),
                        "deadline":      t.get("deadline"),
                        "time_mode":     t["time_mode"],
                        "time_start":    t.get("time_start"),
                        "time_end":      t.get("time_end"),
                        "daypart":       t.get("daypart"),
                        "reminder_lead": t.get("reminder_lead", 10),
                        "carry_over":    carry_over,
                    },
                )
                ids.append(cur.fetchone()["id"])
    return ids


# ------------------------------------------------------------------
# Чтение
# ------------------------------------------------------------------

def list_open_tasks() -> List[Dict[str, Any]]:
    """
    Все незакрытые задачи. Уходит в промпт парсера, чтобы он мог
    сопоставить «купил цемент» с конкретной строкой в базе.

    Порядок: сначала с датами (ближайшие раньше), потом бессрочные.
    NULLS LAST нужен явно — по умолчанию Postgres при ASC ставит
    NULL в конец, но полагаться на умолчание не стоит.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, title, assignee, list, date, deadline,
                       time_mode, time_start, time_end, daypart
                  FROM tasks
                 WHERE status = 'pending'
                 ORDER BY date ASC NULLS LAST, time_start ASC NULLS LAST, id
            """)
            return cur.fetchall()


def get_task(task_id: int) -> Optional[Dict[str, Any]]:
    """Одна задача по номеру. None, если такой нет."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tasks WHERE id = %s", (task_id,))
            return cur.fetchone()


def tasks_for_date(day: date) -> List[Dict[str, Any]]:
    """Задачи на конкретный день. Основа утренней сводки."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, title, assignee, time_mode,
                       time_start, time_end, daypart
                  FROM tasks
                 WHERE date = %s AND status = 'pending'
                 ORDER BY time_start ASC NULLS LAST, id
            """, (day,))
            return cur.fetchall()


# ------------------------------------------------------------------
# Журнал сообщений
# ------------------------------------------------------------------

def log_message(chat_id: int, raw_text: str, parsed: Optional[dict],
                parse_ok: bool = True, model: Optional[str] = None,
                input_tokens: Optional[int] = None,
                output_tokens: Optional[int] = None,
                message_id: Optional[int] = None) -> int:
    """
    Сохраняет сырую фразу и то, как её разобрал парсер.

    Две цели: разбирать ошибки парсера постфактум и пополнять
    eval-датасет реальными фразами вместо придуманных.

    json.dumps с ensure_ascii=False — иначе кириллица в базе
    превратится в \\u0442\\u0435\\u0441\\u0442 и станет нечитаемой.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO message_log
                    (chat_id, message_id, raw_text, parsed, parse_ok,
                     model, input_tokens, output_tokens)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                chat_id, message_id, raw_text,
                json.dumps(parsed, ensure_ascii=False) if parsed else None,
                parse_ok, model, input_tokens, output_tokens,
            ))
            return cur.fetchone()["id"]


# ------------------------------------------------------------------
# Самопроверка
# ------------------------------------------------------------------

if __name__ == "__main__":
    print("Подключение...", end=" ")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version()")
            v = cur.fetchone()["version"].split(",")[0]
    print("ок:", v)

    print("\nЗапись задачи с точным временем...", end=" ")
    tid = create_task({
        "title": "тестовая тренировка",
        "assignee": "seva",
        "list": "scheduled",
        "date": "2026-09-08",
        "deadline": None,
        "time_mode": "exact",
        "time_start": "15:00",
        "time_end": None,
        "daypart": None,
        "reminder_lead": 10,
    })
    print(f"id={tid}")

    print("Запись двух задач одной транзакцией...", end=" ")
    ids = create_tasks([
        {"title": "пилатес", "assignee": "sasha", "list": "scheduled",
         "date": "2026-09-04", "time_mode": "daypart", "daypart": "morning"},
        {"title": "пилатес", "assignee": "vova", "list": "scheduled",
         "date": "2026-09-04", "time_mode": "daypart", "daypart": "morning"},
    ])
    print(f"ids={ids}")

    print("Запись в бэклог...", end=" ")
    bid = create_task({
        "title": "подстричь газон",
        "assignee": None,
        "list": "backlog",
        "date": None,
        "time_mode": "undated",
        "reminder_lead": 10,
    })
    print(f"id={bid}")

    print("\nОткрытые задачи:")
    for t in list_open_tasks():
        when = t["date"] or "без даты"
        tm = t["time_start"] or t["daypart"] or ""
        who = t["assignee"] or "дом"
        print(f"  #{t['id']:<3} {str(when):<12} {str(tm):<10} {who:<8} {t['title']}")

    print("\nПроверка защиты (ожидается отказ базы)...", end=" ")
    try:
        create_task({
            "title": "сломанная", "assignee": None,
            "list": "scheduled",     # scheduled требует дату
            "date": None,            # а её нет
            "time_mode": "allday", "reminder_lead": 10,
        })
        print("ПРОВАЛ — база приняла противоречивую строку!")
    except psycopg.errors.CheckViolation as e:
        # Имя ограничения берём из структурированной диагностики, а не
        # разбором текста ошибки: там первым идёт имя таблицы, и парсинг
        # по кавычкам давал "tasks" вместо "list_matches_date".
        name = e.diag.constraint_name or "?"
        print(f"ок, отвергнуто ({name})")

    print("\nЖурнал сообщений...", end=" ")
    lid = log_message(
        chat_id=-1001234567890,
        raw_text="Севе теннис во вторник в 3",
        parsed={"action": "create", "tasks": [{"title": "теннис"}]},
        model="claude-haiku-4-5-20251001",
        input_tokens=4600, output_tokens=150,
    )
    print(f"id={lid}")

    print("\nГотово. Удалить тестовые данные:")
    print("  psql -d familybot -c 'TRUNCATE tasks, message_log RESTART IDENTITY CASCADE;'")
