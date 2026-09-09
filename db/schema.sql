-- ============================================================
-- Family Assistant — схема базы данных
-- PostgreSQL 14+
-- ============================================================
--
-- Ключевые решения, зафиксированные при проектировании:
--
--  1. Шаблоны недели отделены от экземпляров задач. Перенос тенниса
--     на этой неделе не должен менять правило «теннис по вторникам».
--  2. Всё время в UTC. Показывается в Europe/Dublin. Ирландия
--     переводит часы дважды в год — без UTC расписание уедет на час.
--  3. Ежедневные задачи не переносятся: не сделал — статус skipped,
--     без вопросов и без попадания в отдельные дела.
--  4. Бот не знает, кто пишет. Общий чат, общая база, без прав.


-- ------------------------------------------------------------
-- ТИПЫ
-- ------------------------------------------------------------
-- ENUM вместо текста: база сама отвергнет опечатку вроде 'sasha1'.
-- Цена решения: добавить члена семьи = миграция ALTER TYPE.
-- Для фиксированного состава семьи это приемлемо.

CREATE TYPE assignee AS ENUM ('seva', 'gleb', 'kamilla', 'vova', 'sasha');

-- Три списка из нашего обсуждения: план по дням / отдельные дела / ежедневные
CREATE TYPE task_list AS ENUM ('scheduled', 'backlog', 'daily');

CREATE TYPE time_mode AS ENUM ('exact', 'range', 'daypart', 'allday', 'undated');

CREATE TYPE daypart AS ENUM ('morning', 'afternoon', 'evening');

-- skipped — только для ежедневных: день прошёл, галочки нет, переноса нет
CREATE TYPE task_status AS ENUM ('pending', 'done', 'skipped', 'cancelled');

-- Лестница напоминаний: за час, за полчаса, за десять минут и в момент
-- начала. Каждая ступень — отдельное значение, а не общий 'timed',
-- потому что защита от дублей стоит на ключе (task_id, kind, sent_date):
-- под одним видом вторая ступень того же дня не прошла бы.
--
-- 'timed' остаётся за пингом части дня — у него ступеней нет.
CREATE TYPE reminder_kind AS ENUM ('digest_morning', 'digest_evening',
                                   'timed', 'deadline',
                                   'lead_60', 'lead_30', 'lead_10', 'start');


-- ------------------------------------------------------------
-- ШАБЛОНЫ НЕДЕЛИ
-- ------------------------------------------------------------
-- Намерение: «теннис по вторникам в 15:00».
-- Раз в неделю разворачивается в конкретные строки tasks.

CREATE TABLE task_templates (
    id             SERIAL PRIMARY KEY,
    title          TEXT        NOT NULL,
    assignee       assignee,                    -- NULL = дело дома
    weekday        SMALLINT    NOT NULL,        -- 0 = понедельник, 6 = воскресенье
    time_mode      time_mode   NOT NULL,
    time_start     TIME,
    time_end       TIME,
    daypart        daypart,
    reminder_lead  INTEGER     NOT NULL DEFAULT 10,   -- минут до начала
    active         BOOLEAN     NOT NULL DEFAULT TRUE, -- FALSE = на паузе, но не удалён
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT weekday_valid CHECK (weekday BETWEEN 0 AND 6)
);


-- ------------------------------------------------------------
-- ЗАДАЧИ
-- ------------------------------------------------------------

CREATE TABLE tasks (
    id           SERIAL PRIMARY KEY,
    title        TEXT        NOT NULL,
    assignee     assignee,                      -- NULL = дело дома
    list         task_list   NOT NULL,

    -- ON DELETE SET NULL: удалили шаблон — уже созданные задачи остаются,
    -- просто теряют связь с правилом. Иначе снос шаблона стёр бы историю.
    template_id  INTEGER     REFERENCES task_templates(id) ON DELETE SET NULL,

    date         DATE,                          -- NULL = отдельные дела
    deadline     DATE,                          -- «до субботы», подсветка при приближении

    time_mode    time_mode   NOT NULL,
    time_start   TIME,
    time_end     TIME,
    daypart      daypart,

    reminder_lead   INTEGER  NOT NULL DEFAULT 10,
    -- FALSE для ежедневных: не сделал — сгорело, никакого переноса
    carry_over      BOOLEAN  NOT NULL DEFAULT TRUE,

    status          task_status NOT NULL DEFAULT 'pending',
    postponed_count INTEGER     NOT NULL DEFAULT 0,

    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,

    -- ---- Правила целостности ----
    -- Поле list дублирует то, что выводится из date и типа задачи.
    -- Держим его явно ради простых запросов, но заставляем базу
    -- следить за согласованностью. Иначе рассинхрон неизбежен.
    CONSTRAINT list_matches_date CHECK (
        (list = 'scheduled' AND date IS NOT NULL) OR
        (list = 'backlog'   AND date IS NULL)     OR
        (list = 'daily')
    ),

    -- Каждому режиму времени — свой обязательный набор полей.
    -- Без этого появится exact без времени, и напоминание не отправится.
    CONSTRAINT time_fields_match_mode CHECK (
        (time_mode = 'exact'   AND time_start IS NOT NULL AND daypart IS NULL) OR
        (time_mode = 'range'   AND time_start IS NOT NULL AND time_end IS NOT NULL) OR
        (time_mode = 'daypart' AND daypart IS NOT NULL AND time_start IS NULL) OR
        (time_mode = 'allday'  AND time_start IS NULL AND daypart IS NULL) OR
        (time_mode = 'undated' AND time_start IS NULL AND daypart IS NULL)
    ),

    CONSTRAINT range_ordered CHECK (time_end IS NULL OR time_end > time_start)
);

