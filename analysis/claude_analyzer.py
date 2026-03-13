"""
Claude Vertex AI 분석 파이프라인
감성 태깅 → 플랫폼별 테마 발굴 → 크로스플랫폼 통합 → 테마 매핑 → 인사이트 생성
"""

import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

MAX_WORKERS = 4  # 동시 API 요청 수 (rate limit 고려)

import anthropic

HAIKU  = "claude-haiku-4-5"   # 감성 태깅, 테마 매핑 (배치)
SONNET = "claude-sonnet-4-6"  # 테마 발굴, 통합, 인사이트 (품질 우선)

BATCH_SIZE     = 30   # 배치당 항목 수
MAX_TEXT_CHARS = 300  # 항목당 최대 텍스트 길이
SAMPLE_MAX     = 100  # 테마 발굴용 샘플 수


# ── 클라이언트 ────────────────────────────────────────────────

def _client(project_id: str, region: str):
    return anthropic.AnthropicVertex(project_id=project_id, region=region)


# ── 유틸 ─────────────────────────────────────────────────────

def _trunc(text: str, max_chars: int = MAX_TEXT_CHARS) -> str:
    s = str(text).strip()
    return s[:max_chars] + "..." if len(s) > max_chars else s


def _parse_json(text: str):
    """Claude 응답에서 JSON 추출 (마크다운 코드블록 허용)"""
    text = re.sub(r"```(?:json)?\s*", "", text).replace("```", "").strip()

    # 직접 파싱 먼저 시도
    try:
        return json.loads(text)
    except Exception:
        pass

    # { ... } 추출 시도 (첫 { ~ 마지막 })
    start = text.find('{')
    if start != -1:
        end = text.rfind('}')
        if end > start:
            try:
                return json.loads(text[start:end + 1])
            except Exception:
                pass

    # [ ... ] 추출 시도 (첫 [ ~ 마지막 ])
    start = text.find('[')
    if start != -1:
        end = text.rfind(']')
        if end > start:
            try:
                return json.loads(text[start:end + 1])
            except Exception:
                pass

    return None


