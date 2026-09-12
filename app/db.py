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
from datetime import date, datetime, timedelta
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
    """
    Задачи на конкретный день. Основа утренней сводки и вечерней сверки.

    Сортировка по «эффективному времени»: у задач с частью дня своего
    времени нет, но в списке они должны стоять там, куда попадают по
    смыслу. Простое NULLS LAST отправляло «утром прописи» в конец,
    ниже задачи на 19:00 — читалось неправильно.

    Числа ниже — только ключи сортировки, в саму задачу они не
    записываются: «утром» так и остаётся «утром». Границы совпадают
    с началом частей дня из промпта парсера.

    allday (день есть, времени нет) остаётся в конце — там ему и место.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, title, assignee, time_mode,
                       time_start, time_end, daypart
                  FROM tasks
                 WHERE date = %s AND status = 'pending'
                 ORDER BY COALESCE(
                              time_start,
                              CASE daypart
                                  WHEN 'morning'   THEN TIME '07:00'
                                  WHEN 'afternoon' THEN TIME '12:00'
                                  WHEN 'evening'   THEN TIME '17:00'
                              END
                          ) ASC NULLS LAST,
                          id
            """, (day,))
            return cur.fetchall()


def tasks_beyond(after: date) -> List[Dict[str, Any]]:
    """
    Незакрытые задачи с датой позже `after`. Всё, что не попало
    в сводки из-за их горизонта.

    Сводки смотрят на 7 и 30 дней вперёд, поэтому задача на декабрь
    не видна нигде до самого декабря: записал и проверить нельзя.
    Через месяц человек не помнит, записывал ли, и записывает заново —
    отсюда дубли.

    Потолка по датам нет намеренно: смысл выборки в том, чтобы
    показать всё, что есть, включая задачу на следующий год.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT t.id, t.title, t.assignee, t.date, t.time_mode,
                       t.time_start, t.time_end, t.daypart
                  FROM tasks t
                 WHERE t.status = 'pending'
                   AND t.date > %s
                   AND {_NOT_PAUSED}
                 ORDER BY t.date, t.time_start NULLS LAST, t.id
            """, (after,))
            return cur.fetchall()


def tasks_for_range(start: date, end: date) -> List[Dict[str, Any]]:
    """
    Задачи за отрезок дней, включая обе границы. Основа сводок
    на неделю и на месяц.

    Сортировка та же, что в tasks_for_date, плюс дата первым ключом:
    внутри дня порядок должен совпадать с дневной сводкой, иначе одна
    и та же среда выглядит по-разному в двух местах.

    Колонка date здесь в выборке нужна — вёрстка группирует по дням.
    В tasks_for_date её нет, потому что там день и так известен.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, title, assignee, date, time_mode,
                       time_start, time_end, daypart
                  FROM tasks
                 WHERE date BETWEEN %s AND %s AND status = 'pending'
                 ORDER BY date ASC,
                          COALESCE(
                              time_start,
                              CASE daypart
                                  WHEN 'morning'   THEN TIME '07:00'
                                  WHEN 'afternoon' THEN TIME '12:00'
                                  WHEN 'evening'   THEN TIME '17:00'
                              END
                          ) ASC NULLS LAST,
                          id
            """, (start, end))
            return cur.fetchall()


# ------------------------------------------------------------------
# Запросы планировщика
# ------------------------------------------------------------------

# Задачи на паузе исключаются из всех выборок планировщика.
# Пауза бывает трёх уровней: вся система, человек, отдельная задача.
_NOT_PAUSED = """
    NOT EXISTS (
        SELECT 1 FROM pauses p
         WHERE (p.starts_on <= CURRENT_DATE)
           AND (p.ends_on IS NULL OR p.ends_on >= CURRENT_DATE)
           AND (
                (p.scope_person IS NULL AND p.task_id IS NULL)  -- вся система
             OR (p.scope_person = t.assignee)                    -- человек
             OR (p.task_id = t.id)                               -- задача
           )
    )
"""


