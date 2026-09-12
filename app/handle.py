"""
Обработчик сообщений: соединяет парсер и базу.

Единственная точка входа — handle_message(). Бот будет вызывать её и
отправлять полученную строку в чат. Вся логика «что делать с разобранной
фразой» живёт здесь, а не в коде Telegram: когда понадобится другой
канал ввода, менять придётся только его.

Проверка из командной строки:
    python3 app/handle.py "Севе теннис во вторник в 3"
"""

import logging
import re
import sys
from collections import namedtuple
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))

from db import (cancel_task, complete_task, create_tasks, create_template,
                drop_future_instances, expand_templates, find_active_template,
                get_task, list_open_tasks, log_message, reschedule_task,
                update_template)
from parser import ParseError, parse, TZ

# Логгер модуля. Настройку уровня и формата делает bot.py, поэтому
# здесь только получение — иначе запуск из командной строки перебивал
# бы настройки бота.
log = logging.getLogger("handle")
# Константы и вёрстка живут в ui.py: их используют и подтверждения,
# и сводки. Держать имена членов семьи в двух файлах — гарантия того,
# что однажды они разъедутся.
from ui import (ASSIGNEE_RU, DAYPART_RU, WEEKDAYS_SHORT, day_label,
                build_all, build_backlog, build_daily, build_help,
                build_month, build_morning, build_rules, build_week)

# Тестовый chat_id для запусков из командной строки.
# У настоящих групп Telegram он отрицательный и приходит из апдейта.
CLI_CHAT_ID = 0

# Ответ обработчика: текст плюс варианты переспроса, если они есть.
# Раньше возвращалась просто строка, но боту нужно знать, надо ли
# рисовать кнопки — из текста это не вытащить.
#
# markup и html добавились для сводок: у них разметка уже собрана в ui.py
# (это InlineKeyboardMarkup, а не список строк, как в options), и текст
# содержит HTML-теги. Остальные ответы создаются как Reply(text, None)
# и получают значения по умолчанию.
Reply = namedtuple("Reply", ["text", "options", "markup", "html"],
                   defaults=(None, None, False))

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
# Фиксированные команды
# ------------------------------------------------------------------
# Запросы на чтение обрабатываются здесь, ДО вызова парсера. Причины:
#   1. Мгновенно и бесплатно — без обращения к API.
#   2. Однозначно. Модель может принять «что сегодня» за задачу
#      с таким названием; точное совпадение строки не может.
#
# Сравнение по полной строке, а не по вхождению: «сводка на день» —
# команда, «записать сводку на день по проекту» — задача.

COMMANDS = {
    # день
    "сводка на день":     "day",
    "сводка за день":     "day",
    "сводка дня":         "day",
    "сводка день":        "day",
    "сводка на сегодня":  "day",
    "что сегодня":        "day",
    "план на день":       "day",
    "план на сегодня":    "day",
    "задачи на сегодня":  "day",
    "дела на сегодня":    "day",
    # неделя
    "сводка на неделю":   "week",
    "сводка за неделю":   "week",
    "сводка недели":      "week",
    "сводка неделя":      "week",
    "сводка неделю":      "week",
    "план на неделю":     "week",
    "план недели":        "week",
    "что на неделе":      "week",
    "задачи на неделю":   "week",
    "дела на неделю":     "week",
    # месяц
    "сводка на месяц":    "month",
    "сводка за месяц":    "month",
    "сводка месяца":      "month",
    "сводка месяц":       "month",
    "план на месяц":      "month",
    "план месяца":        "month",
    "что на месяц":       "month",
    "задачи на месяц":    "month",
    "дела на месяц":      "month",
    # правила недели
    "правила":            "rules",
    "правило":            "rules",
    "мои правила":        "rules",
    "правила недели":     "rules",
    "расписание":         "rules",
    "расписания":         "rules",
    "расписание недели":  "rules",
    "повторяющиеся":      "rules",
    # всё сразу: месяц по дням + отдельные дела + ежедневные
    "сводка вся":         "all",
    "сводка общая":       "all",
    "общая сводка":       "all",
    "вся сводка":         "all",
    "полная сводка":      "all",
    "полную сводку":      "all",
    "общую сводку":       "all",
    "всю сводку":         "all",
    "сводка полная":      "all",
    "сводка всё":         "all",
    "сводка все":         "all",
    "всё сразу":          "all",
    "все дела":           "all",
    "все задачи":         "all",
    # дела без даты
    "список дел":         "backlog",
    "отдельные дела":     "backlog",
    "что висит":          "backlog",
    # инструкция
    "инструкция":         "help",
    "инструкцию":         "help",
    "помощь":             "help",
    "как пользоваться":   "help",
    "что ты умеешь":      "help",
    "справка":            "help",
    # ежедневные
    "ежедневные":         "daily",
    "ежедневные дела":    "daily",
    "список ежедневных":  "daily",
    "привычки":           "daily",
}


