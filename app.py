"""
게임 동향 대시보드 v1.0
Streamlit 기반 멀티플랫폼 동향 수집 및 분석 대시보드
"""

import base64
import json
import os
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st
from PIL import Image
from datetime import datetime, timedelta
from database.schema import init_db
from crawlers.collector import run_collection
from analysis.claude_analyzer import run_full_analysis, run_quick_summary, _aggregate_theme_stats
from analysis.live_analyzer import run_live_analysis
from analysis.visualizer import (
    sentiment_by_platform,
    live_sentiment_timeline, live_sentiment_donut,
)
from utils.excel_exporter import generate_excel

# secrets.toml에서 고정값 로드
_YT_API_KEY   = st.secrets.get("youtube", {}).get("api_key", "")
_GCP_PROJECT  = st.secrets.get("gcp", {}).get("project_id", "o-pubmkt4-team")
_GCP_REGION   = st.secrets.get("gcp", {}).get("region", "us-east5")

# ─── 호랑이 PNG 로딩 애니메이션 ──────────────────────────────

def _tiger_b64() -> str:
    try:
        p = Path(__file__).parent / "assets" / "tiger.png"
        if p.exists():
            return base64.b64encode(p.read_bytes()).decode()
    except Exception:
        pass
    return ""


def _tiger_html(msg: str = "수집 중") -> str:
    b64 = _tiger_b64()
    if b64:
        tiger_el = f'<img src="data:image/png;base64,{b64}" class="tig-img">'
    else:
        tiger_el = '<div class="tig-fallback">🐯</div>'
    return f"""
<style>
.tig-wrap {{
    display: flex; flex-direction: row; align-items: center;
    justify-content: center; padding: 40px 0 28px; gap: 32px;
}}
.tig-img {{
    width: 130px; image-rendering: pixelated;
    animation: tig-type 0.55s ease-in-out infinite alternate;
}}
.tig-fallback {{
    font-size: 5rem;
    animation: tig-type 0.55s ease-in-out infinite alternate;
}}
@keyframes tig-type {{
    0%   {{ transform: translateY(0px) rotate(-1deg); }}
    100% {{ transform: translateY(-8px) rotate(1deg); }}
}}
.tig-info {{
    display: flex; flex-direction: column; gap: 14px;
}}
.tig-msg {{
    font-size: 1.15rem; font-weight: 700;
    color: #E8720C; letter-spacing: 2px;
}}
.tig-dot {{
    display: inline-block;
    animation: tig-dot-bounce 1s ease-in-out infinite;
}}
.tig-dot:nth-child(2) {{ animation-delay: .18s; }}
.tig-dot:nth-child(3) {{ animation-delay: .36s; }}
@keyframes tig-dot-bounce {{
    0%,100% {{ transform: translateY(0); }}
    50%     {{ transform: translateY(-5px); }}
}}
.tig-hint {{ font-size: 0.78rem; color: #bbb; margin-top: -4px; }}
div[data-testid="stSpinner"] {{ display: none !important; }}
</style>
<div class="tig-wrap">
  {tiger_el}
  <div class="tig-info">
    <div class="tig-msg">{msg}<span class="tig-dot">.</span><span class="tig-dot">.</span><span class="tig-dot">.</span></div>
    <div class="tig-hint">진행률은 아래 바를 확인하세요</div>
  </div>
</div>"""


# ─── 단계 헤더 헬퍼 ──────────────────────────────────────────

def _step_header(num: int, title: str, status: str) -> str:
    """status: 'pending' | 'ready' | 'done'"""
    if status == "done":
        circle_bg, badge_bg, badge_txt = "#4CAF50", "#E8F5E9", "#2E7D32"
        badge_label = "완료 ✅"
    elif status == "ready":
        circle_bg, badge_bg, badge_txt = "#E8720C", "#FFF3E0", "#BF360C"
        badge_label = "진행 가능 ▶"
    else:
        circle_bg, badge_bg, badge_txt = "#ccc", "#F5F5F5", "#999"
        badge_label = "대기중"
    return (
        f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">'
        f'<div style="width:26px;height:26px;border-radius:50%;background:{circle_bg};'
        f'color:#fff;display:flex;align-items:center;justify-content:center;'
        f'font-weight:700;font-size:0.85rem;flex-shrink:0">{num}</div>'
        f'<span style="font-weight:700;font-size:0.95rem">{title}</span>'
        f'<span style="background:{badge_bg};color:{badge_txt};border-radius:10px;'
        f'padding:1px 9px;font-size:0.72rem;font-weight:600">{badge_label}</span>'
        f'</div>'
    )


# ─── 전역 CSS (타이거 테마) ───────────────────────────────────
_GLOBAL_CSS = """
<style>
/* 수집 시작 버튼 */
[data-testid="stSidebar"] .stButton > button[kind="primary"] {
    background-color: #E8720C !important;
    border-color: #E8720C !important;
}
/* 토글 ON 색상 → 호랑이 주황 */
[data-testid="stToggleSwitch"] > div[aria-checked="true"] {
    background-color: #E8720C !important;
}
</style>
"""

# ─── 대시보드 렌더링 ─────────────────────────────────────────

_SENT_COLOR = {"긍정": "#4CAF50", "부정": "#F44336", "혼재": "#FF9800"}
_SENT_EMOJI = {"긍정": "😊", "부정": "😠", "혼재": "😐"}

_CARD_CSS = """
<style>
/* ── 리포트 헤더 ── */
.rpt-header {
    background: linear-gradient(135deg, #0F2027 0%, #203A43 60%, #2C5364 100%);
    border-radius: 16px; padding: 22px 28px; margin-bottom: 18px; color: white;
}
.rpt-header-kw   { font-size: 1.4rem; font-weight: 800; letter-spacing: 0.3px; }
.rpt-header-meta { font-size: 0.78rem; color: rgba(255,255,255,0.55); margin-top: 4px; }
.rpt-pills       { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 14px;
                   align-items: center; }
.rpt-pill {
    background: rgba(255,255,255,0.13); border-radius: 20px;
    padding: 4px 13px; font-size: 0.74rem; color: rgba(255,255,255,0.8);
}
.rpt-total {
    background: #E8720C; border-radius: 20px;
    padding: 4px 14px; font-size: 0.74rem; font-weight: 700; color: white;
}
/* ── 감성 % 바 ── */
.sent-lbl-row { display: flex; gap: 16px; font-size: 0.73rem; color: #888; margin-bottom: 4px; }
/* ── 요약 박스 ── */
.summary-box {
    background: linear-gradient(135deg, #E8F4FD 0%, #EDE7F6 100%);
    border-radius: 14px; padding: 18px 22px; margin-bottom: 4px;
    font-size: 0.92rem; color: #1A237E; line-height: 1.75;
    border-left: 5px solid #3F51B5;
}
/* ── 인사이트 카드 ── */
.insight-card {
    background: #FFFFFF; border-radius: 14px; padding: 18px 20px;
    box-shadow: 0 3px 16px rgba(0,0,0,0.08); border-left: 5px solid #9E9E9E;
    margin-bottom: 10px;
}
.card-title   { font-size: 0.97rem; font-weight: 700; margin-bottom: 8px; color: #111; }
.card-badge   { display: inline-block; padding: 2px 11px; border-radius: 20px;
                font-size: 0.74rem; font-weight: 600; color: #fff;
                background: #9E9E9E; margin-bottom: 10px; }
.card-insight { font-size: 0.88rem; color: #333; line-height: 1.65; margin-bottom: 8px; }
.card-evidence{ font-size: 0.79rem; color: #555; padding: 7px 11px;
                background: #F8F9FA; border-radius: 7px; border-left: 3px solid #ddd; }
.card-action  { font-size: 0.81rem; color: #1565C0; font-weight: 600;
                margin-top: 9px; padding: 6px 10px;
                background: #EEF5FF; border-radius: 6px; }
/* ── 테마 카드 ── */
.theme-card {
    background: white; border-radius: 14px; padding: 18px 20px;
    box-shadow: 0 2px 10px rgba(0,0,0,0.07); margin-bottom: 12px;
}
.theme-head { display: flex; justify-content: space-between;
              align-items: flex-start; margin-bottom: 4px; }
.theme-name { font-size: 0.96rem; font-weight: 700; color: #111; }
.theme-badge {
    background: #F0F4FF; color: #3F51B5; border-radius: 12px;
    padding: 2px 10px; font-size: 0.71rem; font-weight: 700; white-space: nowrap;
}
.theme-desc { font-size: 0.80rem; color: #666; margin-bottom: 11px; line-height: 1.5; }
.theme-ex   { font-size: 0.79rem; color: #444; background: #FFF8F3;
              border-left: 3px solid #E8720C; border-radius: 0 6px 6px 0;
              padding: 5px 10px; margin-top: 5px; }
/* ── 섹션 제목 ── */
.sec-title {
    font-size: 1.02rem; font-weight: 700; color: #222;
    margin: 0 0 12px; padding-bottom: 8px; border-bottom: 2px solid #F0F0F0;
}
.rpt-divider { border: none; border-top: 2px solid #F0F0F0; margin: 18px 0; }
</style>
"""


