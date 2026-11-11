"""Ollies Production Dashboard entry point.

The shared backend uses Supabase PostgreSQL when SUPABASE_DATABASE_URL is set.
Without that variable it keeps using the local SQLite database so the dashboard
can still be run and tested on a workstation.
"""

import app_sqlite_backup as backend
from app_sqlite_backup import *  # noqa: F401,F403 - keeps the existing import API


if __name__ == "__main__":
    backend.main()