def tasks_timed_today(now: datetime) -> List[Dict[str, Any]]:
    """
    Все незакрытые задачи на сегодня с точным временем начала.

    Раньше здесь считалось окно напоминания: запрос сам отбирал задачи,
    до которых осталось не больше reminder_lead минут. Ступень была одна,
    и это работало.

    Теперь ступеней четыре — за час, за полчаса, за десять минут и в момент
    начала, — и решает, какая наступила, планировщик. Ему для этого нужны
    все задачи дня целиком.

    Условие «ещё не началась» тоже убрано: ступень 'start' срабатывает
    в момент начала и сразу после него, отсечка по времени её потеряла бы.

    Ежедневные попадают сюда по отдельной ветке: у них date равен NULL,
    потому что одна строка служит и правилом, и экземпляром. Раньше
    условие date = сегодня отсекало их молча, и привычка со временем
    («ложить спать в 20:00») не получала напоминаний вовсе.

    Повторную отправку это не ломает: ключ в reminders — (task_id, kind,
    sent_date), то есть посуточный. Одна и та же ежедневная задача
    получит свой набор ступеней каждый день.

    Поле list в выборке нужно вёрстке: у ежедневных не должно быть кнопок.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT t.id, t.title, t.assignee, t.list, t.time_mode,
                       t.time_start, t.time_end, t.reminder_lead
                  FROM tasks t
                 WHERE t.status = 'pending'
                   AND t.time_mode IN ('exact', 'range')
                   AND t.time_start IS NOT NULL
                   AND (t.date = %(today)s OR t.list = 'daily')
                   AND {_NOT_PAUSED}
                 ORDER BY t.time_start
            """, {"today": now.date()})
            return cur.fetchall()


def tasks_daypart_now(now: datetime, daypart_name: str) -> List[Dict[str, Any]]:
    """
    Задачи на сегодня с указанной частью дня — для пинга в начале окна.

    Ежедневные включены той же веткой, что и в tasks_timed_today():
    у них date равен NULL, и условие date = сегодня отсекало их молча.
    «Чистить зубы вечером» не получало пинга в 17:00 именно поэтому.

    Поле list в выборке нужно вёрстке: у ежедневных не должно быть кнопок.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT t.id, t.title, t.assignee, t.list, t.daypart
                  FROM tasks t
                 WHERE t.status = 'pending'
                   AND t.time_mode = 'daypart'
                   AND t.daypart = %(dp)s
                   AND (t.date = %(today)s OR t.list = 'daily')
                   AND {_NOT_PAUSED}
                 ORDER BY t.id
            """, {"today": now.date(), "dp": daypart_name})
            return cur.fetchall()


def tasks_overdue(today: date) -> List[Dict[str, Any]]:
    """
    Незакрытые задачи прошедших дней.

    Автоматически никуда не переносятся — иначе факт «дело не сделано»
    растворится, а postponed_count перестанет что-либо значить.
    Просто показываются как просроченные, решение за человеком.

    carry_over = FALSE (ежедневные) исключены: они сгорают, а не висят.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT t.id, t.title, t.assignee, t.date, t.postponed_count
                  FROM tasks t
                 WHERE t.status = 'pending'
                   AND t.carry_over
                   AND t.date IS NOT NULL
                   AND t.date < %(today)s
                   AND {_NOT_PAUSED}
                 ORDER BY t.date, t.id
            """, {"today": today})
            return cur.fetchall()


def tasks_backlog(today: date) -> List[Dict[str, Any]]:
    """Отдельные дела. Дедлайны ближе к сроку идут первыми."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT t.id, t.title, t.assignee, t.deadline, t.postponed_count
                  FROM tasks t
                 WHERE t.status = 'pending'
                   AND t.list = 'backlog'
                   AND {_NOT_PAUSED}
                 ORDER BY t.deadline ASC NULLS LAST, t.id
            """)
            return cur.fetchall()


def tasks_daily() -> List[Dict[str, Any]]:
    """
    Ежедневные дела — для сводок и списка привычек.

    Поля времени добавлены не для напоминаний (те берёт
    tasks_timed_today), а для вёрстки: в сводке у каждой привычки
    должно стоять своё время, иначе «зарядка» и «чистить зубы»
    выглядят одинаково безвременными.

    Порядок по времени, безвременные в конец: NULLS LAST. Сортировка
    по id давала случайный порядок — привычка, заведённая раньше,
    оказывалась выше утренней, хотя делается вечером.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT t.id, t.title, t.assignee, t.status, t.list,
                       t.time_mode, t.time_start, t.time_end, t.daypart
                  FROM tasks t
                 WHERE t.list = 'daily'
                   AND t.status = 'pending'
                   AND {_NOT_PAUSED}
                 ORDER BY t.time_start NULLS LAST, t.daypart NULLS LAST, t.id
            """)
            return cur.fetchall()


def deadlines_soon(today: date, days: int = 1) -> List[Dict[str, Any]]:
    """Задачи с дедлайном сегодня или в ближайшие N дней."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT t.id, t.title, t.assignee, t.deadline
                  FROM tasks t
                 WHERE t.status = 'pending'
                   AND t.deadline IS NOT NULL
                   AND t.deadline BETWEEN %(today)s AND %(limit)s
                   AND {_NOT_PAUSED}
                 ORDER BY t.deadline, t.id
            """, {"today": today, "limit": today + timedelta(days=days)})
            return cur.fetchall()