# Глаголы, с которых начинаются варианты переспроса от модели:
# «Посмотреть список дел», «Показать ежедневные». Отбрасываем их перед
# сравнением, иначе кнопка переспроса не совпадёт ни с одной командой,
# уйдёт обратно в парсер и вызовет второй такой же переспрос.
# Порядок важен: проверка идёт по списку и останавливается на первом
# совпадении. «дай мне » должно стоять раньше «дай », иначе от фразы
# «дай мне инструкцию» останется «мне инструкцию».
LEAD_VERBS = ("дай мне ", "покажи мне ", "посмотреть ", "показать ",
              "покажи ", "посмотри ", "открыть ", "дай ")


def _normalize(text: str) -> str:
    """
    Приведение к виду, в котором строка сравнивается с таблицей.

    Регистр не важен, лишние пробелы не важны, точка или знак вопроса
    в конце не важны. «Сводка на неделю?» — та же команда.
    """
    cleaned = " ".join(text.lower().split()).rstrip(".!?…")
    for verb in LEAD_VERBS:
        if cleaned.startswith(verb):
            cleaned = cleaned[len(verb):]
            break
    # После отбрасывания глагола остаётся винительный падеж: «покажи
    # сводку на неделю» → «сводку на неделю». В таблице ключи в
    # именительном, поэтому одно слово приводим здесь, а не заводим
    # вторую копию всех сорока форм.
    if cleaned.startswith("сводку"):
        cleaned = "сводка" + cleaned[len("сводку"):]
    return cleaned


def try_command(text: str) -> Optional[Reply]:
    """
    Готовый ответ, если текст — фиксированная команда. Иначе None.

    Отдельно от handle_message, потому что нужна боту: когда человек
    нажимает вариант переспроса, вариант надо сначала проверить по
    таблице команд. Иначе «Посмотреть список дел» склеивается с
    исходной фразой, снова идёт в парсер и снова возвращает переспрос.
    """
    kind = COMMANDS.get(_normalize(text))
    if not kind:
        return None
    return _run_command(kind, datetime.now(TZ).date())


def _run_command(kind: str, today: date) -> Reply:
    """Собирает нужную сводку. Текст и кнопки уже готовы в ui.py."""
    if kind == "week":
        text, kb = build_week(today)
    elif kind == "month":
        text, kb = build_month(today)
    elif kind == "all":
        text, kb = build_all(today)
    elif kind == "rules":
        text, kb = build_rules()
    elif kind == "backlog":
        text, kb = build_backlog(today)
    elif kind == "daily":
        text, kb = build_daily()
    elif kind == "help":
        text, kb = build_help()
    else:
        text, kb = build_morning(today)
    return Reply(text, None, kb, True)


# ------------------------------------------------------------------
# Автоответ на переспрос по части дня
# ------------------------------------------------------------------
# «Зарядка утром в 8» → парсер честно спрашивает «08:00 или 20:00?»,
# хотя слово «утром» уже всё решило. Правило механическое, и место
# ему в коде, а не в промпте: три попытки вписать его в промпт
# ломали соседние правила (кейсы #5, #23, #34 в датасете).
#
# Код выбирает вариант сам и прогоняет фразу через тот же путь
# «Уточнение: …», что и нажатие кнопки. Один лишний вызов API
# в редком случае — дешевле, чем нестабильный промпт.

DAYPART_HINTS = {
    "утром": "am", "днём": "am", "днем": "am",
    "вечером": "pm", "ночью": "pm",
}

CLARIFY_MARK = "Уточнение:"