def _render_quick_summary(qs: dict):
    """빠른 요약 결과를 컴팩트하게 렌더링"""
    st.markdown(_CARD_CSS, unsafe_allow_html=True)

    sent        = qs.get("sentiment", "혼재")
    sent_color  = _SENT_COLOR.get(sent, "#9E9E9E")
    sent_emoji  = _SENT_EMOJI.get(sent, "😐")
    sample_cnt  = qs.get("sample_count", 0)
    total_cnt   = qs.get("total_count", 0)

    # 배너 헤더
    st.markdown(f"""
<div style="background:linear-gradient(135deg,#1e3a5f 0%,#1a4731 100%);
            border-radius:14px;padding:18px 24px;margin-bottom:16px;color:white;
            display:flex;align-items:center;gap:16px">
  <div style="font-size:2rem;flex-shrink:0">⚡</div>
  <div style="flex:1">
    <div style="font-size:1.05rem;font-weight:700">빠른 동향 요약</div>
    <div style="font-size:0.74rem;color:rgba(255,255,255,0.55);margin-top:3px">
      전체 {total_cnt:,}건 중 샘플 {sample_cnt}건 기반 · Claude AI 정성 추정
    </div>
  </div>
  <div style="background:rgba(255,255,255,0.15);border-radius:20px;
              padding:5px 16px;font-size:0.84rem;font-weight:700;flex-shrink:0">
    {sent_emoji} 전반적 분위기: {sent}
  </div>
</div>""", unsafe_allow_html=True)

    # 요약 + 감성 이유
    if qs.get("summary"):
        reason_html = (
            f'<br><br><span style="font-size:0.82rem;color:#5B21B6;font-style:italic">'
            f'💡 {qs["sentiment_reason"]}</span>'
            if qs.get("sentiment_reason") else ""
        )
        st.markdown(
            f'<div class="summary-box">📋 <b>전체 동향</b><br><br>'
            f'{qs["summary"]}{reason_html}</div>',
            unsafe_allow_html=True
        )

    col_topics, col_watch = st.columns([3, 2])

    # 핫 토픽
    with col_topics:
        st.markdown('<div class="sec-title">🔥 핫 토픽</div>', unsafe_allow_html=True)
        for topic in qs.get("hot_topics", []):
            t_sent  = topic.get("sentiment", "혼재")
            t_color = _SENT_COLOR.get(t_sent, "#9E9E9E")
            t_emoji = _SENT_EMOJI.get(t_sent, "😐")
            st.markdown(f"""
<div style="display:flex;align-items:flex-start;gap:12px;padding:10px 0;
            border-bottom:1px solid #F0F0F0">
  <div style="width:24px;height:24px;border-radius:50%;background:#1e3a5f;color:white;
              font-size:0.72rem;font-weight:700;display:flex;align-items:center;
              justify-content:center;flex-shrink:0">{topic.get('rank','')}</div>
  <div style="flex:1">
    <div style="font-size:0.88rem;font-weight:700;color:#111;margin-bottom:3px">
      {topic.get('topic','')}</div>
    <div style="font-size:0.80rem;color:#555;line-height:1.55">{topic.get('desc','')}</div>
  </div>
  <span style="background:{t_color};color:white;border-radius:10px;
               padding:2px 9px;font-size:0.68rem;font-weight:600;flex-shrink:0;
               white-space:nowrap">{t_emoji} {t_sent}</span>
</div>""", unsafe_allow_html=True)

    # 주목 이슈 + 활용 가이드
    with col_watch:
        if qs.get("watch_point"):
            st.markdown('<div class="sec-title">👀 주목 이슈</div>', unsafe_allow_html=True)
            st.markdown(f"""
<div style="background:#FFF3CD;border-radius:12px;padding:15px 17px;
            border-left:4px solid #F59E0B;font-size:0.87rem;
            color:#78350F;line-height:1.65;margin-bottom:12px">
  ⚠️ {qs['watch_point']}
</div>""", unsafe_allow_html=True)

        st.markdown("""
<div style="background:#F8FAFC;border-radius:10px;padding:13px 15px;
            border:1px solid #E2E8F0;font-size:0.75rem;color:#64748B;line-height:1.8">
  <b>📌 이 결과를 볼 때 꼭 알아두세요</b><br>
  · 샘플 기반 <b>추정치</b> — 수치 그대로 인용 금지<br>
  · 누락된 맥락이 있을 수 있음<br>
  · 정확한 근거가 필요하면 <b>정밀 분석</b> 사용<br>
  · 동향 방향 확인, 보고 전 사전 점검에 적합
</div>""", unsafe_allow_html=True)