# ------------------------------------------------------------------
# Шаблоны недели
# ------------------------------------------------------------------
#
# Правило живёт здесь, экземпляры — в tasks. Развязка нужна потому,
# что одна строка не может быть одновременно правилом и экземпляром:
# именно отсюда росли и галочка у ежедневных, закрывавшая привычку
# навсегда, и разовость задач на перечисленные дни недели.
#
# Разворачивает правила в задачи expand_templates(). Она идемпотентна:
# повторный запуск не создаёт дублей.


def create_template(t: Dict[str, Any]) -> int:
    """
    Заводит правило недели и возвращает его id.

    weekdays — список номеров дней, 0 = понедельник. Порядок внутри
    списка не важен, здесь он приводится к возрастающему без повторов.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO task_templates
                    (title, assignee, weekdays, time_mode,
                     time_start, time_end, daypart, reminder_lead)
                VALUES
                    (%(title)s, %(assignee)s, %(weekdays)s::SMALLINT[],
                     %(time_mode)s, %(time_start)s, %(time_end)s,
                     %(daypart)s, %(reminder_lead)s)
                RETURNING id
            """, {
                "title": t["title"],
                "assignee": t.get("assignee"),
                "weekdays": sorted(set(t["weekdays"])),
                "time_mode": t.get("time_mode", "allday"),
                "time_start": t.get("time_start"),
                "time_end": t.get("time_end"),
                "daypart": t.get("daypart"),
                "reminder_lead": t.get("reminder_lead", 10),
            })
            return cur.fetchone()["id"]


def list_templates(active_only: bool = True) -> List[Dict[str, Any]]:
    """Правила недели. По умолчанию только действующие."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT id, title, assignee, weekdays, time_mode,
                       time_start, time_end, daypart, reminder_lead,
                       active, created_at
                  FROM task_templates
                 {"WHERE active" if active_only else ""}
                 ORDER BY time_start NULLS LAST, id
            """)
            return cur.fetchall()


