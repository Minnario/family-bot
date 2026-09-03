#!/usr/bin/env python3
"""
Eval парсера Family Assistant.

Прогоняет тестовые фразы через несколько моделей, сравнивает результат
с эталоном и печатает точность + стоимость.

Запуск:
    export ANTHROPIC_API_KEY=sk-ant-...
    pip install anthropic
    python run_eval.py                 # все модели
    python run_eval.py --model haiku   # одна модель
    python run_eval.py --verbose       # показать все расхождения
"""

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

HERE = Path(__file__).parent

# Ключ берётся из .env рядом со скриптом. Экспортировать вручную не нужно.
load_dotenv(HERE / ".env")

# Ставки за 1M токенов (input, output), USD. Проверять на
# https://www.anthropic.com/pricing перед тем как доверять цифрам.
MODELS = {
    "haiku":  {"id": "claude-haiku-4-5-20251001", "in": 1.00, "out": 5.00},
    "sonnet": {"id": "claude-sonnet-5",           "in": 3.00, "out": 15.00},
    "opus":   {"id": "claude-opus-5",             "in": 5.00, "out": 25.00},
}

WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг",
               "пятница", "суббота", "воскресенье"]

# --------------------------------------------------------------------------
# Схема инструмента
# --------------------------------------------------------------------------

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
        "reminder_lead": {"type": "integer", "description": "минут до начала, по умолчанию 10"},
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
                "enum": ["create", "complete", "reschedule", "cancel", "pause", "clarify"],
            },
            "tasks": {
                "type": "array", "items": TASK_SCHEMA,
                "description": "Только для action=create. Два имени в одной фразе = два элемента.",
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

# --------------------------------------------------------------------------
# Сравнение
# --------------------------------------------------------------------------

TASK_FIELDS = ["title", "assignee", "list", "date", "deadline", "time_mode",
               "time_start", "time_end", "daypart", "recurring", "reminder_lead"]


def norm(v):
    """Нормализация для сравнения: регистр и пробелы в title не критичны."""
    if isinstance(v, str):
        return v.strip().lower().rstrip(".")
    return v


def title_matches(expected, actual):
    """
    Сравнение названий по множеству слов, а не по подстроке.

    Порядок слов в title значения не имеет: 'форму школы заполнить' и
    'заполнить форму школы' — одно и то же. Проверяем, что все значимые
    слова эталона присутствуют в ответе модели.

    Сознательно мягкое сравнение: title — свободный текст, а предмет
    проверки в этом eval'е — структурированные поля (даты, время, списки).
    Строгое сравнение title давало ложные провалы на перестановках.
    """
    if not expected or not actual:
        return expected == actual

    def words(s):
        cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in s.lower())
        # предлоги и связки выкидываем — они не несут смысла
        stop = {"по", "на", "в", "с", "для", "и", "у", "к", "до"}
        # Грубое отсечение окончания: русский склоняется, и 'домашка' /
        # 'домашку' — одно слово. Точное сравнение давало ложные провалы.
        return {w[:-1] if len(w) > 4 else w
                for w in cleaned.split() if w and w not in stop}

    return words(expected) <= words(actual)


def compare(expected, actual):
    """Возвращает список расхождений. Пустой список = полное совпадение."""
    diffs = []

    if expected.get("action") != actual.get("action"):
        diffs.append(f"action: ждали {expected.get('action')}, "
                     f"получили {actual.get('action')}")
        return diffs  # дальше сравнивать бессмысленно

    if expected["action"] == "create":
        exp_tasks = expected.get("tasks", [])
        act_tasks = actual.get("tasks", []) or []

        if len(exp_tasks) != len(act_tasks):
            diffs.append(f"кол-во задач: ждали {len(exp_tasks)}, "
                         f"получили {len(act_tasks)}")
            return diffs

        for i, (e, a) in enumerate(zip(exp_tasks, act_tasks)):
            for f in TASK_FIELDS:
                ev, av = norm(e.get(f)), norm(a.get(f))
                if f == "title":
                    if title_matches(e.get(f), a.get(f)):
                        continue
                if ev != av:
                    tag = f"[{i}] " if len(exp_tasks) > 1 else ""
                    diffs.append(f"{tag}{f}: ждали {e.get(f)!r}, "
                                 f"получили {a.get(f)!r}")
    else:
        for f in ["task_id", "new_date", "keep_time"]:
            if f in expected and expected[f] != actual.get(f):
                diffs.append(f"{f}: ждали {expected[f]!r}, "
                             f"получили {actual.get(f)!r}")

    return diffs


# --------------------------------------------------------------------------
# Прогон
# --------------------------------------------------------------------------