def _render_dashboard(ar: dict):
    insights       = ar.get("insights", {})
    unified_themes = ar.get("unified_themes", [])
    items_by_plat  = ar.get("platform_items", {})
    theme_stats    = _aggregate_theme_stats(unified_themes, items_by_plat)

    # 전체 감성 집계
    pos = neu = neg = 0
    for items in items_by_plat.values():
        for item in items:
            s = item.get("sentiment", "중립")
            if s == "긍정":   pos += 1
            elif s == "부정": neg += 1
            else:             neu += 1
    total = pos + neu + neg or 1
    pos_pct = round(pos / total * 100)
    neu_pct = round(neu / total * 100)
    neg_pct = 100 - pos_pct - neu_pct  # 반올림 오차 보정

    platform_counts = {p: len(v) for p, v in items_by_plat.items() if v}

    st.markdown(_CARD_CSS, unsafe_allow_html=True)

    # ── 리포트 헤더 ──────────────────────────────────────────
    pills_html = "".join(
        f'<span class="rpt-pill">{p} {n:,}건</span>'
        for p, n in platform_counts.items()
    )
    st.markdown(f"""
<div class="rpt-header">
  <div class="rpt-header-kw">🐯 게임 동향 분석 리포트</div>
  <div class="rpt-header-meta">{datetime.now().strftime('%Y년 %m월 %d일')} 기준 · Claude AI 분석</div>
  <div class="rpt-pills">{pills_html}<span class="rpt-total">총 {total:,}건</span></div>
</div>""", unsafe_allow_html=True)

    # ── 전체 감성 지표 (3 대형 카드) ─────────────────────────
    m1, m2, m3 = st.columns(3)
    for col, pct, cnt, label, emoji, color, dark in [
        (m1, pos_pct, pos, "긍정", "😊", "#4CAF50", "#2E7D32"),
        (m2, neu_pct, neu, "중립", "😐", "#9E9E9E", "#616161"),
        (m3, neg_pct, neg, "부정", "😠", "#F44336", "#C62828"),
    ]:
        with col:
            st.markdown(f"""
<div style="background:white;border-radius:14px;padding:20px;
            box-shadow:0 3px 14px rgba(0,0,0,0.08);
            border-top:4px solid {color};text-align:center;margin-bottom:8px">
  <div style="font-size:2.2rem;font-weight:800;color:{dark}">{pct}%</div>
  <div style="font-size:0.82rem;color:#888;margin-top:2px">{emoji} {label}</div>
  <div style="font-size:0.78rem;color:#bbb;margin-top:4px">{cnt:,}건</div>
</div>""", unsafe_allow_html=True)

    # 전체 감성 % 바
    st.markdown(f"""
<div style="display:flex;height:10px;border-radius:6px;overflow:hidden;margin:4px 0 6px">
  <div style="width:{pos_pct}%;background:#4CAF50"></div>
  <div style="width:{neu_pct}%;background:#BDBDBD"></div>
  <div style="width:{neg_pct}%;background:#F44336"></div>
</div>
<div class="sent-lbl-row">
  <span style="color:#2E7D32">😊 긍정 {pos_pct}%</span>
  <span style="color:#616161">😐 중립 {neu_pct}%</span>
  <span style="color:#C62828">😠 부정 {neg_pct}%</span>
</div>""", unsafe_allow_html=True)

    st.markdown('<hr class="rpt-divider">', unsafe_allow_html=True)

    # ── 종합 요약 ────────────────────────────────────────────
    if insights.get("summary"):
        st.markdown(
            f'<div class="summary-box">📋 <b>종합 동향 요약</b><br><br>{insights["summary"]}</div>',
            unsafe_allow_html=True
        )
        st.markdown("<div style='height:14px'></div>", unsafe_allow_html=True)

    # ── 인사이트 카드 ────────────────────────────────────────
    st.markdown('<div class="sec-title">💡 핵심 인사이트</div>', unsafe_allow_html=True)
    cards = insights.get("cards", [])
    if not insights:
        st.warning("인사이트 생성에 실패했습니다. 분석 로그를 확인하세요.", icon="⚠️")
        _err = [l for l in st.session_state.get("analysis_log", []) if "⚠️" in l]
        if _err:
            st.caption("\n".join(_err[-3:]))
    elif cards:
        for i in range(0, len(cards), 2):
            pair = cards[i:i+2]
            cols = st.columns(len(pair))
            for col, card in zip(cols, pair):
                sent  = card.get("sentiment", "혼재")
                color = _SENT_COLOR.get(sent, "#9E9E9E")
                emoji = _SENT_EMOJI.get(sent, "😐")
                with col:
                    st.markdown(f"""
<div class="insight-card" style="border-left-color:{color}">
  <div class="card-title">{card.get('title','')}</div>
  <div class="card-badge" style="background:{color}">{emoji} {sent}</div>
  <div class="card-insight">{card.get('insight','')}</div>
  <div class="card-evidence">💬 {card.get('evidence','')}</div>
  <div class="card-action">➡ {card.get('action','')}</div>
</div>""", unsafe_allow_html=True)
    elif insights:
        st.info("인사이트 카드 데이터가 없습니다. 분석을 다시 실행해 보세요.")

    # ── 주요 테마 분석 ────────────────────────────────────────
    if theme_stats:
        st.markdown('<hr class="rpt-divider">', unsafe_allow_html=True)
        total_theme = sum(s["count"] for s in theme_stats) or 1
        max_share   = max(round(s["count"] / total_theme * 100) for s in theme_stats) or 1

        _SENT_C = {"긍정": "#4CAF50", "중립": "#9E9E9E", "부정": "#F44336"}
        _SENT_E = {"긍정": "😊", "중립": "😐", "부정": "😠"}

        def _dominant(p, n, g):
            return max([("긍정", p), ("중립", n), ("부정", g)], key=lambda x: x[1])[0]

        st.markdown(
            f'<div class="sec-title">🏷 주요 테마 분석 '
            f'<span style="font-size:0.78rem;color:#aaa;font-weight:400">'
            f'{len(theme_stats)}개 테마 발굴</span></div>',
            unsafe_allow_html=True
        )

        # ① 순위 개요 바
        rank_rows = ""
        for rank, stat in enumerate(theme_stats, 1):
            share_pct = round(stat["count"] / total_theme * 100)
            bar_w     = round(share_pct / max_share * 100)
            dom       = _dominant(stat["pos"], stat["neu"], stat["neg"])
            rank_rows += f"""
<div style="display:flex;align-items:center;gap:12px;padding:9px 0;
            border-bottom:1px solid #F5F5F5">
  <div style="width:24px;height:24px;border-radius:50%;background:#E8720C;color:white;
              font-size:0.73rem;font-weight:700;display:flex;align-items:center;
              justify-content:center;flex-shrink:0">{rank}</div>
  <div style="min-width:130px;max-width:220px;font-size:0.84rem;font-weight:600;
              color:#222;flex-shrink:0;word-break:keep-all;line-height:1.4">{stat['name']}</div>
  <div style="flex:1;background:#EFEFEF;border-radius:4px;height:9px;overflow:hidden">
    <div style="width:{bar_w}%;height:100%;background:{_SENT_C[dom]};border-radius:4px"></div>
  </div>
  <div style="width:36px;font-size:0.84rem;font-weight:700;color:#333;
              text-align:right;flex-shrink:0">{share_pct}%</div>
  <div style="width:70px;font-size:0.74rem;color:#888;flex-shrink:0;text-align:right">
    {_SENT_E[dom]} {dom} 우세</div>
</div>"""

        st.markdown(f"""
<div class="theme-card">
  <div style="font-size:0.78rem;color:#aaa;font-weight:600;letter-spacing:0.3px;margin-bottom:6px">
    전체 언급 비중 순위 (비중 높은 순)</div>
  {rank_rows}
</div>""", unsafe_allow_html=True)

        # ② 테마별 상세 카드 (1열, 인용 최대)
        st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)
        for rank, stat in enumerate(theme_stats, 1):
            theme_obj = next(
                (t for t in unified_themes if t.get("name") == stat["name"]), {}
            )
            desc      = theme_obj.get("desc", "")
            share_pct = round(stat["count"] / total_theme * 100)
            p, n, g   = stat["pos"], stat["neu"], stat["neg"]
            dom       = _dominant(p, n, g)
            examples  = [ex for ex in stat.get("examples", []) if ex]
            ex_html   = "".join(
                f'<div class="theme-ex">"{ex}"</div>' for ex in examples
            )
            st.markdown(f"""
<div class="theme-card">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">
    <div style="display:flex;align-items:center;gap:10px">
      <div style="width:26px;height:26px;border-radius:50%;background:#E8720C;color:white;
                  font-size:0.78rem;font-weight:700;display:flex;align-items:center;
                  justify-content:center;flex-shrink:0">{rank}</div>
      <span class="theme-name">{stat['name']}</span>
    </div>
    <div style="display:flex;gap:7px;align-items:center">
      <span style="background:{_SENT_C[dom]};color:white;border-radius:10px;
                   padding:2px 10px;font-size:0.70rem;font-weight:700">
        {_SENT_E[dom]} {dom} 우세</span>
      <span class="theme-badge">비중 {share_pct}%</span>
    </div>
  </div>
  {f'<div class="theme-desc">{desc}</div>' if desc else ''}
  <div style="display:flex;height:10px;border-radius:5px;overflow:hidden;margin-bottom:6px">
    <div style="width:{p}%;background:#4CAF50"></div>
    <div style="width:{n}%;background:#BDBDBD"></div>
    <div style="width:{g}%;background:#F44336"></div>
  </div>
  <div class="sent-lbl-row" style="margin-bottom:{('10px' if ex_html else '0')}">
    <span style="color:#2E7D32">😊 긍정 {p}%</span>
    <span style="color:#616161">😐 중립 {n}%</span>
    <span style="color:#C62828">😠 부정 {g}%</span>
  </div>
  {f'<div style="font-size:0.76rem;color:#bbb;font-weight:600;letter-spacing:0.2px;margin-bottom:4px">💬 대표 인용</div>{ex_html}' if ex_html else ''}
</div>""", unsafe_allow_html=True)

    # ── 플랫폼별 감성 (복수 플랫폼일 때만) ───────────────────
    if len(platform_counts) > 1:
        st.markdown('<hr class="rpt-divider">', unsafe_allow_html=True)
        st.markdown('<div class="sec-title">📡 플랫폼별 감성 분포</div>', unsafe_allow_html=True)
        st.plotly_chart(sentiment_by_platform(items_by_plat),
                        use_container_width=True, key="chart_platform")