def _resolve_by_daypart(text: str, result: Dict[str, Any]) -> Optional[str]:
    """
    Возвращает вариант ответа, если переспрос — про половину суток,
    а фраза сама на неё указывает. Иначе None, и кнопки покажутся
    как обычно.

    Условия жёсткие намеренно: ровно два варианта, оба — время,
    разница ровно 12 часов. Любой другой переспрос («какой матч?»,
    «18:10 или 6 октября?») этот код не трогает.
    """
    if CLARIFY_MARK in text:
        return None                      # уже отвечали, второй круг не нужен

    c = result.get("clarification") or {}
    opts = [o.strip() for o in (c.get("options") or [])]
    if len(opts) != 2:
        return None

    try:
        t0 = datetime.strptime(opts[0], "%H:%M")
        t1 = datetime.strptime(opts[1], "%H:%M")
    except ValueError:
        return None
    if abs(t0.hour - t1.hour) != 12 or t0.minute != t1.minute:
        return None

    low = text.lower()
    hint = next((half for word, half in DAYPART_HINTS.items()
                 if re.search(rf"\b{word}\b", low)), None)
    if hint is None:
        return None

    am, pm = sorted(opts, key=lambda o: datetime.strptime(o, "%H:%M"))
    return am if hint == "am" else pm


# ------------------------------------------------------------------
# Проверка дня недели
# ------------------------------------------------------------------
# 9 сентября (среда) фраза «в четверг» дала 17 сентября вместо 10-го —
# неделя лишку. Правило в промпте на этот счёт есть и сформулировано
# верно: «следующее наступление, исключая сегодня». Модель его нарушила,
# зацепившись за пример «сегодня четверг, сказано в четверг → +7 дней».
#
# Правило чисто арифметическое: имя дня плюс сегодняшняя дата дают
# ответ без единой неоднозначности. Значит, ему место здесь, а не
# в промпте — четвёртая формулировка словами дала бы то же, что и
# три попытки с «утром».
#
# Код не «угадывает» дату, а проверяет ответ модели: сдвигает его
# только тогда, когда день недели совпал, а неделя выбрана не та.
# Во всех спорных случаях молча оставляет как есть.

WEEKDAY_WORDS = {
    0: ("понедельник", "понедельника", "понедельнику", "понедельникам"),
    1: ("вторник", "вторника", "вторнику", "вторникам"),
    2: ("среда", "среду", "среды", "средам"),
    3: ("четверг", "четверга", "четвергу", "четвергам"),
    4: ("пятница", "пятницу", "пятницы", "пятницам"),
    5: ("суббота", "субботу", "субботы", "субботам"),
    6: ("воскресенье", "воскресенья", "воскресеньям"),
}

# Дата, названная словами: «17 сентября». Тогда день недели во фразе —
# уточнение к дате, а не источник даты, и трогать ответ модели нельзя.
#
# Цифры через точку сюда НЕ входят намеренно. В этом проекте точка —
# разделитель времени, а не даты: «4.30» это 16:30, «6.10» это 18:10.
# Первая версия фильтра ловила такие пары как дату и глушила поправку
# ровно на той фразе, ради которой её писали — «Вова Четверг сходить
# к ветеринару в 4.30».
_EXPLICIT_DATE = re.compile(
    r"\b\d{1,2}\s+(январ|феврал|март|апрел|ма[йя]|июн|июл|"
    r"август|сентябр|октябр|ноябр|декабр)"
)

# Слова, которые сами отодвигают дату дальше ближайшего дня:
# «в четверг через неделю», «в следующий вторник».
# Слова, которые сами отодвигают дату дальше ближайшего дня:
# «в четверг через неделю», «в следующий вторник».
#
# Голого «недел» здесь нет намеренно. Оно ловило и «каждую неделю» —
# а это ровно противоположный смысл: не сдвиг, а повтор. Сдвиг всегда
# назван словом: через, следующий, будущий.
_FAR_WEEK = re.compile(r"\bчерез\b|\bследующ|\bбудущ")


# Слова, которые делают задачу повторяющейся при любом числе дней.
# «Каждый вторник» — правило, хотя день всего один.
_RECURRING_WORDS = re.compile(
    r"\bкажд|\bеженедельн|\bрегулярно|\bпо понедельникам|\bпо вторникам"
    r"|\bпо средам|\bпо четвергам|\bпо пятницам|\bпо субботам"
    r"|\bпо воскресеньям"
)

