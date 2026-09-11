#!/usr/bin/env python3
"""
Nyota Assistant - Migration Runner
Usage:
    py -3.13 backend/migrate.py          # Run all pending migrations
    py -3.13 backend/migrate.py --down   # Rollback all migrations
"""

import sys
from migrations.base import MigrationRunner
from migrations._registry import get_migrations

MIGRATIONS = get_migrations()


def main():
    runner = MigrationRunner()
    try:
        if "--down" in sys.argv:
            runner.rollback(MIGRATIONS)
            print("\n Migration rollback complete.")
        else:
            runner.run(MIGRATIONS)
            print("\n All migrations complete.")
    finally:
        runner.close()


if __name__ == "__main__":
    main()
