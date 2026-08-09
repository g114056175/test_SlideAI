-- Migration: Add projects table to support historical project management
-- Compatible with SQLite and PostgreSQL.
-- Run this manually if you cannot use the Python migration helper.

CREATE TABLE IF NOT EXISTS projects (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER  NOT NULL REFERENCES users(id),
    project_name TEXT     NOT NULL,
    pdf_id       TEXT,
    pdf_path     TEXT,
    video_url    TEXT,
    script_json  TEXT,
    status       TEXT     NOT NULL DEFAULT 'processing',
    created_at   DATETIME NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_project_user_id     ON projects(user_id);
CREATE INDEX IF NOT EXISTS idx_project_status       ON projects(status);
CREATE INDEX IF NOT EXISTS idx_projects_user_status ON projects(user_id, status);
CREATE INDEX IF NOT EXISTS idx_project_pdf_id       ON projects(pdf_id);

-- For existing databases that already have the projects table, run:
-- ALTER TABLE projects ADD COLUMN pdf_id TEXT;
-- CREATE INDEX IF NOT EXISTS idx_project_pdf_id ON projects(pdf_id);