def find_active_template(title: str,
                         assignee: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Действующее правило с тем же названием и человеком, или None.

    Нужно против дублей: повтор фразы «домашка ПН СР ЧТ Севе» не должен
    заводить второе такое же расписание — иначе пойдут двойные
    напоминания, и понять причину из чата будет нельзя.

    Сравнение по паре название плюс человек, потому что именно её
    называет человек. Время и дни при повторе могут отличаться — это
    и есть изменение расписания, а не новое правило.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, title, assignee, weekdays, time_mode,
                       time_start, time_end, daypart, reminder_lead
                  FROM task_templates
                 WHERE active
                   AND title = %s
                   AND assignee IS NOT DISTINCT FROM %s
                 ORDER BY id
                 LIMIT 1
            """, (title, assignee))
            return cur.fetchone()


def update_template(template_id: int, t: Dict[str, Any]
                    ) -> Optional[Dict[str, Any]]:
    """
    Меняет дни и время правила, не создавая новое.

    Правка на месте, а не удаление с пересозданием: у уже прошедших
    задач сохраняется связь с правилом, то есть история не рвётся.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE task_templates
                   SET weekdays = %(weekdays)s::SMALLINT[],
                       time_mode = %(time_mode)s,
                       time_start = %(time_start)s,
                       time_end = %(time_end)s,
                       daypart = %(daypart)s,
                       reminder_lead = %(reminder_lead)s
                 WHERE id = %(id)s
                RETURNING id, title, assignee, weekdays
            """, {
                "id": template_id,
                "weekdays": sorted(set(t["weekdays"])),
                "time_mode": t.get("time_mode", "allday"),
                "time_start": t.get("time_start"),
                "time_end": t.get("time_end"),
                "daypart": t.get("daypart"),
                "reminder_lead": t.get("reminder_lead", 10),
            })
            return cur.fetchone()


def drop_future_instances(template_id: int, since: date) -> int:
    """
    Удаляет незакрытые экземпляры правила от даты `since` и дальше.
    Возвращает число удалённых.

    Вызывается после правки правила: старые дни и время больше
    не действуют, и развёртка создаст новые. Без этого «домашка
    ПН СР ЧТ» после смены на «ПН ВТ» оставила бы четверги висеть.

    Закрытые и отменённые не трогаются: это история, а не план.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM tasks
                 WHERE template_id = %s
                   AND date >= %s
                   AND status = 'pending'
            """, (template_id, since))
            return cur.rowcount


def resume_template(template_id: int) -> Optional[Dict[str, Any]]:
    """
    Снимает правило с паузы. Экземпляры создаст следующая развёртка.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE task_templates SET active = TRUE
                 WHERE id = %s AND NOT active
                RETURNING id, title
            """, (template_id,))
            return cur.fetchone()


def pause_template(template_id: int) -> Optional[Dict[str, Any]]:
    """
    Снимает правило с развёртки, не удаляя его.

    Уже созданные экземпляры остаются — их убирает отдельным вызовом
    drop_future_instances(), если нужно. Разделено намеренно: пауза
    правила и уборка плана на эту неделю — разные решения, и в разных
    местах нужны по отдельности.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE task_templates SET active = FALSE
                 WHERE id = %s AND active
                RETURNING id, title
            """, (template_id,))
            return cur.fetchone()


def delete_template(template_id: int) -> Optional[Dict[str, Any]]:
    """
    Удаляет правило целиком.

    Экземпляры выживают: у tasks.template_id в схеме стоит
    ON DELETE SET NULL, они теряют связь с правилом и становятся
    обычными задачами. Иначе снос правила стёр бы историю.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM task_templates WHERE id = %s
                RETURNING id, title
            """, (template_id,))
            return cur.fetchone()


# Горизонт развёртки. Две недели дают запас: даже если бот простоит
# несколько дней, при следующем запуске задачи на сегодня появятся.
# Значение живёт здесь, а не в планировщике, потому что разворачивает
# ещё и handle.py — сразу после того, как человек завёл правило.
EXPAND_DAYS = 14


def expand_templates(start: date, days: int = EXPAND_DAYS) -> int:
    """
    Разворачивает действующие правила в задачи на `days` дней вперёд,
    начиная с `start`. Возвращает число созданных задач.

    Одним запросом, а не циклом на Python: generate_series даёт все
    даты окна, CROSS JOIN сводит их с правилами, ON CONFLICT гасит уже
    созданное. Поэтому функцию можно звать сколько угодно раз —
    лишнего не появится, и это главное её свойство: развёртка идёт
    и по расписанию, и при старте бота.

    EXTRACT(ISODOW) даёт 1 для понедельника и 7 для воскресенья,
    поэтому минус один переводит в нашу нумерацию с нуля.

    carry_over = FALSE у экземпляров: не сделал в свой день — сгорело,
    переносить нечего, на следующей неделе будет свой экземпляр.
    """
    until = start + timedelta(days=days - 1)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO tasks
                    (title, assignee, list, template_id, date,
                     time_mode, time_start, time_end, daypart,
                     reminder_lead, carry_over)
                SELECT tt.title, tt.assignee, 'scheduled', tt.id, d::date,
                       tt.time_mode, tt.time_start, tt.time_end, tt.daypart,
                       tt.reminder_lead, FALSE
                  FROM task_templates tt
                  CROSS JOIN generate_series(%(start)s::date,
                                             %(until)s::date,
                                             INTERVAL '1 day') AS d
                 WHERE tt.active
                   AND (EXTRACT(ISODOW FROM d)::int - 1) = ANY(tt.weekdays)
                ON CONFLICT (template_id, date) DO NOTHING
            """, {"start": start, "until": until})
            return cur.rowcount


# ------------------------------------------------------------------
# Дедупликация напоминаний
# ------------------------------------------------------------------

def reminder_sent(task_id: int, kind: str, day: date) -> bool:
    """
    Отправляли ли уже такое напоминание сегодня.

    Проверять надо ДО отправки. Уникальный индекс в базе тоже защищает,
    но он сработает после того, как сообщение уже улетит в чат.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM reminders
                 WHERE task_id = %s AND kind = %s AND sent_date = %s
                 LIMIT 1
            """, (task_id, kind, day))
            return cur.fetchone() is not None