# С какого числа названных дней задача считается расписанием, а не
# набором разовых дел. Три и больше: «понедельник, среда, четверг» —
# это уклад недели, а «среда и пятница» ещё может быть разовым.
#
# Порог невидим для человека, поэтому подтверждение обязано его
# проговаривать — пометка «(каждую неделю)» ставится именно для этого.
RULE_MIN_DAYS = 3


def _nearest_weekday(today: date, target: int) -> date:
    """
    Ближайшее наступление дня недели, исключая сегодня.

    Совпадение с сегодняшним днём даёт +7: для сегодня человек говорит
    «сегодня», а не называет день. Это то же правило, что в промпте.
    """
    ahead = (target - today.weekday()) % 7
    return today + timedelta(days=ahead or 7)


def _named_weekdays(text: str) -> List[int]:
    """
    Номера всех дней недели, названных во фразе, по возрастанию.

    Раньше возвращался один номер, а несколько дней давали None —
    код отказывался вмешиваться. Теперь несколько дней это не помеха,
    а сам смысл: «домашка пн вт ср чт» означает четыре задачи.
    """
    low = text.lower()
    return sorted(num for num, forms in WEEKDAY_WORDS.items()
                  if any(re.search(rf"\b{w}\b", low) for w in forms))


def _named_weekday(text: str) -> Optional[int]:
    """Единственный названный день недели или None, если их не ровно один."""
    found = _named_weekdays(text)
    return found[0] if len(found) == 1 else None


def _fix_weekday_date(text: str, value: Optional[str],
                      today: date) -> Optional[str]:
    """
    Сдвигает дату к ближайшему наступлению названного дня недели.

    Возвращает value без изменений, если хоть одно условие не сошлось:
    даты нет, во фразе месяц словами или слово «через», день недели
    не назван или назван не один, разбор не читается, день недели
    у даты не совпал с названным, дата уже ближайшая.

    Последнее условие — главное. Совпадение дня недели означает, что
    модель поняла фразу правильно и ошиблась только неделей. Если день
    не совпал, значит модель прочитала что-то другое, и подменять её
    ответ вслепую нельзя.
    """
    if not value:
        return value
    if _EXPLICIT_DATE.search(text) or _FAR_WEEK.search(text.lower()):
        return value

    target = _named_weekday(text)
    if target is None:
        return value

    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return value

    if parsed.weekday() != target:
        return value

    nearest = _nearest_weekday(today, target)
    return value if parsed == nearest else nearest.isoformat()


def _rule_weekdays(text: str, tasks: List[Dict[str, Any]]) -> List[int]:
    """
    Дни недели, если фраза описывает расписание, а не разовые дела.
    Пустой список — значит обычные задачи.

    Правилом фраза становится по любому из трёх признаков:

      * названо RULE_MIN_DAYS дней и больше. «Понедельник, среда,
        четверг» — это уклад недели, никто не перечисляет три дня
        ради одного раза;
      * есть слово повтора: «каждый вторник», «по средам»,
        «еженедельно». Тогда хватает и одного дня;
      * парсер сам поставил recurring. Раньше это поле приходило
        и выбрасывалось — бот показывал «(каждую неделю)» и ничего
        не повторял.

    Не правило, если во фразе явная дата словами или слово сдвига
    («через», «следующий»): «в четверг 17 сентября» и «в среду через
    неделю» — про конкретный раз, а не про уклад.

    Задачи без даты не рассматриваются вовсе. Ежедневные и отдельные
    дела живут по своим правилам, и трогать их здесь нельзя.
    """
    if _EXPLICIT_DATE.search(text) or _FAR_WEEK.search(text.lower()):
        return []
    if not any(t.get("date") for t in tasks):
        return []

    days = _named_weekdays(text)
    if not days:
        return []
    if len(days) >= RULE_MIN_DAYS:
        return days
    if _RECURRING_WORDS.search(text.lower()):
        return days
    if any(t.get("recurring") for t in tasks):
        return days
    return []


