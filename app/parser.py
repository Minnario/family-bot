"""
Парсер сообщений Family Assistant.

Обёртка вокруг логики, отлаженной в parser/run_eval.py, оформленная
как функция для бота. Промпт, схема инструмента и настройки модели —
те же самые, чтобы измеренная точность 22/22 относилась к тому же коду,
который поедет в прод.

Проверка из командной строки:
    python3 app/parser.py "Севе теннис во вторник в 3"
"""

import json
import os
import sys
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from anthropic import Anthropic
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

PROMPT_PATH = ROOT / "parser" / "parser_prompt.md"

MODEL = "claude-haiku-4-5-20251001"
TZ = ZoneInfo("Europe/Dublin")

WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг",
               "пятница", "суббота", "воскресенье"]

# ------------------------------------------------------------------
# Схема инструмента — копия проверенной eval'ом
# ------------------------------------------------------------------

TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "title":      {"type": "string",
                       "description": "Название без имени, даты и времени"},
        "assignee":   {"type": ["string", "null"],
                       "enum": ["seva", "gleb", "kamilla", "vova", "sasha", None]},
        "list":       {"type": "string", "enum": ["scheduled", "backlog", "daily"]},
        "date":       {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "deadline":   {"type": ["string", "null"],
                       "description": "YYYY-MM-DD, для 'до субботы'"},
        "time_mode":  {"type": "string",
                       "enum": ["exact", "range", "daypart", "allday", "undated"]},
        "time_start": {"type": ["string", "null"], "description": "HH:MM"},
        "time_end":   {"type": ["string", "null"], "description": "HH:MM"},
        "daypart":    {"type": ["string", "null"],
                       "enum": ["morning", "afternoon", "evening", None]},
        "recurring":  {"type": "boolean"},
        "reminder_lead": {"type": "integer",
                          "description": "минут до начала, по умолчанию 10"},
    },
    "required": ["title", "assignee", "list", "date", "deadline",
                 "time_mode", "time_start", "time_end", "daypart",
                 "recurring", "reminder_lead"],
}

TOOL = {
    "name": "parse_message",
    "description": "Разобрать сообщение семейного планировщика в структуру.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "complete", "reschedule",
                         "cancel", "pause", "clarify"],
            },
            "tasks": {
                "type": "array", "items": TASK_SCHEMA,
                "description": "Только для action=create. "
                               "Два имени в одной фразе = два элемента.",
            },
            "task_id":   {"type": ["integer", "null"],
                          "description": "Для complete / reschedule / cancel"},
            "new_date":  {"type": ["string", "null"], "description": "Для reschedule"},
            "keep_time": {"type": ["boolean", "null"],
                          "description": "Для reschedule: сохранить прежнее время"},
            "clarification": {
                "type": ["object", "null"],
                "properties": {
                    "question": {"type": "string"},
                    "options":  {"type": "array", "items": {"type": "string"}},
                },
                "description": "Только для action=clarify",
            },
        },
        "required": ["action"],
    },
}


class ParseError(Exception):
    """Парсер не смог вернуть структуру. Бот должен попросить переформулировать."""


def _json_safe(value: Any) -> Any:
    """
    Из базы приходят объекты date и time, а json.dumps их не умеет.
    Приводим к строкам того же формата, который ждёт промпт.
    """
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, time):
        return value.strftime("%H:%M")
    return value


def _open_tasks_block(open_tasks: Optional[List[Dict[str, Any]]]) -> str:
    """Открытые задачи в промпт — чтобы парсер мог сопоставить с ними команду."""
    if not open_tasks:
        return "(нет открытых задач)"
    rows = [{k: _json_safe(v) for k, v in t.items()} for t in open_tasks]
    return json.dumps(rows, ensure_ascii=False, indent=2)


def _build_system(open_tasks, today: date) -> List[Dict[str, Any]]:
    """
    Системный промпт двумя блоками.

    Первый — статичные правила, помечены cache_control: они не меняются
    между запросами, поэтому читаются из кеша по цене 10% от обычного
    ввода. Второй — дата и открытые задачи, они меняются каждый раз.

    Порядок обязателен: кешируется префикс, поэтому изменяемая часть
    идёт ПОСЛЕ статичной. При обратном порядке кеш не сработает ни разу.
    """
    rules = PROMPT_PATH.read_text(encoding="utf-8")
    dynamic = (
        f"## КОНТЕКСТ\n\n"
        f"Сегодня: {today.isoformat()} ({WEEKDAYS_RU[today.weekday()]})\n"
        f"Часовой пояс: Europe/Dublin\n\n"
        f"Открытые задачи в базе:\n{_open_tasks_block(open_tasks)}"
    )
    return [
        {"type": "text", "text": rules,
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": dynamic},
    ]


def parse(text: str,
          open_tasks: Optional[List[Dict[str, Any]]] = None,
          today: Optional[date] = None,
          client: Optional[Anthropic] = None) -> Dict[str, Any]:
    """
    Разбирает фразу в структуру.

    Возвращает словарь с результатом разбора плюс ключ "_usage"
    с расходом токенов — он идёт в message_log, чтобы учёт стоимости
    был фактическим, а не оценочным.

    Бросает ParseError, если модель не вернула структуру.
    """
    if today is None:
        # Дата по Дублину, а не по времени сервера. На Oracle сервер
        # будет в UTC, и после полуночи UTC "сегодня" разъедется с
        # реальным днём семьи.
        today = datetime.now(TZ).date()

    client = client or Anthropic()

    resp = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        # 0 вместо дефолтной 1.0. Задача — извлечение по правилам.
        # Именно это убрало разброс между прогонами в eval'е.
        temperature=0,
        system=_build_system(open_tasks, today),
        tools=[TOOL],
        tool_choice={"type": "tool", "name": "parse_message"},
        messages=[{"role": "user", "content": text}],
    )

    # Обрезанный по max_tokens tool_use приходит неполным и выглядит
    # как обычный ответ. Тот самый тихий сбой — проверяем явно.
    if resp.stop_reason == "max_tokens":
        raise ParseError("ответ обрезан по max_tokens")

    block = next((b for b in resp.content if b.type == "tool_use"), None)
    if block is None:
        raise ParseError(f"инструмент не вызван (stop_reason={resp.stop_reason})")

    u = resp.usage
    result = dict(block.input)
    result["_usage"] = {
        "model": MODEL,
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }
    return result


# ------------------------------------------------------------------
# Проверка из командной строки
# ------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit('Использование: python3 app/parser.py "фраза"')

    phrase = " ".join(sys.argv[1:])

    # Открытые задачи берём из базы — так же, как будет делать бот.
    try:
        from db import list_open_tasks
    except ImportError:
        sys.path.insert(0, str(Path(__file__).parent))
        from db import list_open_tasks

    try:
        open_tasks = list_open_tasks()
    except Exception as exc:
        print(f"[!] база недоступна ({exc}), разбираем без контекста\n")
        open_tasks = []

    print(f"Фраза : {phrase}")
    print(f"Сегодня: {datetime.now(TZ).date().isoformat()}")
    print(f"Открытых задач в базе: {len(open_tasks)}\n")

    result = parse(phrase, open_tasks)
    usage = result.pop("_usage")

    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\nТокены: {usage['input_tokens']} in / {usage['output_tokens']} out"
          f"  (кеш: {usage['cache_read']} прочитано)")