def _build_cardnews_html(ar: dict, label: str) -> str:
    """분석 결과 → 완결된 리포트 HTML 반환 (인사이트 카드 + 테마 분석 포함)"""
    ins            = ar.get("insights", {})
    unified_themes = ar.get("unified_themes", [])
    items_by_plat  = ar.get("platform_items", {})
    theme_stats    = _aggregate_theme_stats(unified_themes, items_by_plat)

    b64  = _tiger_b64()
    logo = f'<img src="data:image/png;base64,{b64}" style="height:52px">' if b64 else "🐯"

    # 전체 감성 집계
    pos = neu = neg = 0
    for items in items_by_plat.values():
        for item in items:
            s = item.get("sentiment", "중립")
            if s == "긍정":   pos += 1
            elif s == "부정": neg += 1
            else:             neu += 1
    total = pos + neu + neg or 1
    pos_pct = round(pos / total * 100)
    neu_pct = round(neu / total * 100)
    neg_pct = 100 - pos_pct - neu_pct

    # 플랫폼 pills
    plat_pills = "".join(
        f'<span style="background:rgba(255,255,255,0.18);border-radius:16px;'
        f'padding:3px 11px;font-size:0.73rem;color:rgba(255,255,255,0.85)">'
        f'{p} {len(v):,}건</span>'
        for p, v in items_by_plat.items() if v
    )

    # 인사이트 카드 HTML
    cards_html = ""
    for card in ins.get("cards", []):
        sent  = card.get("sentiment", "혼재")
        color = _SENT_COLOR.get(sent, "#9E9E9E")
        emoji = _SENT_EMOJI.get(sent, "😐")
        cards_html += f"""
<div style="background:#fff;border-radius:13px;padding:17px 19px;
            box-shadow:0 2px 10px rgba(0,0,0,0.07);border-left:5px solid {color}">
  <div style="font-size:0.97rem;font-weight:700;margin-bottom:7px;color:#111">{card.get('title','')}</div>
  <div style="display:inline-block;padding:2px 11px;border-radius:20px;
              font-size:0.73rem;font-weight:600;color:#fff;background:{color};
              margin-bottom:10px">{emoji} {sent}</div>
  <div style="font-size:0.87rem;color:#333;line-height:1.65;margin-bottom:8px">{card.get('insight','')}</div>
  <div style="font-size:0.79rem;color:#555;padding:7px 11px;background:#F8F9FA;
              border-radius:7px;border-left:3px solid #ddd;margin-bottom:7px">💬 {card.get('evidence','')}</div>
  <div style="font-size:0.80rem;color:#1565C0;font-weight:600;padding:5px 9px;
              background:#EEF5FF;border-radius:6px">➡ {card.get('action','')}</div>
</div>"""

    # 테마 섹션 HTML (순위 개요 + 상세 카드)
    _SC = {"긍정": "#4CAF50", "중립": "#9E9E9E", "부정": "#F44336"}
    _SE = {"긍정": "😊", "중립": "😐", "부정": "😠"}
    def _dom(p, n, g):
        return max([("긍정", p), ("중립", n), ("부정", g)], key=lambda x: x[1])[0]

    total_theme = sum(s["count"] for s in theme_stats) or 1
    max_share   = max(round(s["count"] / total_theme * 100) for s in theme_stats) or 1

    # 순위 개요 바 (HTML)
    rank_rows_html = ""
    for rank, stat in enumerate(theme_stats, 1):
        share_pct = round(stat["count"] / total_theme * 100)
        bar_w     = round(share_pct / max_share * 100)
        dom       = _dom(stat["pos"], stat["neu"], stat["neg"])
        rank_rows_html += (
            f'<div style="display:flex;align-items:center;gap:10px;padding:8px 0;'
            f'border-bottom:1px solid #F5F5F5">'
            f'<div style="width:22px;height:22px;border-radius:50%;background:#E8720C;'
            f'color:white;font-size:0.70rem;font-weight:700;display:flex;align-items:center;'
            f'justify-content:center;flex-shrink:0">{rank}</div>'
            f'<div style="min-width:130px;max-width:220px;font-size:0.83rem;font-weight:600;'
            f'color:#222;flex-shrink:0;word-break:keep-all;line-height:1.4">'
            f'{stat["name"]}</div>'
            f'<div style="flex:1;background:#EFEFEF;border-radius:4px;height:8px;overflow:hidden">'
            f'<div style="width:{bar_w}%;height:100%;background:{_SC[dom]};border-radius:4px"></div>'
            f'</div>'
            f'<div style="width:34px;font-size:0.82rem;font-weight:700;color:#333;'
            f'text-align:right;flex-shrink:0">{share_pct}%</div>'
            f'<div style="width:72px;font-size:0.73rem;color:#888;flex-shrink:0;text-align:right">'
            f'{_SE[dom]} {dom} 우세</div>'
            f'</div>'
        )

    # 상세 카드 (HTML)
    detail_cards_html = ""
    for rank, stat in enumerate(theme_stats, 1):
        theme_obj = next((t for t in unified_themes if t.get("name") == stat["name"]), {})
        desc      = theme_obj.get("desc", "")
        share_pct = round(stat["count"] / total_theme * 100)
        p, n, g   = stat["pos"], stat["neu"], stat["neg"]
        dom       = _dom(p, n, g)
        examples  = [ex for ex in stat.get("examples", []) if ex]
        ex_items  = "".join(
            f'<div style="font-size:0.78rem;color:#444;background:#FFF8F3;'
            f'border-left:3px solid #E8720C;border-radius:0 5px 5px 0;'
            f'padding:5px 9px;margin-top:5px">"{ex}"</div>'
            for ex in examples
        )
        detail_cards_html += (
            f'<div style="background:#fff;border-radius:13px;padding:18px 20px;'
            f'box-shadow:0 2px 10px rgba(0,0,0,0.07);margin-bottom:12px">'
            f'<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">'
            f'<div style="display:flex;align-items:center;gap:10px">'
            f'<div style="width:26px;height:26px;border-radius:50%;background:#E8720C;color:white;'
            f'font-size:0.76rem;font-weight:700;display:flex;align-items:center;'
            f'justify-content:center;flex-shrink:0">{rank}</div>'
            f'<span style="font-size:0.96rem;font-weight:700;color:#111">{stat["name"]}</span>'
            f'</div>'
            f'<div style="display:flex;gap:7px;align-items:center">'
            f'<span style="background:{_SC[dom]};color:white;border-radius:10px;'
            f'padding:2px 10px;font-size:0.69rem;font-weight:700">{_SE[dom]} {dom} 우세</span>'
            f'<span style="background:#F0F4FF;color:#3F51B5;border-radius:12px;'
            f'padding:2px 9px;font-size:0.69rem;font-weight:700">비중 {share_pct}%</span>'
            f'</div></div>'
            + (f'<div style="font-size:0.80rem;color:#666;margin-bottom:10px;line-height:1.5">{desc}</div>' if desc else '')
            + f'<div style="display:flex;height:9px;border-radius:5px;overflow:hidden;margin-bottom:5px">'
            f'<div style="width:{p}%;background:#4CAF50"></div>'
            f'<div style="width:{n}%;background:#BDBDBD"></div>'
            f'<div style="width:{g}%;background:#F44336"></div>'
            f'</div>'
            f'<div style="display:flex;gap:14px;font-size:0.72rem;margin-bottom:{("10px" if ex_items else "0")}">'
            f'<span style="color:#2E7D32">😊 긍정 {p}%</span>'
            f'<span style="color:#616161">😐 중립 {n}%</span>'
            f'<span style="color:#C62828">😠 부정 {g}%</span>'
            f'</div>'
            + (f'<div style="font-size:0.74rem;color:#bbb;font-weight:600;margin-bottom:4px">💬 대표 인용</div>{ex_items}' if ex_items else '')
            + '</div>'
        )

    summary_html = (
        f'<div style="background:linear-gradient(135deg,#E8F4FD 0%,#EDE7F6 100%);'
        f'border-radius:13px;padding:17px 21px;margin-bottom:24px;'
        f'font-size:0.92rem;color:#1A237E;line-height:1.75;'
        f'border-left:5px solid #3F51B5">'
        f'📋 <b>종합 동향 요약</b><br><br>{ins["summary"]}</div>'
        if ins.get("summary") else ""
    )

    theme_section = (
        f'<div style="margin-bottom:28px">'
        f'<div style="font-size:1rem;font-weight:700;color:#222;margin-bottom:14px;'
        f'padding-bottom:8px;border-bottom:2px solid #F0F0F0">'
        f'🏷 주요 테마 분석 <span style="font-size:0.78rem;color:#aaa;font-weight:400">'
        f'{len(theme_stats)}개 테마 발굴</span></div>'
        f'<div style="background:#fff;border-radius:13px;padding:16px 18px;'
        f'box-shadow:0 2px 10px rgba(0,0,0,0.07);margin-bottom:14px">'
        f'<div style="font-size:0.76rem;color:#aaa;font-weight:600;letter-spacing:0.3px;margin-bottom:6px">'
        f'전체 언급 비중 순위</div>'
        f'{rank_rows_html}</div>'
        f'{detail_cards_html}'
        f'</div>'
    ) if theme_stats else ""

    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="UTF-8">
<title>{label} 게임 동향 분석 리포트</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: 'Malgun Gothic', 'Apple SD Gothic Neo', sans-serif;
        background: #F4F6FA; color: #222; padding: 32px; max-width: 960px; margin: 0 auto; }}
@media print {{ body {{ padding: 16px; background: white; }} }}
</style></head><body>

<div style="background:linear-gradient(135deg,#0F2027 0%,#203A43 60%,#2C5364 100%);
            border-radius:16px;padding:22px 26px;margin-bottom:22px;color:white">
  <div style="display:flex;align-items:center;gap:14px;margin-bottom:12px">
    {logo}
    <div>
      <div style="font-size:1.35rem;font-weight:800;letter-spacing:0.3px">🐯 {label} 게임 동향 분석 리포트</div>
      <div style="font-size:0.77rem;color:rgba(255,255,255,0.55);margin-top:4px">{datetime.now().strftime('%Y년 %m월 %d일')} 기준 · Claude AI 분석</div>
    </div>
  </div>
  <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
    {plat_pills}
    <span style="background:#E8720C;border-radius:16px;padding:3px 13px;
                 font-size:0.73rem;font-weight:700;color:white">총 {total:,}건</span>
  </div>
</div>

<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px">
  <div style="background:white;border-radius:13px;padding:18px;box-shadow:0 2px 10px rgba(0,0,0,0.07);
              border-top:4px solid #4CAF50;text-align:center">
    <div style="font-size:2rem;font-weight:800;color:#2E7D32">{pos_pct}%</div>
    <div style="font-size:0.79rem;color:#888;margin-top:3px">😊 긍정</div>
    <div style="font-size:0.76rem;color:#bbb;margin-top:3px">{pos:,}건</div>
  </div>
  <div style="background:white;border-radius:13px;padding:18px;box-shadow:0 2px 10px rgba(0,0,0,0.07);
              border-top:4px solid #9E9E9E;text-align:center">
    <div style="font-size:2rem;font-weight:800;color:#616161">{neu_pct}%</div>
    <div style="font-size:0.79rem;color:#888;margin-top:3px">😐 중립</div>
    <div style="font-size:0.76rem;color:#bbb;margin-top:3px">{neu:,}건</div>
  </div>
  <div style="background:white;border-radius:13px;padding:18px;box-shadow:0 2px 10px rgba(0,0,0,0.07);
              border-top:4px solid #F44336;text-align:center">
    <div style="font-size:2rem;font-weight:800;color:#C62828">{neg_pct}%</div>
    <div style="font-size:0.79rem;color:#888;margin-top:3px">😠 부정</div>
    <div style="font-size:0.76rem;color:#bbb;margin-top:3px">{neg:,}건</div>
  </div>
</div>
<div style="display:flex;height:10px;border-radius:6px;overflow:hidden;margin-bottom:6px">
  <div style="width:{pos_pct}%;background:#4CAF50"></div>
  <div style="width:{neu_pct}%;background:#BDBDBD"></div>
  <div style="width:{neg_pct}%;background:#F44336"></div>
</div>
<div style="display:flex;gap:16px;font-size:0.72rem;color:#888;margin-bottom:22px">
  <span style="color:#2E7D32">😊 긍정 {pos_pct}%</span>
  <span style="color:#616161">😐 중립 {neu_pct}%</span>
  <span style="color:#C62828">😠 부정 {neg_pct}%</span>
</div>

{summary_html}