def _sample(items: list, max_items: int = SAMPLE_MAX) -> list:
    """균등 간격 샘플링"""
    if len(items) <= max_items:
        return items
    step = max(1, len(items) // max_items)
    return items[::step][:max_items]


def _first_text(item: dict) -> str:
    """항목에서 텍스트 추출 (여러 키 순차 시도)"""
    for key in ("댓글", "본문", "내용", "제목", "message"):
        val = item.get(key, "")
        if val:
            return str(val)
    return ""


# ── 1. 감성 태깅 ──────────────────────────────────────────────

def tag_sentiment_batch(client, items: list, text_key: str,
                        log_fn=None, on_batch=None) -> list:
    """배치 30개씩 감성 태깅 → 각 항목에 'sentiment' 키 추가"""
    tagged = [dict(item) for item in items]
    total_batches = math.ceil(len(tagged) / BATCH_SIZE)

    for i in range(0, len(tagged), BATCH_SIZE):
        batch = tagged[i: i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        lines = "\n".join(
            f"{j+1}. {_trunc(str(item.get(text_key, '')))}"
            for j, item in enumerate(batch)
        )
        prompt = (
            f"다음 텍스트들의 감성을 분류하세요.\n"
            f"반드시 JSON 배열만 출력하고 다른 텍스트는 쓰지 마세요.\n"
            f"감성 값: 긍정, 중립, 부정 중 하나\n\n"
            f"텍스트:\n{lines}\n\n"
            f'형식: [{{"id": 1, "sentiment": "긍정"}}, ...]'
        )
        try:
            resp = client.messages.create(
                model=HAIKU, max_tokens=800,
                messages=[{"role": "user", "content": prompt}]
            )
            result = _parse_json(resp.content[0].text)
            if isinstance(result, list):
                for r in result:
                    idx = r.get("id", 0) - 1
                    if 0 <= idx < len(batch):
                        batch[idx]["sentiment"] = r.get("sentiment", "중립")
        except Exception as e:
            if log_fn:
                log_fn(f"  ⚠️ 감성 배치 오류: {e}")
        time.sleep(0.2)

        done = min(i + BATCH_SIZE, len(tagged))
        if log_fn:
            log_fn(f"  감성 태깅 {done}/{len(tagged)}건 완료")
        if on_batch:
            on_batch(batch_num, total_batches)

    return tagged


def tag_sentiment_individual(client, items: list, text_key: str, log_fn=None) -> list:
    """건별 감성 태깅 (정밀 모드)"""
    tagged = [dict(item) for item in items]

    for i, item in enumerate(tagged):
        text = _trunc(str(item.get(text_key, "")), 500)
        prompt = (
            f"다음 텍스트의 감성을 분류하세요. 반드시 JSON만 출력하세요.\n"
            f"텍스트: {text}\n"
            f'응답: {{"sentiment": "긍정"}}  (긍정/중립/부정 중 하나)'
        )
        try:
            resp = client.messages.create(
                model=HAIKU, max_tokens=50,
                messages=[{"role": "user", "content": prompt}]
            )
            result = _parse_json(resp.content[0].text)
            if result:
                item["sentiment"] = result.get("sentiment", "중립")
        except Exception as e:
            if log_fn:
                log_fn(f"  ⚠️ 감성 개별 오류 ({i}): {e}")
        time.sleep(0.1)

        if log_fn and (i + 1) % 50 == 0:
            log_fn(f"  감성 태깅 {i+1}/{len(tagged)}건 완료")

    return tagged


# ── 2. 플랫폼별 테마 발굴 ─────────────────────────────────────

def discover_themes(client, keyword: str, platform: str,
                    items: list, text_key: str, log_fn=None) -> list:
    """플랫폼 데이터 맥락 기반 동적 테마 5~10개 발굴"""
    sample = _sample(items)
    if not sample:
        return []

    sample_text = "\n".join(
        f"- {_trunc(str(item.get(text_key, '')), 200)}"
        for item in sample
    )
    prompt = (
        f'다음은 게임 키워드 "{keyword}"에 대한 {platform} 데이터입니다.\n'
        f"이 데이터를 읽고 현재 주요 동향/이슈를 5~10개 테마로 분류해주세요.\n\n"
        f"중요: 데이터의 실제 맥락을 기반으로 테마를 도출하세요.\n"
        f"고정 카테고리가 아닌, 이 데이터에서 실제 논의되는 주제를 반영해야 합니다.\n\n"
        f"데이터:\n{sample_text}\n\n"
        f"반드시 JSON 배열만 출력하세요.\n"
        f'형식: [{{"name": "테마명", "desc": "설명 1~2문장", "keywords": ["키워드1"]}}]'
    )
    try:
        resp = client.messages.create(
            model=SONNET, max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        themes = _parse_json(resp.content[0].text)
        if isinstance(themes, list):
            if log_fn:
                log_fn(f"  {platform} 테마 {len(themes)}개 발굴 완료")
            return themes
    except Exception as e:
        if log_fn:
            log_fn(f"  ⚠️ {platform} 테마 발굴 오류: {e}")
    return []


# ── 3. 크로스플랫폼 테마 통합 ────────────────────────────────

def synthesize_themes(client, keyword: str, platform_themes: dict, log_fn=None) -> list:
    """플랫폼별 테마 → 크로스플랫폼 통합 테마 5~8개"""
    if not any(platform_themes.values()):
        return []

    themes_text = ""
    for platform, themes in platform_themes.items():
        if themes:
            themes_text += f"\n[{platform}]\n"
            for t in themes:
                themes_text += f"- {t.get('name')}: {t.get('desc', '')}\n"

    prompt = (
        f'다음은 "{keyword}" 키워드에 대한 플랫폼별 동향 테마입니다.\n'
        f"이를 통합하여 핵심 크로스플랫폼 테마 5~8개를 도출하세요.\n"
        f"여러 플랫폼에서 공통으로 보이는 이슈를 우선 반영하세요.\n\n"
        f"플랫폼별 테마:\n{themes_text}\n\n"
        f"반드시 JSON 배열만 출력하세요.\n"
        f'형식: [{{"name": "테마명", "desc": "설명", "platforms": ["플랫폼"], "keywords": ["키워드"]}}]'
    )
    try:
        resp = client.messages.create(
            model=SONNET, max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        themes = _parse_json(resp.content[0].text)
        if isinstance(themes, list):
            if log_fn:
                log_fn(f"  통합 테마 {len(themes)}개 도출 완료")
            return themes
    except Exception as e:
        if log_fn:
            log_fn(f"  ⚠️ 테마 통합 오류: {e}")
    return []


# ── 4. 테마 매핑 ─────────────────────────────────────────────

def map_themes(client, items: list, text_key: str, themes: list,
               log_fn=None, on_batch=None) -> list:
    """각 항목을 통합 테마 중 하나로 분류"""
    if not themes:
        return items

    theme_names = [t.get("name") for t in themes]
    theme_list  = "\n".join(
        f"{i+1}. {t.get('name')}: {t.get('desc', '')}"
        for i, t in enumerate(themes)
    )
    tagged = [dict(item) for item in items]
    total_batches = math.ceil(len(tagged) / BATCH_SIZE)

    for i in range(0, len(tagged), BATCH_SIZE):
        batch = tagged[i: i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        lines = "\n".join(
            f"{j+1}. {_trunc(str(item.get(text_key, '')))}"
            for j, item in enumerate(batch)
        )
        prompt = (
            f"다음 항목들을 테마 목록 중 가장 적합한 하나로 분류하세요.\n"
            f"해당 없으면 '기타'로 분류하세요.\n"
            f"반드시 JSON 배열만 출력하세요.\n\n"
            f"테마 목록:\n{theme_list}\n\n"
            f"항목:\n{lines}\n\n"
            f'형식: [{{"id": 1, "theme": "테마명"}}]'
        )
        try:
            resp = client.messages.create(
                model=HAIKU, max_tokens=1500,
                messages=[{"role": "user", "content": prompt}]
            )
            result = _parse_json(resp.content[0].text)
            if isinstance(result, list):
                for r in result:
                    idx = r.get("id", 0) - 1
                    if 0 <= idx < len(batch):
                        theme = r.get("theme", "기타")
                        # 정확히 일치하지 않으면 가장 유사한 테마명으로 매핑
                        if theme not in theme_names:
                            matched = next(
                                (tn for tn in theme_names if tn in theme or theme in tn), "기타"
                            )
                            theme = matched
                        batch[idx]["theme"] = theme
            elif log_fn:
                log_fn(f"  ⚠️ 테마 매핑 배치 {batch_num} JSON 파싱 실패")
        except Exception as e:
            if log_fn:
                log_fn(f"  ⚠️ 테마 매핑 배치 오류: {e}")
        time.sleep(0.2)
        if on_batch:
            on_batch(batch_num, total_batches)

    return tagged


# ── 5. 인사이트 생성 ──────────────────────────────────────────

def generate_insights(client, keyword: str, unified_themes: list,
                      items_by_platform: dict, log_fn=None) -> dict:
    """테마별 집계 + 대표 사례 → 마케팅 인사이트 카드"""
    stats = _aggregate_theme_stats(unified_themes, items_by_platform)
    if not stats:
        if log_fn:
            theme_count = len(unified_themes)
            item_count = sum(len(v) for v in items_by_platform.values())
            themed_count = sum(
                1 for items in items_by_platform.values()
                for item in items if item.get("theme")
            )
            log_fn(f"  ⚠️ 테마 매핑 데이터 부족 ({themed_count}/{item_count}건). 원본 데이터로 인사이트 생성 시도...")
        # 폴백: 테마 통계 없이 원본 텍스트 샘플로 인사이트 생성
        return _generate_insights_fallback(client, keyword, unified_themes, items_by_platform, log_fn)

    stats_text = ""
    for s in stats:
        stats_text += (
            f"\n【{s['name']}】 {s['count']}건\n"
            f"  감성: 긍정 {s['pos']}% / 중립 {s['neu']}% / 부정 {s['neg']}%\n"
            f"  대표 사례:\n"
        )
        for ex in s.get("examples", [])[:3]:
            stats_text += f"    - {ex}\n"

    prompt = (
        f'다음은 게임 키워드 "{keyword}"에 대한 멀티플랫폼 동향 분석 데이터입니다.\n'
        f"게임 마케팅 담당자를 위한 인사이트 카드를 작성하세요.\n\n"
        f"분석 데이터:\n{stats_text}\n\n"
        f"반드시 JSON만 출력하세요.\n"
        f'형식:\n{{'
        f'"summary": "전체 동향 요약 2~3문장",\n'
        f'"cards": [\n'
        f'  {{"title": "카드 제목", "sentiment": "긍정/부정/혼재",\n'
        f'   "insight": "인사이트 2~3문장", "evidence": "대표 사례 또는 수치",\n'
        f'   "action": "마케팅 관점 시사점 1문장"}}\n'
        f"]}}"
    )
    try:
        resp = client.messages.create(
            model=SONNET, max_tokens=6000,
            messages=[{"role": "user", "content": prompt}]
        )
        result = _parse_json(resp.content[0].text)
        if isinstance(result, dict):
            if log_fn:
                log_fn(f"  인사이트 카드 {len(result.get('cards', []))}개 생성 완료")
            return result
        if log_fn:
            log_fn(f"  ⚠️ 인사이트 JSON 파싱 실패 (응답: {resp.content[0].text[:200]})")
    except Exception as e:
        if log_fn:
            log_fn(f"  ⚠️ 인사이트 생성 오류: {e}")
    return {}


# ── 빠른 요약 ────────────────────────────────────────────────

def run_quick_summary(project_id: str, region: str,
                      collection_result: dict, log_fn=None) -> dict:
    """
    수집 데이터 샘플 → Sonnet 1회 호출 → 빠른 동향 요약.
    감성/토픽은 Claude 추정치 (정량 통계 아님).
    반환: summary, sentiment, sentiment_reason, hot_topics, watch_point,
          sample_count, total_count
    """
    client = _client(project_id, region)

    # 플랫폼별 텍스트 수집
    all_items = []
    for key, field, plat in [
        ("yt_comments", "댓글",   "YouTube"),
        ("dc_posts",    "본문",   "디시인사이드"),
        ("appstore",    "내용",   "앱스토어"),
        ("playstore",   "내용",   "플레이스토어"),
    ]:
        for item in collection_result.get(key, []):
            text = str(item.get(field, "")).strip()
            if len(text) >= 10:
                all_items.append(f"[{plat}] {text[:200]}")

    total_count = len(all_items)
    if not all_items:
        return {}

    # 균등 샘플링 (최대 200건)
    step   = max(1, len(all_items) // 200)
    sample = all_items[::step][:200]

    text_block = "\n".join(f"- {t}" for t in sample)

    prompt = (
        f"다음은 게임 관련 커뮤니티·리뷰 데이터 {len(sample)}건 샘플입니다.\n"
        "핵심 동향을 빠르게 파악해주세요. 반드시 JSON만 출력하세요.\n\n"
        f"데이터:\n{text_block}\n\n"
        '형식:\n'
        '{\n'
        '  "summary": "전반적인 동향 요약 2~3문장",\n'
        '  "sentiment": "긍정/부정/혼재 중 하나",\n'
        '  "sentiment_reason": "감성 방향 판단 이유 1문장",\n'
        '  "hot_topics": [\n'
        '    {"rank": 1, "topic": "주제명", "desc": "1~2문장 설명", '
        '"sentiment": "긍정/부정/혼재"}\n'
        '  ],\n'
        '  "watch_point": "마케터가 주목해야 할 이슈 또는 리스크 1문장"\n'
        '}'
    )

    try:
        if log_fn:
            log_fn(f"⚡ 빠른 요약 생성 중... (샘플 {len(sample)}건)")
        resp = client.messages.create(
            model=SONNET, max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        result = _parse_json(resp.content[0].text)
        if isinstance(result, dict):
            result["sample_count"] = len(sample)
            result["total_count"]  = total_count
            if log_fn:
                log_fn(f"✅ 빠른 요약 완료 (토픽 {len(result.get('hot_topics', []))}개)")
            return result
        if log_fn:
            log_fn("⚠️ 빠른 요약 JSON 파싱 실패")
    except Exception as e:
        if log_fn:
            log_fn(f"⚠️ 빠른 요약 오류: {e}")
    return {}


# ── 전체 파이프라인 (병렬 처리) ──────────────────────────────

def run_full_analysis(project_id: str, region: str, keyword: str,
                      collection_result: dict, config: dict,
                      log_fn, progress_fn=None) -> dict:
    """
    수집 결과를 받아 전체 분석 파이프라인 실행 (플랫폼 단위 병렬 처리).
    progress_fn(pct: int, msg: str) — 진행률 콜백 (0~100)
    반환값: platform_items, platform_themes, unified_themes, insights
    """
    def _progress(pct: int, msg: str):
        if progress_fn:
            progress_fn(min(pct, 100), msg)

    client = _client(project_id, region)
    use_individual = "건별" in config.get("sentiment_mode", "")
    lock = threading.Lock()

    # 플랫폼별 (items, text_key) 매핑
    platforms = {
        "YouTube 댓글":       (collection_result.get("yt_comments", []), "댓글"),
        "YouTube 라이브채팅": (collection_result.get("yt_live",     []), "메시지"),
        "디시인사이드":       (collection_result.get("dc_posts",    []), "본문"),
        "앱스토어":           (collection_result.get("appstore",    []), "내용"),
        "플레이스토어":       (collection_result.get("playstore",   []), "내용"),
    }
    active = [(p, items, tk) for p, (items, tk) in platforms.items() if items]
    total_sent_batches = sum(math.ceil(len(it) / BATCH_SIZE) for _, it, _ in active)
    total_map_batches  = total_sent_batches
    sent_done = [0]
    map_done  = [0]

    # ── 1. 감성 태깅 — 플랫폼별 병렬 (0 → 40%) ─────────────────
    log_fn("😊 감성 태깅 시작... (병렬)")
    _progress(0, "감성 태깅 중...")

    def _tag_platform(args):
        platform, items, text_key = args
        log_fn(f"  [{platform}] {len(items)}건 감성 태깅 중...")

        def on_batch(batch_num, total_b):
            with lock:
                sent_done[0] += 1
                pct = int(sent_done[0] / max(total_sent_batches, 1) * 40)
            _progress(pct, f"감성 태깅 중... ({sent_done[0]}/{total_sent_batches} 배치)")

        if use_individual:
            return platform, tag_sentiment_individual(client, items, text_key, log_fn), text_key
        else:
            return platform, tag_sentiment_batch(client, items, text_key, log_fn, on_batch=on_batch), text_key

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for platform, tagged, text_key in ex.map(_tag_platform, active):
            platforms[platform] = (tagged, text_key)

    # ── 2. 테마 발굴 — 플랫폼별 병렬 (40 → 55%) ────────────────
    log_fn("🏷 플랫폼별 테마 발굴 시작... (병렬)")
    _progress(40, "테마 발굴 중...")
    platform_themes = {}
    active_platforms = [(p, items, tk) for p, (items, tk) in platforms.items() if items]
    disc_done = [0]

    def _discover_platform(args):
        platform, items, text_key = args
        themes = discover_themes(client, keyword, platform, items, text_key, log_fn)
        with lock:
            disc_done[0] += 1
            pct = 40 + int(disc_done[0] / max(len(active_platforms), 1) * 15)
        _progress(pct, f"테마 발굴 중... ({platform} 완료)")
        return platform, themes

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for platform, themes in ex.map(_discover_platform, active_platforms):
            platform_themes[platform] = themes

    # ── 3. 크로스플랫폼 통합 — 순차 필수 (55 → 65%) ────────────
    log_fn("🔗 크로스플랫폼 테마 통합 중...")
    _progress(55, "크로스플랫폼 테마 통합 중...")
    unified_themes = synthesize_themes(client, keyword, platform_themes, log_fn)
    _progress(65, "테마 통합 완료")

    # ── 4. 테마 매핑 — 플랫폼별 병렬 (65 → 90%) ────────────────
    log_fn("📌 테마 매핑 시작... (병렬)")
    _progress(65, "테마 매핑 중...")
    active_for_map = [(p, items, tk) for p, (items, tk) in platforms.items()
                      if items and unified_themes]

    def _map_platform(args):
        platform, items, text_key = args

        def on_batch(batch_num, total_b):
            with lock:
                map_done[0] += 1
                pct = 65 + int(map_done[0] / max(total_map_batches, 1) * 25)
            _progress(pct, f"테마 매핑 중... ({map_done[0]}/{total_map_batches} 배치)")

        mapped = map_themes(client, items, text_key, unified_themes, log_fn, on_batch=on_batch)
        return platform, mapped, text_key

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for platform, mapped, text_key in ex.map(_map_platform, active_for_map):
            platforms[platform] = (mapped, text_key)

    # ── 5. 인사이트 생성 — 순차 필수 (90 → 100%) ───────────────
    log_fn("💡 인사이트 생성 중...")
    _progress(90, "인사이트 생성 중...")
    items_by_platform = {p: items for p, (items, _) in platforms.items()}
    insights = generate_insights(client, keyword, unified_themes, items_by_platform, log_fn)

    _progress(100, "분석 완료!")
    log_fn("✅ 분석 완료!")
    return {
        "platform_items":  items_by_platform,
        "platform_themes": platform_themes,
        "unified_themes":  unified_themes,
        "insights":        insights,
    }


# ── 내부 집계 헬퍼 ───────────────────────────────────────────

def _aggregate_theme_stats(themes: list, items_by_platform: dict) -> list:
    """테마별 건수 / 감성 비율 / 대표 사례 집계"""
    stats = []
    theme_names = [t.get("name") for t in (themes or [])]

    for theme_name in theme_names:
        count = pos = neu = neg = 0
        examples = []

        for items in items_by_platform.values():
            for item in items:
                if item.get("theme") != theme_name:
                    continue
                count += 1
                s = item.get("sentiment", "중립")
                if s == "긍정":
                    pos += 1
                elif s == "부정":
                    neg += 1
                else:
                    neu += 1
                if len(examples) < 5:
                    text = _first_text(item)
                    if text:
                        examples.append(text[:100])

        if count > 0:
            stats.append({
                "name":     theme_name,
                "count":    count,
                "pos":      round(pos / count * 100),
                "neu":      round(neu / count * 100),
                "neg":      round(neg / count * 100),
                "examples": examples,
            })

    return sorted(stats, key=lambda x: x["count"], reverse=True)


def _generate_insights_fallback(client, keyword: str, unified_themes: list,
                                 items_by_platform: dict, log_fn=None) -> dict:
    """테마 매핑 실패 시 원본 샘플 텍스트로 인사이트 직접 생성"""
    all_items = []
    for plat, items in items_by_platform.items():
        for item in items:
            text = _first_text(item)
            sent = item.get("sentiment", "중립")
            if text:
                all_items.append(f"[{plat}/{sent}] {text[:150]}")

    if not all_items:
        return {}

    sample = _sample(all_items, 150)
    themes_text = "\n".join(
        f"- {t.get('name')}: {t.get('desc', '')}" for t in unified_themes
    ) if unified_themes else "테마 정보 없음"

    prompt = (
        f'다음은 게임 키워드 "{keyword}"에 대한 멀티플랫폼 커뮤니티 데이터 샘플입니다.\n'
        f"게임 마케팅 담당자를 위한 인사이트 카드를 작성하세요.\n\n"
        f"주요 동향 테마:\n{themes_text}\n\n"
        f"데이터 샘플 ({len(sample)}건):\n" + "\n".join(f"- {s}" for s in sample) + "\n\n"
        f"반드시 JSON만 출력하세요.\n"
        f'형식:\n{{'
        f'"summary": "전체 동향 요약 2~3문장",\n'
        f'"cards": [\n'
        f'  {{"title": "카드 제목", "sentiment": "긍정/부정/혼재",\n'
        f'   "insight": "인사이트 2~3문장", "evidence": "대표 사례 또는 수치",\n'
        f'   "action": "마케팅 관점 시사점 1문장"}}\n'
        f"]}}"
    )
    try:
        resp = client.messages.create(
            model=SONNET, max_tokens=6000,
            messages=[{"role": "user", "content": prompt}]
        )
        result = _parse_json(resp.content[0].text)
        if isinstance(result, dict):
            if log_fn:
                log_fn(f"  ✅ 폴백 인사이트 카드 {len(result.get('cards', []))}개 생성 완료")
            return result
        if log_fn:
            log_fn(f"  ⚠️ 폴백 인사이트 JSON 파싱 실패")
    except Exception as e:
        if log_fn:
            log_fn(f"  ⚠️ 폴백 인사이트 생성 오류: {e}")
    return {}