-- Основной запрос планировщика: «что сегодня и не закрыто».
-- WHERE в индексе — частичный индекс: строки done/cancelled в него
-- не попадают, поэтому он остаётся маленьким даже через год.
CREATE INDEX idx_tasks_due ON tasks (date, time_start)
    WHERE status = 'pending';

CREATE INDEX idx_tasks_backlog ON tasks (deadline)
    WHERE list = 'backlog' AND status = 'pending';

CREATE INDEX idx_tasks_assignee ON tasks (assignee, date)
    WHERE status = 'pending';


-- ------------------------------------------------------------
-- ОТПРАВЛЕННЫЕ НАПОМИНАНИЯ
-- ------------------------------------------------------------
-- message_id обязателен: без него нечего редактировать при закрытии
-- задачи, и вместо зачёркивания придётся слать новое сообщение.

CREATE TABLE reminders (
    id          SERIAL PRIMARY KEY,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    chat_id     BIGINT  NOT NULL,           -- у групп Telegram отрицательный
    message_id  BIGINT  NOT NULL,
    kind        reminder_kind NOT NULL,

    -- Момент отправки в UTC.
    sent_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- День по дублинскому времени, к которому относится напоминание.
    -- Отдельная колонка, а не sent_at::date, по двум причинам:
    --   1. Приведение TIMESTAMPTZ к DATE зависит от часового пояса сессии,
    --      поэтому Postgres не пускает его в индекс (только IMMUTABLE).
    --   2. По сути это не производная величина, а бизнес-понятие:
    --      «за какой день это напоминание». Приложение знает дублинскую
    --      дату и записывает её явно.
    sent_date   DATE NOT NULL DEFAULT CURRENT_DATE,

    answered_at TIMESTAMPTZ
);

-- Защита от дублей: одно напоминание одного вида на задачу в день.
-- Если планировщик запустится дважды, база не даст отправить повтор.
CREATE UNIQUE INDEX idx_reminders_once
    ON reminders (task_id, kind, sent_date);


-- ------------------------------------------------------------
-- ЗАКРЕПЛЁННЫЙ ПЛАН НЕДЕЛИ
-- ------------------------------------------------------------
-- Одна строка. Нужна, чтобы после перезапуска бот знал,
-- какое сообщение редактировать вместо отправки нового.

CREATE TABLE pinned_plan (
    chat_id     BIGINT PRIMARY KEY,
    message_id  BIGINT NOT NULL,
    week_start  DATE   NOT NULL,            -- понедельник текущей недели
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ------------------------------------------------------------
-- ПАУЗЫ
-- ------------------------------------------------------------
-- Три уровня, как договорились: вся система / человек / задача.
-- Каникулы, отпуск, болезнь. На паузе напоминания не идут,
-- шаблоны не разворачиваются, задачи не копятся в бэклоге.

CREATE TABLE pauses (
    id          SERIAL PRIMARY KEY,
    scope_person assignee,                  -- NULL + NULL task_id = вся система
    task_id      INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    starts_on    DATE NOT NULL DEFAULT CURRENT_DATE,
    ends_on      DATE,                      -- NULL = бессрочно, до отмены
    reason       TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Пауза либо на человека, либо на задачу, либо глобальная.
    -- Не «на человека И на задачу» одновременно — это неоднозначно.
    CONSTRAINT one_scope CHECK (
        NOT (scope_person IS NOT NULL AND task_id IS NOT NULL)
    ),
    CONSTRAINT dates_ordered CHECK (ends_on IS NULL OR ends_on >= starts_on)
);


-- ------------------------------------------------------------
-- ЖУРНАЛ СООБЩЕНИЙ
-- ------------------------------------------------------------
-- Сырой текст и то, как его разобрал парсер.
-- Нужен для двух вещей: разбор ошибок парсера («почему он так понял?»)
-- и пополнение eval-датасета реальными фразами.

CREATE TABLE message_log (
    id           SERIAL PRIMARY KEY,
    chat_id      BIGINT NOT NULL,
    message_id   BIGINT,
    raw_text     TEXT   NOT NULL,
    parsed       JSONB,                     -- что вернул парсер
    parse_ok     BOOLEAN NOT NULL DEFAULT TRUE,
    model        TEXT,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_message_log_recent ON message_log (created_at DESC);


-- ------------------------------------------------------------
-- ЧИСТКА ИСТОРИИ
-- ------------------------------------------------------------
-- Договорились: 60 дней. Запускать раз в сутки.
-- Ежедневные задачи чистим агрессивнее — их много и они однотипны.

-- DELETE FROM tasks
--  WHERE status IN ('done', 'skipped', 'cancelled')
--    AND completed_at < NOW() - INTERVAL '60 days';
--
-- DELETE FROM message_log
--  WHERE created_at < NOW() - INTERVAL '60 days';