<div style="margin-bottom:28px">
  <div style="font-size:1rem;font-weight:700;color:#222;margin-bottom:14px;
              padding-bottom:8px;border-bottom:2px solid #F0F0F0">💡 핵심 인사이트</div>
  <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:14px">
    {cards_html}
  </div>
</div>

{theme_section}

<div style="text-align:center;font-size:0.73rem;color:#bbb;margin-top:8px;padding-top:16px;
            border-top:1px solid #eee">동향 수집하는 호랑이 · 내부용 자료</div>
</body></html>"""


# ─── GCP Service Account 인증 (Streamlit Cloud 배포용) ───────
# 로컬: gcloud ADC 자동 사용 / Cloud: secrets에서 SA JSON 읽어 환경변수 설정
def _setup_gcp_auth():
    sa = st.secrets.get("gcp", {}).get("service_account_json", None)
    if not sa:
        return  # 로컬 ADC 사용
    try:
        sa_info = json.loads(sa) if isinstance(sa, str) else dict(sa)
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(sa_info, tmp)
        tmp.flush()
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = tmp.name
    except Exception:
        pass

_setup_gcp_auth()

# ─── 초기화 ──────────────────────────────────────────────────
_tiger_path = Path(__file__).parent / "assets" / "tiger.png"
_page_icon  = Image.open(_tiger_path) if _tiger_path.exists() else "🐯"

st.set_page_config(
    page_title="동향 수집하는 호랑이",
    page_icon=_page_icon,
    layout="wide",
    initial_sidebar_state="collapsed"
)
st.markdown(_GLOBAL_CSS, unsafe_allow_html=True)

init_db()

if "collection_log"    not in st.session_state:
    st.session_state.collection_log    = []
if "analysis_log"      not in st.session_state:
    st.session_state.analysis_log      = []
if "collection_result" not in st.session_state:
    st.session_state.collection_result = None
if "analysis_result"   not in st.session_state:
    st.session_state.analysis_result   = None
if "cardnews_html"           not in st.session_state:
    st.session_state.cardnews_html           = None
if "quick_summary_result"   not in st.session_state:
    st.session_state.quick_summary_result   = None
if "is_quick_summarizing"   not in st.session_state:
    st.session_state.is_quick_summarizing   = False
if "live_chat_data"         not in st.session_state:
    st.session_state.live_chat_data         = None
if "live_analysis_result"   not in st.session_state:
    st.session_state.live_analysis_result   = None
if "live_cardnews_html"     not in st.session_state:
    st.session_state.live_cardnews_html     = None
if "live_collection_log"    not in st.session_state:
    st.session_state.live_collection_log    = []
if "live_analysis_log"      not in st.session_state:
    st.session_state.live_analysis_log      = []
if "is_live_collecting"     not in st.session_state:
    st.session_state.is_live_collecting     = False
if "is_live_analyzing"      not in st.session_state:
    st.session_state.is_live_analyzing      = False
if "is_collecting"     not in st.session_state:
    st.session_state.is_collecting     = False
if "is_analyzing"      not in st.session_state:
    st.session_state.is_analyzing      = False
if "yt_urls"           not in st.session_state:
    st.session_state.yt_urls           = [""]

# ─── 메인 레이아웃 ────────────────────────────────────────────
_header_b64 = _tiger_b64()
_header_img = (
    f'<img src="data:image/png;base64,{_header_b64}" '
    f'style="height:140px;image-rendering:pixelated;flex-shrink:0">'
    if _header_b64 else '<span style="font-size:5rem">🐯</span>'
)
st.markdown(f"""
<div style="display:flex;align-items:center;gap:28px;
            background:linear-gradient(135deg,#FFF8F3 0%,#FFFFFF 100%);
            border-radius:20px;padding:24px 32px;
            border:1px solid #FFE0C8;margin-bottom:8px">
  {_header_img}
  <div style="flex:1">
    <div style="font-size:2.4rem;font-weight:900;color:#E8720C;line-height:1.15;
                letter-spacing:-0.5px">동향 수집하는 호랑이</div>
    <div style="font-size:1rem;color:#999;margin-top:8px">
      게임 커뮤니티 반응을 플랫폼별로 수집하고 Claude AI로 분석합니다
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

st.markdown("---")

# ── 좌: 플랫폼 설정 / 우: 분석 옵션·버튼 ──────────────────────
left_col, right_col = st.columns([3, 1], gap="large")

with left_col:
    st.markdown("#### 📡 수집 플랫폼")

    # ── YouTube 댓글 ──────────────────────────────────────────
    with st.container(border=True):
        tog_col, title_col = st.columns([1, 8])
        with tog_col:
            use_youtube = st.toggle("", value=True, key="use_yt")
        with title_col:
            if use_youtube:
                st.markdown("**📺 YouTube 댓글**")
            else:
                st.markdown('<span style="color:#bbb">📺 YouTube 댓글</span>', unsafe_allow_html=True)
        if use_youtube:
            yt_api_key = _YT_API_KEY
            if not yt_api_key:
                st.warning("secrets.toml에 YouTube API 키를 설정해주세요.")
            st.caption("수집할 영상 URL을 입력하세요")
            to_delete = None
            for i, url in enumerate(st.session_state.yt_urls):
                c_url, c_del = st.columns([8, 1])
                with c_url:
                    st.session_state.yt_urls[i] = st.text_input(
                        f"영상 URL {i+1}", value=url,
                        placeholder="https://youtube.com/watch?v=...",
                        key=f"yt_url_{i}", label_visibility="collapsed"
                    )
                with c_del:
                    if st.button("✕", key=f"del_{i}",
                                 disabled=len(st.session_state.yt_urls) == 1):
                        to_delete = i
            if to_delete is not None:
                st.session_state.yt_urls.pop(to_delete)
                st.rerun()
            if st.button("＋ 영상 URL 추가"):
                st.session_state.yt_urls.append("")
                st.rerun()
        else:
            yt_api_key = ""

    # YouTube 라이브채팅 → 🎬 라이브 분석 탭으로 이동
    use_live = False
    live_url = ""

    # ── 디시인사이드 ──────────────────────────────────────────
    with st.container(border=True):
        tog_col, title_col = st.columns([1, 8])
        with tog_col:
            use_dc = st.toggle("", value=True, key="use_dc")
        with title_col:
            if use_dc:
                st.markdown("**💬 디시인사이드**")
            else:
                st.markdown('<span style="color:#bbb">💬 디시인사이드</span>', unsafe_allow_html=True)
        if use_dc:
            dc1, dc2, dc3 = st.columns([3, 1, 1])
            with dc1:
                dc_gallery_id = st.text_input("갤러리 ID", placeholder="예: lostark", key="dc_gid")
            with dc2:
                dc_is_minor = st.checkbox("마이너", value=True)
            with dc3:
                dc_max_pages = st.number_input("페이지 수", min_value=1, max_value=100, value=10, key="dc_pages")
        else:
            dc_gallery_id = ""
            dc_is_minor   = True
            dc_max_pages  = 10

    # ── 앱스토어 ──────────────────────────────────────────────
    with st.container(border=True):
        tog_col, title_col = st.columns([1, 8])
        with tog_col:
            use_appstore = st.toggle("", value=True, key="use_app")
        with title_col:
            if use_appstore:
                st.markdown("**🍎 앱스토어**")
            else:
                st.markdown('<span style="color:#bbb">🍎 앱스토어</span>', unsafe_allow_html=True)
        if use_appstore:
            appstore_id = st.text_input("앱 ID (숫자)", placeholder="예: 641397086", key="app_id")
        else:
            appstore_id = ""

    # ── 플레이스토어 ──────────────────────────────────────────
    with st.container(border=True):
        tog_col, title_col = st.columns([1, 8])
        with tog_col:
            use_playstore = st.toggle("", value=True, key="use_play")
        with title_col:
            if use_playstore:
                st.markdown("**🤖 플레이스토어**")
            else:
                st.markdown('<span style="color:#bbb">🤖 플레이스토어</span>', unsafe_allow_html=True)
        if use_playstore:
            playstore_id = st.text_input(
                "패키지명", placeholder="예: com.smilegate.LOSTARK", key="play_id"
            )
        else:
            playstore_id = ""

with right_col:
    gcp_project_id = _GCP_PROJECT
    gcp_region     = _GCP_REGION
    _any_platform  = use_youtube or use_dc or use_appstore or use_playstore or use_live

    # ── STEP 1: 데이터 수집 ───────────────────────────────────
    _s1 = "done" if st.session_state.collection_result else "ready"
    st.markdown(_step_header(1, "데이터 수집", _s1), unsafe_allow_html=True)
    collect_btn = st.button(
        "🚀 수집 시작",
        type="primary",
        use_container_width=True,
        disabled=not _any_platform or st.session_state.is_collecting
    )

    st.markdown("---")

    # ── STEP 2: AI 분석 ───────────────────────────────────────
    _s2_done = bool(st.session_state.analysis_result or st.session_state.quick_summary_result)
    _s2 = ("done" if _s2_done
           else "ready" if st.session_state.collection_result
           else "pending")
    st.markdown(_step_header(2, "AI 분석", _s2), unsafe_allow_html=True)

    _no_data   = not st.session_state.collection_result
    _busy      = st.session_state.is_analyzing or st.session_state.is_quick_summarizing

    # ── ⚡ 빠른 요약 ─────────────────────────────────────────
    quick_btn = st.button(
        "⚡ 빠른 요약",
        use_container_width=True,
        disabled=_no_data or _busy,
        key="quick_btn",
    )
    st.markdown("""
<div style="background:#EFF6FF;border-radius:8px;padding:10px 13px;
            font-size:0.76rem;color:#1E40AF;line-height:1.75;margin-bottom:10px">
  <b>⚡ 빠른 요약 — 이럴 때 사용하세요</b><br>
  · 수집 직후 큰 흐름을 30초~1분 안에 파악<br>
  · 정밀 분석 전 가설 세우기 / 방향 확인<br>
  <b>참고 시 주의사항</b><br>
  · 전체 중 <b>샘플 200건</b> 기반 — 소량 수집 시 편향 가능<br>
  · 감성·토픽은 <b>Claude 정성 추정</b> (% 수치 직접 인용 금지)<br>
  · 테마 매핑 없음 — 항목별 분류 데이터 생성 안 됨
</div>""", unsafe_allow_html=True)

    st.markdown('<div style="border-top:1px solid #F0F0F0;margin:4px 0 10px"></div>',
                unsafe_allow_html=True)

    # ── 🔍 정밀 분석 ─────────────────────────────────────────
    sentiment_mode = st.radio(
        "감성분석 방식",
        ["배치 (빠름, 추천)", "건별 (정밀, 느림)"],
        help="건별은 정확도가 높지만 시간이 더 걸립니다",
        label_visibility="collapsed",
        disabled=_s2 == "pending",
    )
    analyze_btn = st.button(
        "🔍 정밀 분석",
        use_container_width=True,
        disabled=_no_data or _busy,
        key="analyze_btn",
    )
    st.markdown("""
<div style="background:#FFF8F0;border-radius:8px;padding:10px 13px;
            font-size:0.76rem;color:#92400E;line-height:1.75;margin-bottom:4px">
  <b>🔍 정밀 분석 — 이럴 때 사용하세요</b><br>
  · 보고서·공유용 리포트가 필요할 때<br>
  · 테마별 감성 비중을 정량으로 제시해야 할 때<br>
  <b>참고 시 주의사항</b><br>
  · <b>전체 항목 감성 태깅</b> → 수치는 실제 집계 (인용 가능)<br>
  · 수집량 30건 미만 시 테마 다양성 제한될 수 있음<br>
  · 분석 완료 시 <b>카드뉴스 HTML 자동 생성</b>
</div>""", unsafe_allow_html=True)
    cardnews_btn = False

# ── 현재 수집 대상 요약 ────────────────────────────────────────
yt_valid = [u for u in st.session_state.yt_urls if u.strip()]
_active = []
if use_youtube and yt_valid:       _active.append(f"📺 YouTube {len(yt_valid)}개")
if use_live    and live_url:       _active.append("🎬 라이브채팅")
if use_dc      and dc_gallery_id:  _active.append(f"💬 디시 ({dc_gallery_id})")
if use_appstore and appstore_id:   _active.append("🍎 앱스토어")
if use_playstore and playstore_id: _active.append("🤖 플레이스토어")

if _active:
    st.caption("수집 대상: " + "  |  ".join(_active))

st.markdown("---")

# 파일명용 레이블 (입력된 식별자 중 첫 번째)
_label = dc_gallery_id or appstore_id or playstore_id or "수집"

# config 항상 최신값으로 구성
config = {
    "use_youtube":   use_youtube,
    "use_live":      use_live,
    "use_dc":        use_dc,
    "use_appstore":  use_appstore,
    "use_playstore": use_playstore,
    "yt_api_key":    yt_api_key,
    "yt_urls":       [u for u in st.session_state.yt_urls if u.strip()],
    "live_url":      live_url,
    "dc_gallery_id": dc_gallery_id,
    "dc_is_minor":   dc_is_minor,
    "dc_max_pages":  dc_max_pages,
    "appstore_id":   appstore_id,
    "playstore_id":  playstore_id,
    "gcp_project_id": gcp_project_id,
    "gcp_region":    gcp_region,
    "sentiment_mode": sentiment_mode,
}
st.session_state.last_config = config

main_tiger_placeholder   = st.empty()  # 로딩 중 호랑이 애니메이션
main_progress_placeholder = st.empty()  # 진행률 바
main_status_placeholder   = st.empty()  # 진행률 텍스트 (바 바로 아래)

tab_collect, tab_dashboard, tab_data, tab_download, tab_live = st.tabs(
    ["📡 수집 현황", "📊 분석 결과", "🗂 Raw 데이터", "📥 다운로드", "🎬 라이브 분석"]
)

# ─── 탭1: 수집 현황 ──────────────────────────────────────────
with tab_collect:
    _no_input = not yt_valid and not dc_gallery_id and not appstore_id and not playstore_id and not live_url
    if _no_input:
        st.info("플랫폼 설정을 입력하고 수집을 시작하세요.")
    else:
        result = st.session_state.collection_result or {}
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("YouTube",      result.get("yt_count",   "—"))
        c2.metric("디시인사이드", result.get("dc_count",   "—"))
        c3.metric("앱스토어",     result.get("app_count",  "—"))
        c4.metric("플레이스토어", result.get("play_count", "—"))

        if result.get("dc_blocked"):
            st.warning("⚠️ 디시인사이드 IP 차단이 감지되어 수집이 제한됐습니다.")

        log_text = "\n".join(st.session_state.collection_log) or "수집 대기 중..."
        st.text_area("수집 로그", value=log_text, height=300, disabled=True,
                     label_visibility="collapsed")

# ─── 탭2: 분석 결과 ──────────────────────────────────────────
with tab_dashboard:
    if not st.session_state.collection_result:
        st.info("수집 완료 후 분석 결과가 표시됩니다.")
    else:
        qs = st.session_state.quick_summary_result
        ar = st.session_state.analysis_result

        if not qs and not ar:
            st.info("STEP 2에서 **⚡ 빠른 요약** 또는 **🔍 정밀 분석**을 실행하세요.")
        else:
            # 빠른 요약 결과 (항상 상단)
            if qs:
                _render_quick_summary(qs)

            # 정밀 분석 결과 (아래)
            if ar:
                if qs:
                    st.markdown(
                        '<hr style="border:none;border-top:3px solid #E8720C;margin:28px 0">',
                        unsafe_allow_html=True
                    )
                _render_dashboard(ar)

# ─── 탭3: Raw 데이터 ─────────────────────────────────────────
with tab_data:
    if not st.session_state.collection_result:
        st.info("수집 완료 후 데이터를 확인할 수 있습니다.")
    else:
        result = st.session_state.collection_result
        platform_sel = st.selectbox(
            "플랫폼",
            ["YouTube 영상", "YouTube 댓글", "YouTube 라이브채팅",
             "디시인사이드", "앱스토어", "플레이스토어"]
        )

        data_map = {
            "YouTube 영상":     (result.get("yt_videos", []),
                                 ["순위", "영상 제목", "채널명", "조회수", "좋아요", "댓글수", "업로드일", "링크"]),
            "YouTube 댓글":     (result.get("yt_comments", []),
                                 ["영상 제목", "작성자", "댓글", "좋아요", "작성일", "유형"]),
            "YouTube 라이브채팅": (result.get("yt_live", []),
                                 ["시간", "작성자", "메시지", "플랫폼"]),
            "디시인사이드":     (result.get("dc_posts", []),
                                 ["제목", "본문", "작성일", "출처", "링크"]),
            "앱스토어":         (result.get("appstore", []),
                                 ["제목", "작성자", "평점", "내용", "작성일", "버전"]),
            "플레이스토어":     (result.get("playstore", []),
                                 ["작성자", "평점", "내용", "작성일", "좋아요"]),
        }

        items, cols = data_map[platform_sel]
        if items:
            df = pd.DataFrame(items)
            show_cols = [c for c in cols if c in df.columns]
            st.dataframe(df[show_cols], use_container_width=True, height=500)
            st.caption(f"총 {len(items)}건")
        else:
            st.info("해당 플랫폼 수집 데이터가 없습니다.")

# ─── 탭4: 다운로드 ───────────────────────────────────────────
with tab_download:
    if not st.session_state.collection_result:
        st.info("수집 완료 후 다운로드할 수 있습니다.")
    else:
        ar = st.session_state.analysis_result

        # ── Excel ────────────────────────────────────────────
        st.markdown("#### 📊 Excel 데이터")
        if not ar:
            st.caption("분석 전: Raw 데이터만 포함됩니다. 분석 후엔 감성·테마 컬럼이 추가됩니다.")
        with st.spinner("Excel 생성 중..."):
            excel_bytes = generate_excel(
                keyword=_label,
                collection_result=st.session_state.collection_result,
                analysis_result=ar,
            )
        file_name = f"{_label}_동향분석_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        st.download_button(
            label="📥 Excel 다운로드",
            data=excel_bytes,
            file_name=file_name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        sheets = ["📊 동향 요약", "YouTube_영상", "YouTube_댓글",
                  "디시인사이드", "앱스토어_리뷰", "플레이스토어_리뷰"]
        st.caption("포함 시트: " + " / ".join(sheets))

        # ── 카드뉴스 HTML ─────────────────────────────────────
        st.markdown("---")
        st.markdown("#### 📰 카드뉴스 HTML")
        if not st.session_state.analysis_result:
            st.info("STEP 2 분석 완료 후 자동으로 카드뉴스 HTML이 생성됩니다.")
        elif st.session_state.cardnews_html:
            cn_file = f"{_label}_카드뉴스_{datetime.now().strftime('%Y%m%d_%H%M')}.html"
            st.download_button(
                label="📥 카드뉴스 HTML 다운로드",
                data=st.session_state.cardnews_html.encode("utf-8"),
                file_name=cn_file,
                mime="text/html",
            )
            st.caption("브라우저에서 열고 Ctrl+P → PDF로 저장")
        else:
            st.info("분석 결과가 있지만 카드뉴스 생성에 실패했습니다. 분석을 다시 실행해 보세요.")

# ─── 빠른 요약 실행 ──────────────────────────────────────────
if quick_btn and st.session_state.collection_result:
    st.session_state.is_quick_summarizing = True
    st.session_state.quick_summary_result = None
    _qs_log = []

    main_tiger_placeholder.markdown(
        _tiger_html("호랑이가 빠르게 훑는 중"), unsafe_allow_html=True
    )
    _qs_prog = main_progress_placeholder.progress(0)
    _qs_prog.progress(30)
    main_status_placeholder.caption("⚡ 빠른 요약 생성 중...")

    qs_result = run_quick_summary(
        project_id=_GCP_PROJECT,
        region=_GCP_REGION,
        collection_result=st.session_state.collection_result,
        log_fn=lambda m: _qs_log.append(m),
    )

    main_tiger_placeholder.empty()
    main_progress_placeholder.empty()
    main_status_placeholder.empty()

    st.session_state.quick_summary_result = qs_result
    st.session_state.is_quick_summarizing = False
    st.rerun()


# ─── 분석 실행 ───────────────────────────────────────────────
if analyze_btn:
    st.session_state.is_analyzing = True
    st.session_state.analysis_log = []
    st.session_state.analysis_result = None
    st.session_state.cardnews_html = ""

    def alog_fn(msg: str):
        st.session_state.analysis_log.append(msg)

    main_tiger_placeholder.markdown(
        _tiger_html("호랑이가 열심히 분석 중"), unsafe_allow_html=True
    )
    _prog_bar  = main_progress_placeholder.progress(0)
    _prog_text = main_status_placeholder

    def _progress_fn(pct: int, msg: str):
        _prog_bar.progress(pct)
        _prog_text.caption(f"🔍 {msg}  ({pct}%)")

    ar = run_full_analysis(
        project_id=gcp_project_id,
        region=gcp_region,
        keyword=_label,
        collection_result=st.session_state.collection_result,
        config=st.session_state.get("last_config", {}),
        log_fn=alog_fn,
        progress_fn=_progress_fn,
    )
    main_tiger_placeholder.empty()
    main_progress_placeholder.empty()
    main_status_placeholder.empty()

    st.session_state.analysis_result = ar
    # 분석 완료 즉시 카드뉴스 자동 생성
    st.session_state.cardnews_html = _build_cardnews_html(ar, _label)
    st.session_state.is_analyzing = False
    st.rerun()

# ─── 수집 실행 ───────────────────────────────────────────────
if collect_btn:
    st.session_state.is_collecting = True
    st.session_state.collection_log = []
    st.session_state.collection_result = None

    def log_fn(msg: str):
        st.session_state.collection_log.append(msg)

    main_tiger_placeholder.markdown(
        _tiger_html("호랑이가 열심히 수집 중"), unsafe_allow_html=True
    )
    result = run_collection(_label, st.session_state.last_config, log_fn)
    main_tiger_placeholder.empty()

    st.session_state.collection_result = result
    st.session_state.analysis_result   = None
    st.session_state.is_collecting = False
    st.rerun()


# ─── 탭5: 라이브 분석 ────────────────────────────────────────
with tab_live:
    st.markdown("#### 🎬 YouTube 라이브 방송 분석")
    st.caption("라이브/아카이브 영상의 채팅 전체를 수집하고 방송 동향을 분석합니다")

    live_url_input = st.text_input(
        "라이브 영상 URL",
        placeholder="https://youtube.com/watch?v=... (라이브 또는 아카이브)",
        key="live_url_tab"
    )

    lv_c1, lv_c2, lv_c3 = st.columns(3)

    _lv_s1 = "done" if st.session_state.live_chat_data else "ready"
    _lv_s2 = ("done" if st.session_state.live_analysis_result
               else "ready" if st.session_state.live_chat_data else "pending")
    _lv_s3 = ("done" if st.session_state.live_cardnews_html
               else "ready" if st.session_state.live_analysis_result else "pending")

    with lv_c1:
        st.markdown(_step_header(1, "채팅 수집", _lv_s1), unsafe_allow_html=True)
        live_collect_btn = st.button(
            "🚀 채팅 수집",
            key="live_collect_btn",
            use_container_width=True,
            type="primary",
            disabled=not live_url_input.strip() or st.session_state.is_live_collecting,
        )
    with lv_c2:
        st.markdown(_step_header(2, "AI 분석", _lv_s2), unsafe_allow_html=True)
        live_analyze_btn = st.button(
            "🔍 분석 시작",
            key="live_analyze_btn",
            use_container_width=True,
            disabled=not st.session_state.live_chat_data or st.session_state.is_live_analyzing,
        )
    with lv_c3:
        st.markdown(_step_header(3, "카드뉴스", _lv_s3), unsafe_allow_html=True)
        live_cardnews_btn = st.button(
            "📰 HTML 생성",
            key="live_cardnews_btn",
            use_container_width=True,
            disabled=not st.session_state.live_analysis_result,
        )

    # ── 수집 로그 ─────────────────────────────────────────────
    if st.session_state.live_collection_log:
        with st.expander("수집 로그", expanded=False):
            st.text("\n".join(st.session_state.live_collection_log))

    # ── 분석 결과 표시 ────────────────────────────────────────
    lv_ar = st.session_state.live_analysis_result
    if lv_ar:
        analysis  = lv_ar.get("analysis", {})
        buckets   = lv_ar.get("buckets", [])
        highlights = lv_ar.get("highlights", [])
        tagged    = lv_ar.get("tagged_chats", [])

        st.divider()

        # 전체 요약
        if analysis.get("summary"):
            st.markdown(
                f'<div class="summary-box">📋 <b>방송 요약</b><br>{analysis["summary"]}</div>',
                unsafe_allow_html=True
            )
        if analysis.get("flow"):
            st.info(f"📈 **흐름:** {analysis['flow']}")

        # 토픽 태그
        topics = analysis.get("topics", [])
        if topics:
            st.markdown("**🏷 주요 토픽**  " + "  ".join(
                f"`{t}`" for t in topics
            ))

        st.divider()

        # 감성 차트
        st.subheader("😊 감성 분석")
        lv_ch1, lv_ch2 = st.columns([1, 2])
        with lv_ch1:
            st.plotly_chart(live_sentiment_donut(tagged),
                            use_container_width=True, key="lv_donut")
        with lv_ch2:
            st.plotly_chart(live_sentiment_timeline(buckets),
                            use_container_width=True, key="lv_timeline")

        st.divider()

        # 하이라이트 구간
        if highlights:
            st.subheader("🔥 반응 하이라이트 구간")
            for i, h in enumerate(highlights):
                sent_color = {"긍정": "#4CAF50", "혼재": "#FF9800", "부정": "#F44336"}
                with st.expander(
                    f"**구간 {i+1}** [{h['label']}]  — "
                    f"채팅 {h['volume']}건 / 긍정 {h['pos_pct']}%"
                ):
                    if h.get("sample"):
                        for s in h["sample"]:
                            st.markdown(f"- {s}")

        st.divider()

        # 인사이트 카드 (메인 대시보드와 동일 형태)
        st.subheader("💡 인사이트 카드")
        st.markdown(_CARD_CSS, unsafe_allow_html=True)
        cards = analysis.get("cards", [])
        if cards:
            lv_cols = st.columns(min(len(cards), 2))
            for i, card in enumerate(cards):
                sent  = card.get("sentiment", "혼재")
                color = _SENT_COLOR.get(sent, "#9E9E9E")
                emoji = _SENT_EMOJI.get(sent, "😐")
                with lv_cols[i % 2]:
                    st.markdown(f"""
<div class="insight-card" style="border-left-color:{color}">
  <div class="card-title">{card.get('title','')}</div>
  <div class="card-badge" style="background:{color}">{emoji} {sent}</div>
  <div class="card-insight">{card.get('insight','')}</div>
  <div class="card-evidence">💬 {card.get('evidence','')}</div>
  <div class="card-action">➡ {card.get('action','')}</div>
</div>""", unsafe_allow_html=True)

        # 분석 로그
        if st.session_state.live_analysis_log:
            with st.expander("분석 로그 보기"):
                st.text("\n".join(st.session_state.live_analysis_log))

        # 카드뉴스 다운로드
        if st.session_state.live_cardnews_html:
            st.divider()
            lv_file = f"라이브분석_{datetime.now().strftime('%Y%m%d_%H%M')}.html"
            st.download_button(
                label="📥 카드뉴스 HTML 다운로드",
                data=st.session_state.live_cardnews_html.encode("utf-8"),
                file_name=lv_file,
                mime="text/html",
                key="live_dl_btn"
            )
            st.caption("브라우저에서 열고 Ctrl+P → PDF로 저장")

    elif st.session_state.live_chat_data:
        st.info(f"채팅 {len(st.session_state.live_chat_data)}건 수집 완료. STEP 2 분석을 시작하세요.")
    else:
        st.info("라이브 영상 URL을 입력하고 채팅을 수집하세요.")


# ─── 라이브 채팅 수집 실행 ───────────────────────────────────
if live_collect_btn and live_url_input.strip():
    from crawlers.collector import extract_video_id
    try:
        from keyword_crawler import YouTubeLiveCrawler
        _LIVE_OK = True
    except ImportError:
        _LIVE_OK = False

    st.session_state.is_live_collecting  = True
    st.session_state.live_collection_log = []
    st.session_state.live_chat_data      = None
    st.session_state.live_analysis_result = None
    st.session_state.live_cardnews_html  = None

    def _live_log(msg):
        st.session_state.live_collection_log.append(msg)

    if not _LIVE_OK:
        _live_log("❌ keyword_crawler 로드 실패")
    else:
        _vid = extract_video_id(live_url_input.strip())
        if not _vid:
            _live_log("❌ URL에서 video_id를 추출할 수 없습니다")
        else:
            _live_log(f"🎬 라이브 채팅 수집 시작... (video_id: {_vid})")
            try:
                _chats = YouTubeLiveCrawler().collect(_vid, callback=_live_log)
                st.session_state.live_chat_data = _chats
                _live_log(f"✅ 채팅 {len(_chats)}건 수집 완료")
            except Exception as e:
                _live_log(f"❌ 수집 오류: {e}")

    st.session_state.is_live_collecting = False
    st.rerun()


# ─── 라이브 AI 분석 실행 ─────────────────────────────────────
if live_analyze_btn and st.session_state.live_chat_data:
    st.session_state.is_live_analyzing  = True
    st.session_state.live_analysis_log  = []
    st.session_state.live_analysis_result = None

    def _lv_log(msg):
        st.session_state.live_analysis_log.append(msg)

    _lv_prog_bar  = st.progress(0)
    _lv_prog_text = st.empty()

    def _lv_progress(pct, msg):
        _lv_prog_bar.progress(pct)
        _lv_prog_text.caption(f"🔍 {msg}  ({pct}%)")

    _lv_result = run_live_analysis(
        project_id=_GCP_PROJECT,
        region=_GCP_REGION,
        chats=st.session_state.live_chat_data,
        log_fn=_lv_log,
        progress_fn=_lv_progress,
    )
    _lv_prog_bar.empty()
    _lv_prog_text.empty()

    st.session_state.live_analysis_result = _lv_result
    st.session_state.is_live_analyzing    = False
    st.rerun()


# ─── 라이브 카드뉴스 생성 ────────────────────────────────────
if live_cardnews_btn and st.session_state.live_analysis_result:
    lv_ar      = st.session_state.live_analysis_result
    lv_analysis = lv_ar.get("analysis", {})
    lv_tagged  = lv_ar.get("tagged_chats", [])
    lv_hi      = lv_ar.get("highlights", [])
    b64        = _tiger_b64()
    logo       = f'<img src="data:image/png;base64,{b64}" style="height:56px">' if b64 else "🐯"

    # 하이라이트 HTML
    hi_html = ""
    for i, h in enumerate(lv_hi):
        samples = "".join(f"<li>{s}</li>" for s in h.get("sample", [])[:3])
        hi_html += f"""
<div class="hi-card">
  <div class="hi-label">🔥 구간 {i+1} [{h['label']}]
    — 채팅 {h['volume']}건 / 긍정 {h['pos_pct']}%</div>
  <ul class="hi-samples">{samples}</ul>
</div>"""

    # 인사이트 카드 HTML
    cards_html = ""
    for card in lv_analysis.get("cards", []):
        sent  = card.get("sentiment", "혼재")
        color = _SENT_COLOR.get(sent, "#9E9E9E")
        emoji = _SENT_EMOJI.get(sent, "😐")
        cards_html += f"""
<div class="cn-card" style="border-left:5px solid {color}">
  <div class="cn-title">{card.get('title','')}</div>
  <div class="cn-badge" style="background:{color}">{emoji} {sent}</div>
  <div class="cn-insight">{card.get('insight','')}</div>
  <div class="cn-evidence">💬 {card.get('evidence','')}</div>
  <div class="cn-action">➡ {card.get('action','')}</div>
</div>"""

    topics_html = "  ".join(
        f'<span class="topic-tag">{t}</span>'
        for t in lv_analysis.get("topics", [])
    )
    total = len(lv_tagged)
    pos_p = round(sum(1 for c in lv_tagged if c.get("sentiment")=="긍정") / max(total,1) * 100)
    neg_p = round(sum(1 for c in lv_tagged if c.get("sentiment")=="부정") / max(total,1) * 100)

    lv_html = f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="UTF-8">
<title>라이브 방송 분석 리포트</title>
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ font-family:'Malgun Gothic',sans-serif; background:#F8F9FA; color:#222; padding:32px; }}
.header {{ display:flex; align-items:center; gap:16px; margin-bottom:24px;
           border-bottom:3px solid #E8720C; padding-bottom:16px; }}
.header-text h1 {{ font-size:1.4rem; color:#E8720C; }}
.header-text p  {{ font-size:0.82rem; color:#888; margin-top:4px; }}
.summary-box {{ background:#E3F2FD; border-radius:12px; padding:14px 18px;
                margin-bottom:16px; font-size:0.92rem; color:#0D47A1; line-height:1.7; }}
.flow-box {{ background:#FFF8E1; border-radius:12px; padding:12px 16px;
             margin-bottom:16px; font-size:0.88rem; color:#5D4037; line-height:1.6; }}
.sent-row {{ display:flex; gap:12px; margin-bottom:20px; }}
.sent-chip {{ flex:1; text-align:center; border-radius:10px; padding:12px;
              font-size:0.9rem; font-weight:700; color:#fff; }}
.topics {{ margin-bottom:20px; }}
.topic-tag {{ background:#F3E5F5; color:#6A1B9A; border-radius:8px;
              padding:3px 10px; font-size:0.8rem; margin-right:6px; }}
.hi-card {{ background:#FFF3E0; border-radius:10px; padding:12px 16px;
            margin-bottom:10px; }}
.hi-label {{ font-weight:700; font-size:0.9rem; color:#E65100; margin-bottom:6px; }}
.hi-samples {{ padding-left:18px; font-size:0.82rem; color:#555; line-height:1.8; }}
.section-title {{ font-size:1rem; font-weight:700; margin:20px 0 10px; color:#333; }}
.cards-grid {{ display:grid; grid-template-columns:repeat(2,1fr); gap:14px; margin-bottom:24px; }}
.cn-card {{ background:#fff; border-radius:12px; padding:16px 18px;
            box-shadow:0 2px 8px rgba(0,0,0,0.07); }}
.cn-title  {{ font-size:0.95rem; font-weight:700; margin-bottom:6px; }}
.cn-badge  {{ display:inline-block; padding:2px 10px; border-radius:14px;
              font-size:0.73rem; font-weight:600; color:#fff; margin-bottom:8px; }}
.cn-insight {{ font-size:0.85rem; line-height:1.6; color:#333; margin-bottom:6px; }}
.cn-evidence {{ font-size:0.78rem; color:#666; background:#F5F5F5;
                border-radius:8px; padding:6px 10px; margin-bottom:5px; }}
.cn-action {{ font-size:0.8rem; color:#1565C0; font-weight:500; }}
.footer {{ text-align:center; font-size:0.72rem; color:#bbb; margin-top:16px; }}
@media print {{ body {{ padding:16px; }} }}
</style></head><body>
<div class="header">
  {logo}
  <div class="header-text">
    <h1>🎬 라이브 방송 분석 리포트</h1>
    <p>{datetime.now().strftime('%Y년 %m월 %d일')} 기준 · Claude AI 분석 · 채팅 {total:,}건</p>
  </div>
</div>
<div class="summary-box">📋 <b>방송 요약</b><br>{lv_analysis.get('summary','')}</div>
<div class="flow-box">📈 <b>방송 흐름</b><br>{lv_analysis.get('flow','')}</div>
<div class="sent-row">
  <div class="sent-chip" style="background:#4CAF50">긍정 {pos_p}%</div>
  <div class="sent-chip" style="background:#9E9E9E">중립 {100-pos_p-neg_p}%</div>
  <div class="sent-chip" style="background:#F44336">부정 {neg_p}%</div>
</div>
<div class="topics">{topics_html}</div>
<div class="section-title">🔥 반응 하이라이트</div>
{hi_html}
<div class="section-title">💡 인사이트 카드</div>
<div class="cards-grid">{cards_html}</div>
<div class="footer">동향 수집하는 호랑이 · 내부용 자료</div>
</body></html>"""

    st.session_state.live_cardnews_html = lv_html
    st.rerun()
