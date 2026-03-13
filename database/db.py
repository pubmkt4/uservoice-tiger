"""DB 접근 함수"""
import sqlite3
import hashlib
from datetime import datetime

DB_PATH = "dashboard.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_or_create_keyword(keyword, appstore_id=None, playstore_id=None,
                           dc_gallery_id=None, dc_is_minor=True) -> int:
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO keywords (keyword, appstore_id, playstore_id, dc_gallery_id, dc_is_minor)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(keyword) DO UPDATE SET
                appstore_id   = excluded.appstore_id,
                playstore_id  = excluded.playstore_id,
                dc_gallery_id = excluded.dc_gallery_id,
                dc_is_minor   = excluded.dc_is_minor
        """, (keyword, appstore_id, playstore_id, dc_gallery_id, int(dc_is_minor)))
        conn.commit()
        row = conn.execute("SELECT id FROM keywords WHERE keyword = ?", (keyword,)).fetchone()
        return row["id"]


def create_run(keyword_id: int, period_filter: str = None) -> int:
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO collection_runs (keyword_id, started_at, status, period_filter)
            VALUES (?, ?, 'running', ?)
        """, (keyword_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), period_filter))
        conn.commit()
        return cur.lastrowid


def complete_run(run_id: int, counts: dict):
    with get_conn() as conn:
        conn.execute("""
            UPDATE collection_runs
            SET completed_at = ?, status = 'completed',
                yt_count = ?, dc_count = ?, app_count = ?, play_count = ?
            WHERE id = ?
        """, (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            counts.get("yt", 0), counts.get("dc", 0),
            counts.get("app", 0), counts.get("play", 0),
            run_id
        ))
        conn.commit()


def _safe_int(val):
    try:
        return int(str(val).replace(",", "") or 0)
    except Exception:
        return 0


def _content_hash(author: str, date: str, content: str) -> str:
    raw = f"{author}|{date}|{content[:50]}"
    return hashlib.md5(raw.encode()).hexdigest()


def insert_youtube_videos(keyword_id: int, videos: list):
    with get_conn() as conn:
        for v in videos:
            conn.execute("""
                INSERT OR IGNORE INTO youtube_videos
                (keyword_id, video_id, title, channel, subscribers,
                 views, likes, comment_count, upload_date, thumbnail_url, description, link)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                keyword_id, v.get("video_id"), v.get("영상 제목"), v.get("채널명"),
                v.get("채널 구독자"), v.get("조회수_raw", 0),
                _safe_int(v.get("좋아요", 0)), _safe_int(v.get("댓글수", 0)),
                v.get("업로드일"), v.get("썸네일"), v.get("영상 설명"), v.get("링크")
            ))
        conn.commit()


def insert_youtube_comments(keyword_id: int, comments: list):
    with get_conn() as conn:
        for c in comments:
            conn.execute("""
                INSERT OR IGNORE INTO youtube_comments
                (keyword_id, video_id, author, comment, likes, date, type)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                keyword_id, c.get("video_id"), c.get("작성자"),
                c.get("댓글"), c.get("좋아요", 0),
                c.get("작성일"), c.get("유형", "댓글")
            ))
        conn.commit()


def insert_youtube_live_chat(keyword_id: int, video_id: str, chats: list):
    with get_conn() as conn:
        for c in chats:
            conn.execute("""
                INSERT OR IGNORE INTO youtube_live_chat
                (keyword_id, video_id, author, message, chat_time)
                VALUES (?, ?, ?, ?, ?)
            """, (
                keyword_id, video_id, c.get("작성자"),
                c.get("메시지"), str(c.get("시간", ""))
            ))
        conn.commit()


def insert_dc_posts(keyword_id: int, posts: list):
    with get_conn() as conn:
        for p in posts:
            conn.execute("""
                INSERT OR IGNORE INTO dc_posts (keyword_id, title, link, content, date, source)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (keyword_id, p.get("제목"), p.get("링크"),
                  p.get("본문"), p.get("작성일"), p.get("출처")))
        conn.commit()


def insert_appstore_reviews(keyword_id: int, reviews: list):
    with get_conn() as conn:
        for r in reviews:
            conn.execute("""
                INSERT OR IGNORE INTO appstore_reviews
                (keyword_id, review_id, title, author, rating, content, date, version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                keyword_id, r.get("review_id"), r.get("제목"), r.get("작성자"),
                r.get("평점"), r.get("내용"), r.get("작성일"), r.get("버전")
            ))
        conn.commit()


def insert_playstore_reviews(keyword_id: int, reviews: list):
    with get_conn() as conn:
        for r in reviews:
            chash = _content_hash(
                r.get("작성자", ""), r.get("작성일", ""), r.get("내용", "")
            )
            conn.execute("""
                INSERT OR IGNORE INTO playstore_reviews
                (keyword_id, author, rating, content, date, likes, content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                keyword_id, r.get("작성자"), r.get("평점"),
                r.get("내용"), r.get("작성일"), r.get("좋아요", 0), chash
            ))
        conn.commit()


def update_sentiment(table: str, row_id: int, sentiment: str, theme_id: int = None):
    with get_conn() as conn:
        conn.execute(
            f"UPDATE {table} SET sentiment = ?, theme_id = ? WHERE id = ?",
            (sentiment, theme_id, row_id)
        )
        conn.commit()


def save_theme(keyword_id: int, run_id: int, platform: str, theme: dict) -> int:
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO ai_themes
            (keyword_id, run_id, platform, theme_name, theme_desc,
             item_count, pos_ratio, neu_ratio, neg_ratio, representative_items)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            keyword_id, run_id, platform,
            theme.get("name"), theme.get("desc"),
            theme.get("item_count", 0),
            theme.get("pos_ratio", 0), theme.get("neu_ratio", 0), theme.get("neg_ratio", 0),
            theme.get("representative_items", "")
        ))
        conn.commit()
        return cur.lastrowid


def save_analysis(keyword_id: int, run_id: int, analysis_type: str,
                  platform: str, result_text: str, target_id: str = None, sentiment: str = None):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO ai_analysis
            (keyword_id, run_id, analysis_type, platform, target_id, result_text, sentiment)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (keyword_id, run_id, analysis_type, platform, target_id, result_text, sentiment))
        conn.commit()
