"""
YouTube 라이브 채팅 분석 파이프라인
감성 태깅 → 시간대별 집계 → 전체 흐름 요약 → 하이라이트 → 인사이트 카드
"""

import json
import math
import re
import time

import anthropic

HAIKU      = "claude-haiku-4-5"
SONNET     = "claude-sonnet-4-6"
BATCH_SIZE = 30
MAX_CHARS  = 200
SAMPLE_MAX = 200   # 요약용 샘플 수
N_BUCKETS  = 20    # 시간대 구간 수
N_HIGHLIGHT = 3    # 하이라이트 구간 수


# ── 공통 유틸 ─────────────────────────────────────────────────

def _client(project_id: str, region: str):
    return anthropic.AnthropicVertex(project_id=project_id, region=region)


def _trunc(text: str, max_chars: int = MAX_CHARS) -> str:
    s = str(text).strip()
    return s[:max_chars] + "..." if len(s) > max_chars else s


def _parse_json(text: str):
    text = re.sub(r"```(?:json)?\s*", "", text).replace("```", "").strip()
    match = re.search(r"[\[\{].*[\]\}]", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except Exception:
            pass
    try:
        return json.loads(text)
    except Exception:
        return None


# ── 1. 감성 태깅 ──────────────────────────────────────────────

def tag_sentiment(client, chats: list, log_fn=None, on_batch=None) -> list:
    """Haiku 배치 감성 태깅 → 'sentiment' 키 추가"""
    tagged = [dict(c) for c in chats]
    total_batches = math.ceil(len(tagged) / BATCH_SIZE)

    for i in range(0, len(tagged), BATCH_SIZE):
        batch     = tagged[i: i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        lines = "\n".join(
            f"{j+1}. {_trunc(str(item.get('메시지', '')))}"
            for j, item in enumerate(batch)
        )
        prompt = (
            "다음 유튜브 라이브 채팅 메시지들의 감성을 분류하세요.\n"
            "반드시 JSON 배열만 출력하세요.\n"
            "감성 값: 긍정, 중립, 부정 중 하나\n\n"
            f"채팅:\n{lines}\n\n"
            '형식: [{"id": 1, "sentiment": "긍정"}, ...]'
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


# ── 2. 시간대별 집계 ──────────────────────────────────────────

def build_timeline(chats: list) -> list:
    """N_BUCKETS 구간으로 나누어 볼륨·감성 집계"""
    if not chats:
        return []

    bucket_size = max(1, math.ceil(len(chats) / N_BUCKETS))
    avg_vol     = len(chats) / N_BUCKETS
    buckets     = []

    for i in range(0, len(chats), bucket_size):
        bucket = chats[i: i + bucket_size]
        if not bucket:
            continue

        pos   = sum(1 for c in bucket if c.get("sentiment") == "긍정")
        neg   = sum(1 for c in bucket if c.get("sentiment") == "부정")
        neu   = sum(1 for c in bucket if c.get("sentiment") == "중립")
        total = len(bucket)

        label = str(bucket[0].get("시간", f"구간 {len(buckets)+1}"))

        buckets.append({
            "label":   label,
            "total":   total,
            "pos":     pos,
            "neg":     neg,
            "neu":     neu,
            "pos_pct": round(pos / total * 100) if total else 0,
            "neg_pct": round(neg / total * 100) if total else 0,
            "neu_pct": round(neu / total * 100) if total else 0,
            "is_peak": total > avg_vol * 1.4,
        })

    return buckets


# ── 3. 하이라이트 구간 추출 ───────────────────────────────────

def find_highlights(chats: list, buckets: list, n: int = N_HIGHLIGHT) -> list:
    """볼륨 × (1 + 긍정비율) 점수 기준 상위 N 구간"""
    if not buckets:
        return []

    bucket_size = max(1, math.ceil(len(chats) / N_BUCKETS))
    scored = sorted(
        enumerate(buckets),
        key=lambda x: x[1]["total"] * (1 + x[1]["pos_pct"] / 100),
        reverse=True,
    )

    highlights = []
    for idx, b in scored[:n]:
        start  = idx * bucket_size
        sample = chats[start: start + bucket_size]
        highlights.append({
            "label":   b["label"],
            "volume":  b["total"],
            "pos_pct": b["pos_pct"],
            "neg_pct": b["neg_pct"],
            "sample":  [c.get("메시지", "") for c in sample[:5] if c.get("메시지")],
        })

    return highlights


# ── 4. 전체 요약 + 인사이트 생성 ─────────────────────────────

def summarize_live(client, chats: list, highlights: list, log_fn=None) -> dict:
    """Sonnet: 라이브 전체 요약 + 하이라이트 설명 + 인사이트 카드"""
    # 균등 샘플링
    step   = max(1, len(chats) // SAMPLE_MAX)
    sample = chats[::step][:SAMPLE_MAX]

    chat_text = "\n".join(
        f"[{c.get('시간', '')}] {_trunc(c.get('메시지', ''), 150)}"
        for c in sample
    )

    hi_text = ""
    for h in highlights:
        hi_text += f"\n[{h['label']}] 볼륨:{h['volume']}건 / 긍정:{h['pos_pct']}%\n"
        for s in h["sample"][:3]:
            hi_text += f"  - {s}\n"

    # 전체 감성 비율
    total = len(chats)
    pos_total = sum(1 for c in chats if c.get("sentiment") == "긍정")
    neg_total = sum(1 for c in chats if c.get("sentiment") == "부정")
    neu_total = total - pos_total - neg_total
    sent_summary = (
        f"전체 {total}건 / "
        f"긍정 {round(pos_total/total*100) if total else 0}% / "
        f"중립 {round(neu_total/total*100) if total else 0}% / "
        f"부정 {round(neg_total/total*100) if total else 0}%"
    )

    prompt = (
        "다음은 유튜브 라이브 방송의 채팅 데이터입니다.\n"
        "게임 마케팅 담당자를 위한 라이브 방송 분석 보고서를 작성하세요.\n\n"
        f"채팅 샘플 (시간순):\n{chat_text}\n\n"
        f"감성 요약: {sent_summary}\n\n"
        f"반응 피크 구간:\n{hi_text}\n\n"
        "반드시 JSON만 출력하세요.\n"
        '형식: {\n'
        '  "summary": "이 라이브 방송이 어떤 내용이었는지 3~4문장 요약",\n'
        '  "flow": "방송 흐름 (초반~중반~후반) 설명 2~3문장",\n'
        '  "highlights": [\n'
        '    {"moment": "구간/시간대", "reason": "반응 좋았던 이유 1~2문장",\n'
        '     "sentiment": "긍정/혼재/부정", "sample": "대표 채팅 1개"}\n'
        '  ],\n'
        '  "topics": ["주요 토픽1", "주요 토픽2", "주요 토픽3"],\n'
        '  "overall_sentiment": "전반적인 감성 평가 1문장",\n'
        '  "cards": [\n'
        '    {"title": "카드 제목", "sentiment": "긍정/부정/혼재",\n'
        '     "insight": "인사이트 2~3문장", "evidence": "대표 사례 또는 수치",\n'
        '     "action": "마케팅 관점 시사점 1문장"}\n'
        '  ]\n'
        '}'
    )

    try:
        resp = client.messages.create(
            model=SONNET, max_tokens=4000,
            messages=[{"role": "user", "content": prompt}]
        )
        result = _parse_json(resp.content[0].text)
        if result:
            if log_fn:
                log_fn(f"  요약 및 인사이트 {len(result.get('cards', []))}개 생성 완료")
            return result
    except Exception as e:
        if log_fn:
            log_fn(f"  ⚠️ 요약 오류: {e}")
    return {}


# ── 전체 파이프라인 ───────────────────────────────────────────

def run_live_analysis(project_id: str, region: str, chats: list,
                      log_fn=None, progress_fn=None) -> dict:
    """
    라이브 채팅 전체 분석.
    반환: tagged_chats, buckets, highlights, analysis
    """
    def _progress(pct: int, msg: str):
        if progress_fn:
            progress_fn(min(pct, 100), msg)

    client        = _client(project_id, region)
    total_batches = math.ceil(len(chats) / BATCH_SIZE)
    sent_done     = [0]

    # 1. 감성 태깅 (0 → 50%)
    if log_fn:
        log_fn(f"😊 감성 태깅 시작... ({len(chats)}건)")
    _progress(0, "감성 태깅 중...")

    def on_batch(batch_num, total_b):
        sent_done[0] += 1
        pct = int(sent_done[0] / max(total_batches, 1) * 50)
        _progress(pct, f"감성 태깅 중... ({sent_done[0]}/{total_batches} 배치)")

    tagged = tag_sentiment(client, chats, log_fn, on_batch)

    # 2. 시간대 집계 (50 → 60%)
    if log_fn:
        log_fn("📊 시간대별 집계 중...")
    _progress(55, "시간대별 집계 중...")
    buckets    = build_timeline(tagged)
    highlights = find_highlights(tagged, buckets)

    # 3. 요약 + 인사이트 (60 → 100%)
    if log_fn:
        log_fn("💡 라이브 요약 및 인사이트 생성 중...")
    _progress(60, "라이브 내용 분석 중...")
    analysis = summarize_live(client, tagged, highlights, log_fn)

    _progress(100, "분석 완료!")
    if log_fn:
        log_fn("✅ 라이브 분석 완료!")

    return {
        "tagged_chats": tagged,
        "buckets":      buckets,
        "highlights":   highlights,
        "analysis":     analysis,
    }