def _format_rule(task: Dict[str, Any], weekdays: List[int]) -> str:
    """
    Строка подтверждения для правила: «Сева — домашка, ПН ВТ СР ЧТ 17:00
    (каждую неделю)».

    Отдельно от format_task(), потому что у правила нет даты — вместо
    неё набор дней. Пометка обязательна: порог в три дня человеку не
    виден, и без неё непонятно, почему одна фраза дала расписание,
    а похожая — разовые дела.
    """
    who = ASSIGNEE_RU.get(task.get("assignee"))
    main = [f"{who} — {task['title']}" if who else task["title"]]

    when = " ".join(x for x in (
        " ".join(WEEKDAYS_SHORT[d] for d in weekdays),
        _fmt_time(task)) if x)
    main.append(when)

    return ", ".join(main) + "  (каждую неделю)"


# Потолок на размножение. Четыре дня на двоих детей — восемь задач,
# это нормально. Число заметно больше означает, что разбор пошёл не так:
# лучше оставить ответ модели как есть, чем засыпать чат.
MAX_EXPANDED = 20


def _expand_weekdays(text: str, tasks: List[Dict[str, Any]],
                     today: date) -> List[Dict[str, Any]]:
    """
    Размножает задачи по всем дням недели, названным во фразе.

    «В 4 часа дня, понедельник, вторник, среда, четверг, домашка, Севе,
    Глеб» → восемь задач: по одной каждому ребёнку на каждый день.

    Почему это здесь, а не в промпте. Правило 5 говорит обратное:
    задачу размножают только имена. Модель его иногда нарушала и делала
    восемь задач, иногда соблюдала и делала две — прогон eval поймал
    ровно это, 0 из 3. Поведение зависело от контекста, а не от правил.

    Само правило чисто арифметическое: названо N дней — значит N дат,
    каждая считается от сегодняшнего числа. Ему место в коде.
    Промпт при этом не трогаем: парсер обязан отдавать по задаче на имя,
    размножение по дням — работа этого слоя.

    Не вмешивается, если: дней названо меньше двух, во фразе явная дата
    или слово «через», задачи без даты (отдельные дела и ежедневные —
    у них дня нет по определению), модель уже разложила задачи ровно
    по нужным датам, или результат вышел бы больше MAX_EXPANDED.
    """
    targets = _named_weekdays(text)
    if len(targets) < 2:
        return tasks
    if _EXPLICIT_DATE.search(text) or _FAR_WEEK.search(text.lower()):
        return tasks

    dated = [t for t in tasks if t.get("date")]
    if not dated:
        return tasks

    target_dates = [_nearest_weekday(today, wd) for wd in targets]

    # Модель иногда сама раскладывает задачи по дням. Тогда даты уже
    # те, что нужно, и второй проход умножил бы восемь задач на четыре.
    present = {t["date"] for t in dated}
    if present == {d.isoformat() for d in target_dates}:
        return tasks

    if len(dated) * len(target_dates) > MAX_EXPANDED:
        return tasks

    # Порядок: по дням, внутри дня — как во фразе. «Севе, Глеб» даёт
    # сначала Севу, потом Глеба, и так в каждом дне.
    expanded = []
    for d in target_dates:
        for t in dated:
            copy = dict(t)
            copy["date"] = d.isoformat()
            expanded.append(copy)

    # Задачи без даты проходят мимо размножения, но из ответа не пропадают.
    return expanded + [t for t in tasks if not t.get("date")]


