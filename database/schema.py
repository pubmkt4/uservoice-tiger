"""SQLite 스키마 정의 및 초기화"""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS keywords (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword     TEXT NOT NULL UNIQUE,
    appstore_id TEXT,
    playstore_id TEXT,
    dc_gallery_id TEXT,
    dc_is_minor INTEGER DEFAULT 1,
    created_at  TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS collection_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword_id   INTEGER REFERENCES keywords(id),
    started_at   TEXT,
    completed_at TEXT,
    status       TEXT,
    yt_count     INTEGER DEFAULT 0,
    dc_count     INTEGER DEFAULT 0,
    app_count    INTEGER DEFAULT 0,
    play_count   INTEGER DEFAULT 0,
    period_filter TEXT
);

CREATE TABLE IF NOT EXISTS youtube_videos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword_id    INTEGER REFERENCES keywords(id),
    video_id      TEXT NOT NULL,
    title         TEXT,
    channel       TEXT,
    subscribers   TEXT,
    views         INTEGER DEFAULT 0,
    likes         INTEGER DEFAULT 0,
    comment_count INTEGER DEFAULT 0,
    upload_date   TEXT,
    thumbnail_url TEXT,
    description   TEXT,
    link          TEXT,
    sentiment     TEXT,
    theme_id      INTEGER,
    collected_at  TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(keyword_id, video_id)
);

CREATE TABLE IF NOT EXISTS youtube_comments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword_id   INTEGER REFERENCES keywords(id),
    video_id     TEXT,
    author       TEXT,
    comment      TEXT,
    likes        INTEGER DEFAULT 0,
    date         TEXT,
    type         TEXT,
    sentiment    TEXT,
    theme_id     INTEGER,
    collected_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(video_id, author, date, type)
);

CREATE TABLE IF NOT EXISTS youtube_live_chat (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword_id   INTEGER REFERENCES keywords(id),
    video_id     TEXT,
    author       TEXT,
    message      TEXT,
    chat_time    TEXT,
    sentiment    TEXT,
    theme_id     INTEGER,
    collected_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(video_id, author, chat_time)
);

CREATE TABLE IF NOT EXISTS dc_posts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword_id   INTEGER REFERENCES keywords(id),
    title        TEXT,
    link         TEXT NOT NULL,
    content      TEXT,
    date         TEXT,
    source       TEXT,
    sentiment    TEXT,
    theme_id     INTEGER,
    collected_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(link)
);

CREATE TABLE IF NOT EXISTS appstore_reviews (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword_id   INTEGER REFERENCES keywords(id),
    review_id    TEXT NOT NULL,
    title        TEXT,
    author       TEXT,
    rating       REAL,
    content      TEXT,
    date         TEXT,
    version      TEXT,
    sentiment    TEXT,
    theme_id     INTEGER,
    collected_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(review_id)
);

CREATE TABLE IF NOT EXISTS playstore_reviews (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword_id   INTEGER REFERENCES keywords(id),
    author       TEXT,
    rating       REAL,
    content      TEXT,
    date         TEXT,
    likes        INTEGER DEFAULT 0,
    content_hash TEXT,
    sentiment    TEXT,
    theme_id     INTEGER,
    collected_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(content_hash)
);

CREATE TABLE IF NOT EXISTS ai_themes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword_id          INTEGER REFERENCES keywords(id),
    run_id              INTEGER REFERENCES collection_runs(id),
    platform            TEXT,
    theme_name          TEXT,
    theme_desc          TEXT,
    item_count          INTEGER DEFAULT 0,
    pos_ratio           REAL DEFAULT 0,
    neu_ratio           REAL DEFAULT 0,
    neg_ratio           REAL DEFAULT 0,
    representative_items TEXT,
    created_at          TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS ai_analysis (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword_id    INTEGER REFERENCES keywords(id),
    run_id        INTEGER REFERENCES collection_runs(id),
    analysis_type TEXT,
    platform      TEXT,
    target_id     TEXT,
    result_text   TEXT,
    sentiment     TEXT,
    created_at    TEXT DEFAULT (datetime('now', 'localtime'))
);
"""


def init_db(db_path: str = "dashboard.db"):
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
