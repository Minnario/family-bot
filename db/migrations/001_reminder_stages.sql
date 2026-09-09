-- no-transaction
-- ============================================================
-- 001 — ступени напоминаний
-- ============================================================
--
-- Что делает: добавляет четыре значения в ENUM reminder_kind.
-- Данные не трогает, таблицы не перестраивает.
--
-- Зачем: защита от дублей стоит на уникальном ключе
-- (task_id, kind, sent_date). Пока все ступени пишутся одним видом
-- 'timed', вторая ступень того же дня не проходит индекс
-- idx_reminders_once. Каждой ступени нужен свой вид.
--
-- 'timed' остаётся за пингом части дня — у него ступеней нет.
--
-- Маркер no-transaction в первой строке обязателен: ALTER TYPE ...
-- ADD VALUE до PostgreSQL 12 запрещён внутри транзакции, а начиная
-- с 12-й добавленное значение нельзя использовать в той же
-- транзакции. IF NOT EXISTS делает файл безопасным для повтора.
--
-- Проверка результата:
--   psql -d familybot -c "SELECT unnest(enum_range(NULL::reminder_kind))"
-- Ожидается восемь строк.
-- ============================================================

ALTER TYPE reminder_kind ADD VALUE IF NOT EXISTS 'lead_60';
ALTER TYPE reminder_kind ADD VALUE IF NOT EXISTS 'lead_30';
ALTER TYPE reminder_kind ADD VALUE IF NOT EXISTS 'lead_10';
ALTER TYPE reminder_kind ADD VALUE IF NOT EXISTS 'start';