def _rule_subjects(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Оставляет по одной задаче на каждое сочетание названия и человека.

    Нужно потому, что модель часто сама раскладывает фразу по дням:
    «домашка ПН СР ЧТ Севе и Глебу» приходит шестью задачами, а не
    двумя. Правило описывает все дни сразу, поэтому шесть правил
    вместо двух — это шесть расписаний и шесть напоминаний на каждый
    день.

    Та же защита, что в _expand_weekdays(), только с другой стороны:
    там она мешает умножить уже разложенное, здесь — завести лишние
    правила.

    Порядок первых появлений сохраняется: «Севе и Глебу» даёт сначала
    Севу, потом Глеба, как во фразе.
    """
    seen = {}
    for t in tasks:
        key = (t.get("title"), t.get("assignee"))
        if key not in seen:
            seen[key] = t
    return list(seen.values())


def _create_rules(tasks: List[Dict[str, Any]], weekdays: List[int],
                  today: date) -> "Reply":
    """
    Заводит правила недели вместо разовых задач и сразу разворачивает
    их в задачи на ближайшие две недели.

    Правило на каждую задачу из разбора: «Севе и Глебу домашка» даёт
    два правила, по одному на ребёнка. Размножают именно имена — так же,
    как у обычных задач.

    Развёртка вызывается здесь, а не откладывается до ночи: человек
    только что завёл расписание и ждёт увидеть его в сводке на неделю
    прямо сейчас, а не завтра утром.

    Задачи без даты в правила не превращаются и записываются обычным
    порядком. Смешанная фраза редка, но терять из неё половину нельзя.
    """
    dated = _rule_subjects([t for t in tasks if t.get("date")])
    plain = [t for t in tasks if not t.get("date")]

    verbs = []
    try:
        for t in dated:
            fields = {
                "title": t["title"],
                "assignee": t.get("assignee"),
                "weekdays": weekdays,
                "time_mode": t.get("time_mode", "allday"),
                "time_start": t.get("time_start"),
                "time_end": t.get("time_end"),
                "daypart": t.get("daypart"),
                "reminder_lead": t.get("reminder_lead", 10),
            }
            # Повтор фразы не должен заводить второе такое же расписание:
            # пошли бы двойные напоминания, а причину из чата не увидеть.
            # Заодно это единственный способ изменить правило словами —
            # «домашка теперь ПН ВТ СР» правит существующее.
            found = find_active_template(t["title"], t.get("assignee"))
            if found:
                update_template(found["id"], fields)
                # Старые дни и время больше не действуют. Незакрытые
                # экземпляры от сегодня убираем, развёртка создаст новые.
                drop_future_instances(found["id"], today)
                verbs.append("Обновил")
            else:
                create_template(fields)
                verbs.append("Принял")
        created = expand_templates(today)
        if plain:
            create_tasks(plain)
    except Exception as exc:
        return Reply(f"Не смог записать: {exc}", None)

    log.info("правил: %s, задач развёрнуто: %d",
             ", ".join(verbs).lower() or "нет", created)

    lines = [f"{v}: {_format_rule(t, weekdays)}"
             for v, t in zip(verbs, dated)]
    lines += [f"Принял: {format_task(t, today)}" for t in plain]
    return Reply("\n".join(lines), None)


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

    # Команды чтения — до парсера и до запроса открытых задач.
    # В message_log не пишем: журнал существует ради разбора ошибок
    # парсера, а здесь парсер не участвует.
    command = try_command(text)
    if command:
        return command

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

    # --- автоответ, если переспрос снимается словом про часть дня ---
    if action == "clarify":
        auto = _resolve_by_daypart(text, result)
        if auto:
            return handle_message(f"{text}\n{CLARIFY_MARK} {auto}",
                                  chat_id, message_id)

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

        # Страховка от лишней недели: «в четверг» должно означать
        # ближайший четверг. Подробности — в _fix_weekday_date().
        for t in tasks:
            t["date"] = _fix_weekday_date(text, t.get("date"), today)

        # Расписание или разовые дела? Подробности — в _rule_weekdays().
        rule_days = _rule_weekdays(text, tasks)
        if rule_days:
            return _create_rules(tasks, rule_days, today)

        # Несколько дней недели во фразе — задача на каждый из них.
        # Подробности — в _expand_weekdays().
        tasks = _expand_weekdays(text, tasks, today)

        # Одна транзакция: если вторая задача не пройдёт проверки базы,
        # первая тоже откатится. Иначе человек получит подтверждение
        # на две задачи, а в базе окажется одна.
        try:
            create_tasks(tasks)
        except Exception as exc:
            return Reply(f"Не смог записать: {exc}", None)

        # Номер задачи не показываем. Это id из базы: он растёт без конца
        # и не переиспользуется, так что через год в каждой строке будут
        # пятизначные числа. Человеку он не нужен — закрытие и перенос
        # идут кнопками или фразой, а нужную строку парсер находит сам
        # по списку открытых задач, который уходит ему в промпт.
        lines = [f"Принял: {format_task(t, today)}" for t in tasks]
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

        # «Перенеси теннис на четверг» ошибается на неделю так же,
        # как и запись новой задачи — проверка нужна и здесь.
        new_date = _fix_weekday_date(text, new_date, today)

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
