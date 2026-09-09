#!/usr/bin/env python3
"""
Накатывание миграций Family Assistant.

Что делает:
  1. Заводит таблицу schema_migrations, если её ещё нет.
  2. Смотрит файлы в db/migrations/*.sql по алфавиту.
  3. Выполняет те, которых нет в таблице, и записывает их туда.

Повторный запуск ничего не делает: применённые файлы пропускаются.

    python3 db/migrate.py             накатить всё непринятое
    python3 db/migrate.py --status    показать, что применено и что ждёт
    python3 db/migrate.py --dry-run   показать план, ничего не выполняя

--------------------------------------------------------------------
Почему psql, а не psycopg

Файлы выполняются внешним вызовом psql. Причина одна: psql работает
в автокоммите, каждая команда идёт своей транзакцией. Это единственный
способ выполнить ALTER TYPE ... ADD VALUE на любой версии PostgreSQL —
до 12-й он внутри транзакции запрещён вовсе, а начиная с 12-й добавленное
значение нельзя использовать в той же транзакции.

Обычные миграции безопаснее гонять одной транзакцией: упавшая на
середине не оставит базу наполовину изменённой. Поэтому по умолчанию
файл выполняется с флагом -1, то есть целиком или никак.

Файл, которому транзакция мешает, объявляет это первой строкой:

    -- no-transaction

Тогда -1 не ставится. Такие файлы пишутся идемпотентно — IF NOT EXISTS
и подобное, — потому что после падения на середине часть команд уже
закоммичена, а сама миграция незаписанной, и следующий запуск начнёт
её сначала.

--------------------------------------------------------------------
Правила именования

    001_reminder_stages.sql
    002_daily_checkmarks.sql

Номер с ведущими нулями, потом короткое имя. Сортировка по алфавиту
совпадает с порядком применения, пока номера трёхзначные.

Переименовывать применённый файл нельзя: в schema_migrations лежит
имя, под новым именем миграция накатится второй раз.
"""

import os
import subprocess
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
MIGRATIONS_DIR = ROOT / "db" / "migrations"

load_dotenv(ROOT / ".env")

# Маркер в первой строке файла: выполнять без общей транзакции.
NO_TRANSACTION = "-- no-transaction"


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("Нет DATABASE_URL в .env — не к чему подключаться.")
    return url


def ensure_table(url: str) -> None:
    """
    Журнал применённых миграций.

    Имя файла — первичный ключ: повторно тот же файл не запишется,
    даже если два запуска пойдут одновременно.
    """
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name       TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)


def applied_names(url: str) -> set:
    with psycopg.connect(url, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT name FROM schema_migrations ORDER BY name").fetchall()
    return {r[0] for r in rows}


def all_files() -> list:
    if not MIGRATIONS_DIR.exists():
        sys.exit(f"Нет папки {MIGRATIONS_DIR} — класть миграции некуда.")
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def needs_transaction(path: Path) -> bool:
    """Первая непустая строка файла решает, оборачивать ли в транзакцию."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            return NO_TRANSACTION not in line
    return True


def run_file(url: str, path: Path) -> bool:
    """
    Выполняет один файл через psql. Возвращает True при успехе.

    -X   не читать ~/.psqlrc: чужие настройки не должны влиять
    -v ON_ERROR_STOP=1   остановиться на первой ошибке, иначе psql
         досчитает файл до конца и вернёт нулевой код при провале
    -1   вся миграция одной транзакцией (кроме файлов с маркером)
    """
    cmd = ["psql", url, "-X", "-v", "ON_ERROR_STOP=1"]
    if needs_transaction(path):
        cmd.append("-1")
    else:
        print("     (без транзакции — так указано в файле)")
    cmd += ["-f", str(path)]

    result = subprocess.run(cmd)
    return result.returncode == 0


def record(url: str, name: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO schema_migrations (name) VALUES (%s)", (name,))


def main() -> None:
    flag = sys.argv[1] if len(sys.argv) > 1 else ""
    url = database_url()

    ensure_table(url)
    done = applied_names(url)
    files = all_files()
    pending = [f for f in files if f.name not in done]

    if flag == "--status":
        print(f"База: {url}\n")
        for f in files:
            mark = "✓ применена" if f.name in done else "· ждёт"
            print(f"  {mark:<12} {f.name}")
        if not files:
            print("  (файлов миграций нет)")
        return

    if not pending:
        print(f"Всё применено, файлов в папке: {len(files)}.")
        return

    print(f"Непринятых миграций: {len(pending)}\n")

    if flag == "--dry-run":
        for f in pending:
            mode = "транзакцией" if needs_transaction(f) else "без транзакции"
            print(f"  {f.name}  ({mode})")
        print("\nНичего не выполнено: это --dry-run.")
        return

    for f in pending:
        print(f"→  {f.name}")
        if not run_file(url, f):
            # Не записываем в журнал и не идём дальше: следующая миграция
            # может опираться на то, что эта не доделала.
            print(f"\n[!] {f.name} упала. Дальше не иду.")
            print("    Что уже успело примениться — смотри в выводе psql выше.")
            sys.exit(1)
        record(url, f.name)
        print(f"✓  {f.name} применена\n")

    print("Готово.")


if __name__ == "__main__":
    main()