def build_system(prompt_tpl, ref_date, weekday_ru, open_tasks):
    """
    Системный промпт двумя блоками.

    Первый — статичные правила, они не меняются между запросами, поэтому
    помечены cache_control: повторные запросы читают их из кеша по цене
    10% от обычного ввода. Второй блок — дата и открытые задачи, они
    меняются каждый раз и не кешируются.

    Порядок обязателен: кешируется префикс, поэтому изменяемая часть
    должна идти ПОСЛЕ статичной, иначе кеш не сработает ни разу.
    """
    tasks_str = (json.dumps(open_tasks, ensure_ascii=False, indent=2)
                 if open_tasks else "(нет открытых задач)")
    dynamic = (
        f"## КОНТЕКСТ\n\n"
        f"Сегодня: {ref_date} ({weekday_ru})\n"
        f"Часовой пояс: Europe/Dublin\n\n"
        f"Открытые задачи в базе:\n{tasks_str}"
    )
    return [
        {"type": "text", "text": prompt_tpl,
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": dynamic},
    ]


def run_model(client, model_key, dataset, prompt_tpl, verbose, runs=1):
    cfg = MODELS[model_key]
    ref_date = dataset["reference_date"]
    weekday_ru = dataset.get("reference_weekday_ru") or WEEKDAYS_RU[
        datetime.strptime(ref_date, "%Y-%m-%d").weekday()]

    # Модель недетерминирована: один и тот же кейс может пройти в одном
    # прогоне и упасть в другом. per_case считает, сколько раз из runs
    # каждый кейс прошёл — так виден разброс, а не одна случайная цифра.
    per_case = {c["id"]: 0 for c in dataset["cases"]}
    in_tok, out_tok, failures = 0, 0, {}
    cache_w, cache_r = 0, 0

    for _ in range(runs):
        for case in dataset["cases"]:
            system = build_system(prompt_tpl, ref_date, weekday_ru,
                                  case.get("open_tasks_context"))
            try:
                resp = client.messages.create(
                    model=cfg["id"],
                    max_tokens=2000,
                    # 0 вместо дефолтной 1.0: задача — извлечение по правилам,
                    # творчество здесь вредно. Главный рычаг против разброса.
                    temperature=0,
                    system=system,
                    tools=[TOOL],
                    tool_choice={"type": "tool", "name": "parse_message"},
                    messages=[{"role": "user", "content": case["input"]}],
                )
            except Exception as exc:
                failures[case["id"]] = (case, [f"ошибка API: {exc}"])
                continue

            u = resp.usage
            in_tok += u.input_tokens
            out_tok += u.output_tokens
            cache_w += getattr(u, "cache_creation_input_tokens", 0) or 0
            cache_r += getattr(u, "cache_read_input_tokens", 0) or 0

            # max_tokens может обрезать tool_use на середине — тихий сбой
            if resp.stop_reason == "max_tokens":
                failures[case["id"]] = (case, ["ОБРЕЗАНО по max_tokens"])
                continue

            block = next((b for b in resp.content if b.type == "tool_use"), None)
            if block is None:
                failures[case["id"]] = (case, [f"не вызван инструмент "
                                               f"(stop_reason={resp.stop_reason})"])
                continue

            diffs = compare(case["expected"], block.input)
            if diffs:
                failures[case["id"]] = (case, diffs)
            else:
                per_case[case["id"]] += 1

    total = len(dataset["cases"])
    # кейс засчитывается, только если прошёл ВСЕ прогоны
    passed = sum(1 for v in per_case.values() if v == runs)
    unstable = [cid for cid, v in per_case.items() if 0 < v < runs]

    # Кеш: запись 1.25x базовой ставки ввода, чтение 0.1x.
    cost = (in_tok / 1e6 * cfg["in"]
            + cache_w / 1e6 * cfg["in"] * 1.25
            + cache_r / 1e6 * cfg["in"] * 0.10
            + out_tok / 1e6 * cfg["out"])

    print(f"\n{'=' * 66}")
    print(f"  {model_key.upper()}  ({cfg['id']})")
    print(f"{'=' * 66}")
    print(f"  Точность : {passed}/{total}  ({passed / total * 100:.0f}%)"
          + (f"   [{runs} прогона]" if runs > 1 else ""))
    print(f"  Токены   : {in_tok} in / {out_tok} out")
    if cache_w or cache_r:
        hit = cache_r / (cache_r + cache_w) * 100 if (cache_r + cache_w) else 0
        print(f"  Кеш      : {cache_w} записано / {cache_r} прочитано  "
              f"(попаданий {hit:.0f}%)")
    print(f"  Стоимость: ${cost:.4f}  "
          f"(~${cost / total / runs * 300:.2f} за 300 сообщений)")

    if unstable:
        print(f"\n  НЕСТАБИЛЬНЫЕ (проходят не всегда): "
              f"{', '.join('#' + str(c) for c in unstable)}")

    if failures:
        print(f"\n  Расхождения ({len(failures)}):")
        for case, diffs in failures.values():
            n = per_case[case["id"]]
            tag = f"   [прошёл {n}/{runs}]" if runs > 1 else ""
            print(f"\n  #{case['id']}  {case['input']}{tag}")
            print(f"       проверяет: {case.get('tests', '—')}")
            for d in diffs:
                print(f"       ✗ {d}")

    return {"model": model_key, "passed": passed, "total": total,
            "cost": cost, "unstable": len(unstable)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", choices=list(MODELS) + ["all"], default="haiku",
                    help="по умолчанию haiku; 'all' — сверка со старшими моделями")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--runs", type=int, default=1,
                    help="сколько раз прогнать каждый кейс (замер разброса)")
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit(
            "Ключ не найден.\n"
            f"Создай файл {HERE / '.env'} со строкой:\n"
            "  ANTHROPIC_API_KEY=sk-ant-..."
        )

    dataset = json.loads((HERE / "eval_dataset.json").read_text(encoding="utf-8"))
    prompt_tpl = (HERE / "parser_prompt.md").read_text(encoding="utf-8")
    client = Anthropic()

    keys = list(MODELS) if args.model == "all" else [args.model]
    results = [run_model(client, k, dataset, prompt_tpl, args.verbose, args.runs)
               for k in keys]

    print(f"\n{'=' * 66}")
    print("  ИТОГ" + (f"  ({args.runs} прогона на кейс)" if args.runs > 1 else ""))
    print(f"{'=' * 66}")
    for r in results:
        pct = r["passed"] / r["total"] * 100
        extra = f"   нестабильных: {r['unstable']}" if args.runs > 1 else ""
        print(f"  {r['model']:<8} {r['passed']:>2}/{r['total']}  "
              f"{pct:>3.0f}%   ${r['cost']:.4f}{extra}")
    print()


if __name__ == "__main__":
    main()
