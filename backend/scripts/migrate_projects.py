"""
Migration helper: create the `projects` table (and any other missing tables)
by calling SQLAlchemy's `create_all`.  Safe to run multiple times; existing
tables are not modified.

Usage (from the repository root):
    python -m backend.scripts.migrate_projects
"""

import sys
import os

# Allow running from the repo root without installing the package.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from dotenv import load_dotenv

dotenv_path = os.path.join(os.path.dirname(__file__), '..', '.env')
load_dotenv(dotenv_path=dotenv_path)

from sqlalchemy import create_engine, inspect
from backend.app.models import Base, Project

DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///./test.db')

# Normalise postgres:// -> postgresql://
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

engine = create_engine(DATABASE_URL)

def run():
    print(f"[migrate_projects] Connecting to: {DATABASE_URL}")
    print("[migrate_projects] Running create_all (safe -- existing tables are not touched)...")
    Base.metadata.create_all(bind=engine)

    inspector = inspect(engine)
    tables = inspector.get_table_names()
    if 'projects' in tables:
        cols = [c['name'] for c in inspector.get_columns('projects')]
        print(f"[migrate_projects] Table 'projects' exists with columns: {cols}")
        print("[migrate_projects] Migration completed successfully.")
    else:
        print("[migrate_projects] ERROR: Table 'projects' was not created. Check your DB connection.")
        sys.exit(1)

if __name__ == '__main__':
    run()
