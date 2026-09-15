#!/usr/bin/env python
import os
import sys

# Commands that must not go through a transaction-mode connection pooler (Neon "-pooler" host /
# PgBouncer): schema changes and interactive/session-level tools. They use DATABASE_URL_DIRECT when
# it is set; otherwise DATABASE_URL. Force it for any other command with DJANGO_DB_DIRECT=true.
DIRECT_DB_COMMANDS = {"migrate", "makemigrations", "showmigrations", "sqlmigrate", "dbshell", "createcachetable",
                      "flush", "sqlflush"}


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "patroliq.settings")
    if len(sys.argv) > 1 and sys.argv[1] in DIRECT_DB_COMMANDS:
        os.environ.setdefault("DJANGO_DB_DIRECT", "true")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