def record_reminder(task_id: int, chat_id: int, message_id: int,
                    kind: str, day: date) -> None:
    """
    Отмечает факт отправки. Вызывается ПОСЛЕ успешной отправки:
    если запись сделать раньше и отправка упадёт, напоминание
    потеряется молча.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO reminders
                    (task_id, chat_id, message_id, kind, sent_date)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (task_id, chat_id, message_id, kind, day))


# ------------------------------------------------------------------
# Изменение состояния задач
# ------------------------------------------------------------------

def complete_task(task_id: int) -> Optional[Dict[str, Any]]:
    """
    Закрывает задачу. Возвращает её или None, если такой нет
    либо она уже закрыта.

    Условие status = 'pending' в WHERE — защита от гонки: если два
    человека нажмут «Сделано» одновременно, второй запрос вернёт
    пустоту, и бот не отрапортует о закрытии дважды.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE tasks
                   SET status = 'done', completed_at = NOW()
                 WHERE id = %s AND status = 'pending'
                RETURNING id, title, assignee, date, list
            """, (task_id,))
            return cur.fetchone()


def reschedule_task(task_id: int, new_date: Optional[date],
                    keep_time: bool = True) -> Optional[Dict[str, Any]]:
    """
    Переносит задачу на другую дату.

    new_date = None означает «убрать дату» — задача уходит в отдельные
    дела. Тогда list меняется на backlog, а time_mode на undated:
    без даты время не имеет смысла, и CHECK-ограничение это не пропустит.

    postponed_count увеличивается всегда. Это единственный способ
    отличить «сдвинулось один раз» от «висит четвёртую неделю».
    """
    with connect() as conn:
        with conn.cursor() as cur:
            if new_date is None:
                cur.execute("""
                    UPDATE tasks
                       SET date = NULL,
                           list = 'backlog',
                           time_mode = 'undated',
                           time_start = NULL,
                           time_end = NULL,
                           daypart = NULL,
                           postponed_count = postponed_count + 1
                     WHERE id = %s AND status = 'pending'
                    RETURNING id, title, assignee, date, postponed_count
                """, (task_id,))
            elif keep_time:
                cur.execute("""
                    UPDATE tasks
                       SET date = %s,
                           list = 'scheduled',
                           postponed_count = postponed_count + 1
                     WHERE id = %s AND status = 'pending'
                    RETURNING id, title, assignee, date, time_start,
                              daypart, postponed_count
                """, (new_date, task_id))
            else:
                # Время сбрасывается, дата остаётся: «перенеси на среду»
                # без указания часа — задача на весь день.
                cur.execute("""
                    UPDATE tasks
                       SET date = %s,
                           list = 'scheduled',
                           time_mode = 'allday',
                           time_start = NULL,
                           time_end = NULL,
                           daypart = NULL,
                           postponed_count = postponed_count + 1
                     WHERE id = %s AND status = 'pending'
                    RETURNING id, title, assignee, date, postponed_count
                """, (new_date, task_id))
            return cur.fetchone()


def cancel_task(task_id: int) -> Optional[Dict[str, Any]]:
    """
    Отменяет задачу. Не удаляет: строка остаётся в истории со статусом
    cancelled. Удаление стёрло бы факт, что дело вообще заводилось.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE tasks
                   SET status = 'cancelled', completed_at = NOW()
                 WHERE id = %s AND status = 'pending'
                RETURNING id, title, assignee
            """, (task_id,))
            return cur.fetchone()


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

    # ---- Шаблоны недели ----
    print("\nПравило на понедельник-четверг...", end=" ")
    tpl = create_template({
        "title": "тестовая домашка",
        "assignee": "seva",
        "weekdays": [3, 0, 1, 2, 0],     # с повтором и не по порядку
        "time_mode": "exact",
        "time_start": "17:00",
        "reminder_lead": 10,
    })
    print(f"id={tpl}")

    saved = [t for t in list_templates() if t["id"] == tpl][0]
    print(f"  дни в базе: {saved['weekdays']}  (ждали [0, 1, 2, 3])")

    monday = date(2026, 9, 14)
    print("Развёртка на 14 дней...", end=" ")
    first = expand_templates(monday, 14)
    print(f"создано {first}  (ждали 8: четыре дня × две недели)")

    # Главное свойство: повторный запуск не должен создать ни одной
    # задачи. На нём держится и ночное задание, и развёртка при старте.
    print("Повторная развёртка того же окна...", end=" ")
    again = expand_templates(monday, 14)
    print(f"создано {again}  (ждали 0)")

    print("Сдвиг окна на неделю...", end=" ")
    shifted = expand_templates(monday + timedelta(days=7), 14)
    print(f"создано {shifted}  (ждали 4: только новая неделя)")

    print("Пауза правила...", end=" ")
    print("ок" if pause_template(tpl) else "ПРОВАЛ")

    print("Развёртка после паузы...", end=" ")
    paused = expand_templates(monday + timedelta(days=21), 14)
    print(f"создано {paused}  (ждали 0)")

    print("Удаление правила...", end=" ")
    print("ок" if delete_template(tpl) else "ПРОВАЛ")

    print("\nГотово. Удалить тестовые данные:")
    print("  psql -d familybot -c 'TRUNCATE tasks, task_templates, "
          "message_log RESTART IDENTITY CASCADE;'")
