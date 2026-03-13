"""수집기 오케스트레이터 — keyword_crawler.py 클래스 재사용"""
import re
import sys
import os

# 같은 디렉토리(crawlers/)에 복사된 keyword_crawler 사용
sys.path.insert(0, os.path.dirname(__file__))

try:
    from keyword_crawler import (
        YouTubeCrawler, YouTubeLiveCrawler, DCInsideCrawler,
        AppStoreCrawler, PlayStoreCrawler
    )
    CRAWLERS_AVAILABLE = True
except ImportError as e:
    CRAWLERS_AVAILABLE = False
    IMPORT_ERROR = str(e)

from database import db
from utils.preprocessor import filter_noise


def extract_video_id(url: str) -> str | None:
    """YouTube URL에서 video_id(11자리) 추출"""
    if not url:
        return None
    patterns = [
        r'youtube\.com/watch\?(?:.*&)?v=([a-zA-Z0-9_-]{11})',
        r'youtu\.be/([a-zA-Z0-9_-]{11})',
        r'youtube\.com/live/([a-zA-Z0-9_-]{11})',
        r'youtube\.com/shorts/([a-zA-Z0-9_-]{11})',
    ]
    for pattern in patterns:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


def run_collection(keyword: str, config: dict, log_fn) -> dict:
    """
    전체 수집 실행 후 결과 반환.

    config 키:
        use_youtube, yt_api_key, yt_urls        (URL 목록)
        use_live,    live_url                   (라이브채팅)
        use_dc,      dc_gallery_id, dc_is_minor, dc_max_pages
        use_appstore, appstore_id
        use_playstore, playstore_id
    """
    if not CRAWLERS_AVAILABLE:
        log_fn(f"❌ 수집기 로드 실패: {IMPORT_ERROR}")
        return {}

    results = {
        "yt_videos": [], "yt_comments": [], "yt_live": [],
        "dc_posts": [], "appstore": [], "playstore": [],
        "yt_count": 0, "dc_count": 0, "app_count": 0, "play_count": 0,
        "dc_blocked": False,
    }

    # DB 등록
    keyword_id = db.get_or_create_keyword(
        keyword,
        appstore_id=config.get("appstore_id"),
        playstore_id=config.get("playstore_id"),
        dc_gallery_id=config.get("dc_gallery_id"),
        dc_is_minor=config.get("dc_is_minor", True),
    )
    run_id = db.create_run(keyword_id, period_filter=None)

    # ── YouTube 댓글 (URL 기반) ───────────────────────────────
    yt_urls = [u for u in config.get("yt_urls", []) if u.strip()]
    if config.get("use_youtube") and config.get("yt_api_key") and yt_urls:
        log_fn("📺 YouTube 댓글 수집 시작...")
        try:
            yt = YouTubeCrawler(config["yt_api_key"])
            videos, comments = [], []

            for idx, url in enumerate(yt_urls, 1):
                video_id = extract_video_id(url)
                if not video_id:
                    log_fn(f"  ⚠️ URL {idx}: video_id를 추출할 수 없습니다 — {url}")
                    continue

                log_fn(f"  [{idx}/{len(yt_urls)}] 영상 정보 수집 중... ({video_id})")
                info = yt.get_video_info_by_id(video_id, callback=log_fn)
                if not info:
                    log_fn(f"  ⚠️ 영상 정보 없음: {video_id}")
                    continue

                info["순위"] = idx
                videos.append(info)

                log_fn(f"  [{idx}/{len(yt_urls)}] 댓글 수집 중: {info['영상 제목'][:30]}...")
                v_cmts = yt.get_all_comments(video_id, info["영상 제목"], callback=log_fn)
                comments.extend(v_cmts)

            comments = filter_noise(comments, "댓글")

            db.insert_youtube_videos(keyword_id, videos)
            db.insert_youtube_comments(keyword_id, comments)

            results["yt_videos"]   = videos
            results["yt_comments"] = comments
            results["yt_count"]    = len(videos)
            log_fn(f"✅ YouTube 완료: 영상 {len(videos)}개, 댓글 {len(comments)}개")

        except Exception as e:
            log_fn(f"❌ YouTube 오류: {e}")

    # ── YouTube 라이브채팅 ────────────────────────────────────
    if config.get("use_live") and config.get("live_url"):
        log_fn("🎬 YouTube 라이브채팅 수집 시작...")
        try:
            live_video_id = extract_video_id(config["live_url"])
            if not live_video_id:
                log_fn("⚠️ 라이브 URL에서 video_id를 추출할 수 없습니다.")
            else:
                log_fn(f"  video_id: {live_video_id} (아카이브 채팅 수집)")
                chats = YouTubeLiveCrawler().collect(live_video_id, callback=log_fn)
                db.insert_youtube_live_chat(keyword_id, live_video_id, chats)
                results["yt_live"] = chats
                log_fn(f"✅ 라이브채팅 완료: {len(chats)}개")

        except ImportError:
            log_fn("⚠️ pytchat 미설치 — 라이브채팅 수집 건너뜀 (pip install pytchat)")
        except Exception as e:
            log_fn(f"❌ 라이브채팅 오류: {e}")

    # ── 디시인사이드 ──────────────────────────────────────────
    if config.get("use_dc"):
        log_fn("💬 디시인사이드 수집 시작...")
        try:
            dc = DCInsideCrawler(days_limit=365)
            posts = dc.crawl_all(
                "",
                gallery_id=config.get("dc_gallery_id"),
                is_minor=config.get("dc_is_minor", True),
                max_pages=config.get("dc_max_pages", 10),
                callback=_dc_log_wrapper(log_fn, results),
            )
            posts = filter_noise(posts, "본문")

            if not results["dc_blocked"]:
                db.insert_dc_posts(keyword_id, posts)
                results["dc_posts"] = posts
                results["dc_count"] = len(posts)
                log_fn(f"✅ 디시 완료: {len(posts)}개")

        except Exception as e:
            _handle_dc_error(e, log_fn, results)

    # ── 앱스토어 ─────────────────────────────────────────────
    if config.get("use_appstore") and config.get("appstore_id"):
        log_fn("🍎 앱스토어 수집 시작...")
        try:
            reviews = AppStoreCrawler().crawl(
                config["appstore_id"], max_pages=10, callback=log_fn
            )
            reviews = filter_noise(reviews, "내용")

            db.insert_appstore_reviews(keyword_id, reviews)
            results["appstore"]  = reviews
            results["app_count"] = len(reviews)
            log_fn(f"✅ 앱스토어 완료: {len(reviews)}개")

        except Exception as e:
            log_fn(f"❌ 앱스토어 오류: {e}")

    # ── 플레이스토어 ──────────────────────────────────────────
    if config.get("use_playstore") and config.get("playstore_id"):
        log_fn("🤖 플레이스토어 수집 시작...")
        try:
            reviews = PlayStoreCrawler().crawl(
                config["playstore_id"], max_reviews=500, callback=log_fn
            )
            reviews = filter_noise(reviews, "내용")

            db.insert_playstore_reviews(keyword_id, reviews)
            results["playstore"]  = reviews
            results["play_count"] = len(reviews)
            log_fn(f"✅ 플레이스토어 완료: {len(reviews)}개")

        except Exception as e:
            log_fn(f"❌ 플레이스토어 오류: {e}")

    # ── 완료 ─────────────────────────────────────────────────
    db.complete_run(run_id, {
        "yt": results["yt_count"], "dc": results["dc_count"],
        "app": results["app_count"], "play": results["play_count"],
    })

    results["keyword_id"] = keyword_id
    results["run_id"]     = run_id

    total = (results["yt_count"] + results["dc_count"]
             + results["app_count"] + results["play_count"])
    log_fn(
        f"\n🎉 수집 완료! 총 {total}건 "
        f"(YouTube {results['yt_count']} / 디시 {results['dc_count']} / "
        f"앱스토어 {results['app_count']} / 플레이스토어 {results['play_count']})"
    )
    return results


# ── 헬퍼 ─────────────────────────────────────────────────────

def _dc_log_wrapper(log_fn, results: dict):
    """DC 로그 콜백 — 차단 감지 시 플래그 설정"""
    def callback(msg):
        if "403" in msg or "IP 제한" in msg:
            if not results["dc_blocked"]:
                results["dc_blocked"] = True
                log_fn("⚠️ 디시 IP 차단 감지 — 다른 플랫폼 수집은 계속 진행됩니다")
        else:
            log_fn(msg)
    return callback


def _handle_dc_error(e: Exception, log_fn, results: dict):
    err = str(e)
    if "403" in err or "차단" in err or "block" in err.lower():
        results["dc_blocked"] = True
        log_fn("⚠️ 디시 IP 차단 — 수집 건너뜀")
    else:
        log_fn(f"❌ 디시 오류: {e}")
