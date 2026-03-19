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
from analysis.claude_analyzer import run_full_analysis, run_quick_summary, _aggregate_theme_stats, summarize_youtube_videos
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

_VERSION = "v1.3"
_TEAM    = "퍼블리싱마케팅4팀"

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
/* ── 다크 리포트 공통 ── */
.dr-wrap { background:#1C1C1E; border-radius:14px; padding:22px 24px; margin-bottom:14px; color:#E8E8E8; }
.dr-brand { font-size:0.67rem; color:#909090; letter-spacing:1.2px; text-transform:uppercase; margin-bottom:10px; }
.dr-brand em { color:#E8720C; font-style:normal; }
.dr-title { font-size:1.9rem; font-weight:900; color:#FFFFFF; line-height:1.15; margin-bottom:6px; }
.dr-title b { color:#E8720C; }
.dr-sub { font-size:0.76rem; color:#AAAAAA; margin-bottom:16px; }
.dr-pills { display:flex; gap:7px; flex-wrap:wrap; }
.dr-pill { background:#2A2A2A; border-radius:4px; padding:3px 10px; font-size:0.7rem; color:#BBBBBB; }
.dr-pill-total { background:#E8720C; border-radius:4px; padding:3px 10px; font-size:0.7rem; font-weight:700; color:white; }
/* ── 섹션 레이블 ── */
.sec-lbl { font-size:0.67rem; color:#909090; letter-spacing:0.8px; text-transform:uppercase;
           font-weight:600; margin:20px 0 12px; padding-bottom:8px; border-bottom:1px solid #333; }
/* ── 감성 메트릭 카드 ── */
.met-card { background:#242424; border-radius:10px; padding:18px 16px; margin-bottom:6px; }
.met-num { font-size:2.8rem; font-weight:800; line-height:1; }
.met-pct { font-size:0.74rem; color:#BBBBBB; margin-top:5px; }
.met-bar { height:3px; border-radius:2px; margin-top:14px; }
/* ── 전체 감성 바 ── */
.sent-total-bar { display:flex; height:7px; border-radius:4px; overflow:hidden; margin:10px 0 6px; }
.sent-lbl-row2 { display:flex; gap:16px; font-size:0.69rem; }
/* ── 채널별 감성 ── */
.ch-row { padding:12px 0; border-bottom:1px solid #2A2A2A; }
.ch-row:last-child { border-bottom:none; }
.ch-name { font-size:0.85rem; font-weight:700; color:#E8E8E8; }
.ch-sub { font-size:0.69rem; color:#AAAAAA; margin-bottom:8px; margin-top:2px; }
.ch-bar-row { display:flex; align-items:center; gap:8px; margin-bottom:3px; }
.ch-bar-lbl { font-size:0.67rem; width:22px; flex-shrink:0; }
.ch-bar-bg { flex:1; background:#2A2A2A; border-radius:3px; height:6px; overflow:hidden; }
.ch-bar-fill { height:100%; border-radius:3px; }
.ch-bar-pct { font-size:0.67rem; color:#AAAAAA; width:28px; text-align:right; flex-shrink:0; }
/* ── 감성 pill ── */
.pill-neg { background:#9B2C2C; color:white; border-radius:6px; padding:5px 14px;
            font-size:0.8rem; font-weight:700; display:inline-block; margin-bottom:8px; }
.pill-pos { background:#276749; color:white; border-radius:6px; padding:5px 14px;
            font-size:0.8rem; font-weight:700; display:inline-block; margin-bottom:8px; }
.pill-neu { background:#2D3748; color:white; border-radius:6px; padding:5px 14px;
            font-size:0.8rem; font-weight:700; display:inline-block; margin-bottom:8px; }
/* ── 테마 테이블 행 ── */
.th-row { display:flex; gap:20px; padding:10px 0; border-bottom:1px solid #2A2A2A; }
.th-row:last-child { border-bottom:none; }
.th-name { font-size:0.84rem; font-weight:700; color:#E0E0E0; min-width:110px; max-width:140px;
           flex-shrink:0; line-height:1.4; word-break:keep-all; }
.th-desc { font-size:0.81rem; color:#C0C0C0; line-height:1.6; }
/* ── 인사이트 카드 ── */
.ins-card { display:flex; gap:16px; padding:14px 0; border-bottom:1px solid #2A2A2A; align-items:flex-start; }
.ins-card:last-child { border-bottom:none; }
.ins-num { background:#9B2C2C; color:white; font-size:0.7rem; font-weight:800;
           width:26px; height:26px; border-radius:4px; display:flex; align-items:center;
           justify-content:center; flex-shrink:0; font-family:monospace; }
.ins-body { flex:1; }
.ins-title { font-size:0.9rem; font-weight:700; color:#EEEEEE; margin-bottom:5px; }
.ins-text { font-size:0.83rem; color:#C0C0C0; line-height:1.65; }
.ins-action { font-size:0.8rem; color:#E8720C; margin-top:6px; }
/* ── 요약 박스 ── */
.sum-box { background:#242424; border-left:4px solid #E8720C; border-radius:0 8px 8px 0;
           padding:16px 18px; font-size:0.87rem; color:#DDDDDD; line-height:1.75; }
/* ── 빠른요약 ── */
.qs-topic-row { display:flex; gap:14px; padding:9px 0; border-bottom:1px solid #2A2A2A; align-items:flex-start; }
.qs-topic-row:last-child { border-bottom:none; }
.qs-rank { background:#2A2A2A; color:#BBBBBB; font-size:0.69rem; font-weight:700;
           width:20px; height:20px; border-radius:3px; display:flex; align-items:center;
           justify-content:center; flex-shrink:0; }
.qs-topic-name { font-size:0.85rem; font-weight:700; color:#EEEEEE; margin-bottom:2px; }
.qs-topic-desc { font-size:0.8rem; color:#BBBBBB; line-height:1.5; }
</style>
"""


def _render_youtube_video_cards(summaries: list):
    """영상별 성격·분위기·댓글 반응 요약 카드"""
    if not summaries:
        return
    st.markdown('<div class="sec-lbl">YouTube 영상별 요약</div>', unsafe_allow_html=True)
    cols = st.columns(2)
    for i, s in enumerate(summaries):
        sent       = s.get("감성", "혼재")
        sent_color = {"긍정": "#276749", "부정": "#9B2C2C"}.get(sent, "#2D3748")
        link       = s.get("링크", "")
        title_html = (f'<a href="{link}" target="_blank" style="color:#EEEEEE;text-decoration:none">'
                      f'{s["영상 제목"]}</a>') if link else s["영상 제목"]
        with cols[i % 2]:
            st.markdown(f"""
<div class="dr-wrap" style="margin-bottom:12px">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;margin-bottom:8px">
    <div style="font-size:0.88rem;font-weight:700;color:#EEEEEE;line-height:1.4;flex:1">{title_html}</div>
    <span style="background:{sent_color};color:white;border-radius:4px;padding:2px 9px;
                 font-size:0.68rem;font-weight:700;white-space:nowrap;flex-shrink:0">{sent}</span>
  </div>
  <div style="font-size:0.72rem;color:#909090;margin-bottom:10px">
    {s.get('채널명','')} &nbsp;·&nbsp; {s.get('업로드일','')} &nbsp;·&nbsp; 댓글 {s.get('댓글수_실제',0):,}개
  </div>
  <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px">
    <span style="background:#E8720C22;color:#E8720C;border-radius:4px;padding:2px 9px;font-size:0.7rem;font-weight:600">{s.get('영상_성격','')}</span>
  </div>
  <div style="font-size:0.82rem;color:#BBBBBB;line-height:1.65;margin-bottom:8px">{s.get('분위기','')}</div>
  <div style="border-top:1px solid #2A2A2A;padding-top:10px;font-size:0.8rem;color:#AAAAAA;line-height:1.6">{s.get('댓글_요약','')}</div>
</div>""", unsafe_allow_html=True)


def _render_quick_summary(qs: dict):
    """빠른 요약 결과를 다크 테마로 렌더링"""
    st.markdown(_CARD_CSS, unsafe_allow_html=True)

    sent       = qs.get("sentiment", "혼재")
    sent_color = {"긍정": "#276749", "부정": "#9B2C2C"}.get(sent, "#2D3748")
    sample_cnt = qs.get("sample_count", 0)
    total_cnt  = qs.get("total_count", 0)

    st.markdown(f"""
<div class="dr-wrap">
  <div class="dr-brand"><em>●</em> 동향 수집하는 호랑이 · QUICK SUMMARY</div>
  <div class="dr-title">빠른 <b>동향 요약</b></div>
  <div class="dr-sub">전체 {total_cnt:,}건 중 샘플 {sample_cnt}건 기반 · Claude AI 정성 추정</div>
  <span style="background:{sent_color};color:white;border-radius:6px;padding:4px 12px;
               font-size:0.78rem;font-weight:700">전반적 분위기: {sent}</span>
</div>""", unsafe_allow_html=True)

    if qs.get("summary"):
        st.markdown('<div class="sec-lbl">전체 동향</div>', unsafe_allow_html=True)
        reason = (
            f'<br><br><span style="color:#BBBBBB;font-size:0.82rem;font-style:italic">'
            f'💡 {qs["sentiment_reason"]}</span>'
            if qs.get("sentiment_reason") else ""
        )
        st.markdown(f'<div class="sum-box">{qs["summary"]}{reason}</div>', unsafe_allow_html=True)

    col_topics, col_watch = st.columns([3, 2])

    with col_topics:
        st.markdown('<div class="sec-lbl">핫 토픽</div>', unsafe_allow_html=True)
        topics_html = '<div style="background:#1C1C1E;border-radius:10px;padding:12px 16px">'
        for topic in qs.get("hot_topics", []):
            t_sent  = topic.get("sentiment", "혼재")
            t_color = {"긍정": "#276749", "부정": "#9B2C2C"}.get(t_sent, "#2D3748")
            topics_html += f"""
<div class="qs-topic-row">
  <div class="qs-rank">{topic.get('rank','')}</div>
  <div style="flex:1">
    <div class="qs-topic-name">{topic.get('topic','')}</div>
    <div class="qs-topic-desc">{topic.get('desc','')}</div>
  </div>
  <span style="background:{t_color};color:white;border-radius:4px;padding:2px 8px;
               font-size:0.67rem;font-weight:600;white-space:nowrap;flex-shrink:0">{t_sent}</span>
</div>"""
        topics_html += '</div>'
        st.markdown(topics_html, unsafe_allow_html=True)

    with col_watch:
        if qs.get("watch_point"):
            st.markdown('<div class="sec-lbl">주목 이슈</div>', unsafe_allow_html=True)
            st.markdown(f"""
<div style="background:#2D1B1B;border-left:4px solid #E53E3E;border-radius:0 8px 8px 0;
            padding:14px 16px;font-size:0.84rem;color:#FBB;line-height:1.65;margin-bottom:12px">
  ⚠️ {qs['watch_point']}
</div>""", unsafe_allow_html=True)

        st.markdown("""
<div style="background:#1C1C1E;border-radius:8px;padding:12px 14px;
            border:1px solid #2A2A2A;font-size:0.73rem;color:#909090;line-height:1.8">
  <span style="color:#BBBBBB;font-weight:600">📌 이 결과를 볼 때 꼭 알아두세요</span><br>
  · 샘플 기반 <span style="color:#BBBBBB">추정치</span> — 수치 그대로 인용 금지<br>
  · 누락된 맥락이 있을 수 있음<br>
  · 정확한 근거가 필요하면 <span style="color:#BBBBBB">정밀 분석</span> 사용<br>
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
    neg_pct = 100 - pos_pct - neu_pct

    platform_counts = {p: len(v) for p, v in items_by_plat.items() if v}

    # 플랫폼별 감성 집계
    plat_sent = {}
    for plat, items_list in items_by_plat.items():
        if not items_list: continue
        pp = sum(1 for i in items_list if i.get("sentiment") == "긍정")
        pn = sum(1 for i in items_list if i.get("sentiment") == "중립")
        pg = sum(1 for i in items_list if i.get("sentiment") == "부정")
        pt = pp + pn + pg or 1
        plat_sent[plat] = {
            "count": pt, "pos_pct": round(pp/pt*100),
            "neu_pct": round(pn/pt*100), "neg_pct": round(pg/pt*100),
        }

    st.markdown(_CARD_CSS, unsafe_allow_html=True)

    # ── 리포트 헤더 ──────────────────────────────────────────
    pills_html = "".join(
        f'<span class="dr-pill">{p} {n:,}건</span>' for p, n in platform_counts.items()
    )
    st.markdown(f"""
<div class="dr-wrap">
  <div class="dr-brand"><em>●</em> 동향 수집하는 호랑이 · TREND REPORT {datetime.now().year}</div>
  <div class="dr-title">게임 커뮤니티 <b>동향 분석</b></div>
  <div class="dr-sub">{datetime.now().strftime('%Y년 %m월 %d일')} 기준 · Claude AI 분석 · {len(platform_counts)}개 채널</div>
  <div class="dr-pills">{pills_html}<span class="dr-pill-total">총 {total:,}건</span></div>
</div>""", unsafe_allow_html=True)

    # ── 전체 감성 분포 ──────────────────────────────────────
    st.markdown('<div class="sec-lbl">전체 감성 분포</div>', unsafe_allow_html=True)
    m1, m2, m3 = st.columns(3)
    for col, cnt, pct, label, color in [
        (m1, neg, neg_pct, "부정", "#E53E3E"),
        (m2, neu, neu_pct, "중립", "#4A5568"),
        (m3, pos, pos_pct, "긍정", "#38A169"),
    ]:
        with col:
            st.markdown(f"""
<div class="met-card">
  <div class="met-num" style="color:{color}">{cnt:,}</div>
  <div class="met-pct">{label} · 전체의 {pct}%</div>
  <div class="met-bar" style="background:{color};width:{pct}%"></div>
</div>""", unsafe_allow_html=True)

    st.markdown(f"""
<div class="sent-total-bar">
  <div style="width:{neg_pct}%;background:#E53E3E"></div>
  <div style="width:{neu_pct}%;background:#4A5568"></div>
  <div style="width:{pos_pct}%;background:#38A169"></div>
</div>
<div class="sent-lbl-row2">
  <span style="color:#E53E3E">부정 {neg_pct}%</span>
  <span style="color:#4A5568">중립 {neu_pct}%</span>
  <span style="color:#38A169">긍정 {pos_pct}%</span>
</div>""", unsafe_allow_html=True)

    # ── 채널별 감성 비교 ──────────────────────────────────────
    if len(plat_sent) > 1:
        st.markdown('<div class="sec-lbl">채널별 감성 비교</div>', unsafe_allow_html=True)
        ch_html = '<div style="background:#1C1C1E;border-radius:12px;padding:14px 18px">'
        for plat, ps in plat_sent.items():
            ch_html += f"""
<div class="ch-row">
  <div class="ch-name">{plat}</div>
  <div class="ch-sub">{ps['count']:,}건</div>
  <div>
    <div class="ch-bar-row">
      <span class="ch-bar-lbl" style="color:#E53E3E">부정</span>
      <div class="ch-bar-bg"><div class="ch-bar-fill" style="width:{ps['neg_pct']}%;background:#E53E3E"></div></div>
      <span class="ch-bar-pct">{ps['neg_pct']}%</span>
    </div>
    <div class="ch-bar-row">
      <span class="ch-bar-lbl" style="color:#718096">중립</span>
      <div class="ch-bar-bg"><div class="ch-bar-fill" style="width:{ps['neu_pct']}%;background:#4A5568"></div></div>
      <span class="ch-bar-pct">{ps['neu_pct']}%</span>
    </div>
    <div class="ch-bar-row">
      <span class="ch-bar-lbl" style="color:#38A169">긍정</span>
      <div class="ch-bar-bg"><div class="ch-bar-fill" style="width:{ps['pos_pct']}%;background:#38A169"></div></div>
      <span class="ch-bar-pct">{ps['pos_pct']}%</span>
    </div>
  </div>
</div>"""
        ch_html += '</div>'
        st.markdown(ch_html, unsafe_allow_html=True)

    # ── 종합 요약 ────────────────────────────────────────────
    if insights.get("summary"):
        st.markdown('<div class="sec-lbl">종합 동향 요약</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="sum-box">{insights["summary"]}</div>', unsafe_allow_html=True)

    # ── 주요 동향 요약 (테마, 감성별 그룹) ──────────────────
    if theme_stats:
        def _dominant(p, n, g):
            return max([("긍정", p), ("중립", n), ("부정", g)], key=lambda x: x[1])[0]

        groups: dict = {"부정": [], "중립": [], "긍정": []}
        for stat in theme_stats:
            theme_obj = next((t for t in unified_themes if t.get("name") == stat["name"]), {})
            dom = _dominant(stat["pos"], stat["neu"], stat["neg"])
            groups[dom].append((stat, theme_obj))

        st.markdown('<div class="sec-lbl">주요 동향 요약</div>', unsafe_allow_html=True)
        theme_html = '<div style="background:#1C1C1E;border-radius:12px;padding:14px 18px">'
        for sent_label, pill_cls in [("부정", "pill-neg"), ("긍정", "pill-pos"), ("중립", "pill-neu")]:
            grp = groups[sent_label]
            if not grp:
                continue
            rows = ""
            for stat, theme_obj in grp:
                desc     = theme_obj.get("desc", "")
                examples = [ex for ex in stat.get("examples", []) if ex]
                ex_text  = "  ".join(f'"{ex}"' for ex in examples[:2])
                combined = f"{desc} {ex_text}".strip()
                rows += f"""
<div class="th-row">
  <div class="th-name">{stat['name']}</div>
  <div class="th-desc">{combined}</div>
</div>"""
            theme_html += f"""
<div style="margin-bottom:20px">
  <span class="{pill_cls}">{sent_label} · {len(grp)}개 이슈</span>
  {rows}
</div>"""
        theme_html += '</div>'
        st.markdown(theme_html, unsafe_allow_html=True)

    # ── 핵심 인사이트 ────────────────────────────────────────
    cards = insights.get("cards", [])
    if not insights:
        st.warning("인사이트 생성에 실패했습니다. 분석 로그를 확인하세요.", icon="⚠️")
        _err = [l for l in st.session_state.get("analysis_log", []) if "⚠️" in l]
        if _err:
            st.caption("\n".join(_err[-3:]))
    elif cards:
        st.markdown('<div class="sec-lbl">핵심 인사이트</div>', unsafe_allow_html=True)
        ins_html = '<div style="background:#1C1C1E;border-radius:12px;padding:12px 18px">'
        for i, card in enumerate(cards, 1):
            ins_html += f"""
<div class="ins-card">
  <div class="ins-num">{i:02d}</div>
  <div class="ins-body">
    <div class="ins-title">{card.get('title','')}</div>
    <div class="ins-text">{card.get('insight','')}</div>
    {f'<div class="ins-action">➡ {card["action"]}</div>' if card.get('action') else ''}
  </div>
</div>"""
        ins_html += '</div>'
        st.markdown(ins_html, unsafe_allow_html=True)
    elif insights:
        st.info("인사이트 카드 데이터가 없습니다. 분석을 다시 실행해 보세요.")


def _build_cardnews_html(ar: dict, label: str) -> str:
    """분석 결과 → 다크 테마 리포트 HTML 반환"""
    ins            = ar.get("insights", {})
    unified_themes = ar.get("unified_themes", [])
    items_by_plat  = ar.get("platform_items", {})
    theme_stats    = _aggregate_theme_stats(unified_themes, items_by_plat)

    b64  = _tiger_b64()
    logo = f'<img src="data:image/png;base64,{b64}" style="height:44px">' if b64 else "🐯"

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
        f'<span style="background:#2A2A2A;border-radius:4px;padding:3px 10px;'
        f'font-size:0.7rem;color:#BBBBBB">{p} {len(v):,}건</span>'
        for p, v in items_by_plat.items() if v
    )

    # 인사이트 카드 HTML
    def _dom(p, n, g):
        return max([("긍정", p), ("중립", n), ("부정", g)], key=lambda x: x[1])[0]

    ins_rows = ""
    for i, card in enumerate(ins.get("cards", []), 1):
        action_h = f'<div style="font-size:0.8rem;color:#E8720C;margin-top:6px">➡ {card["action"]}</div>' if card.get("action") else ""
        ins_rows += f"""
<div style="display:flex;gap:16px;padding:14px 0;border-bottom:1px solid #2A2A2A;align-items:flex-start">
  <div style="background:#9B2C2C;color:white;font-size:0.7rem;font-weight:800;
              width:26px;height:26px;border-radius:4px;display:flex;align-items:center;
              justify-content:center;flex-shrink:0;font-family:monospace">{i:02d}</div>
  <div>
    <div style="font-size:0.9rem;font-weight:700;color:#E0E0E0;margin-bottom:5px">{card.get('title','')}</div>
    <div style="font-size:0.83rem;color:#C0C0C0;line-height:1.65">{card.get('insight','')}</div>
    {action_h}
  </div>
</div>"""

    ins_section = f"""
<div style="font-size:0.67rem;color:#909090;letter-spacing:0.8px;text-transform:uppercase;font-weight:600;
            padding-bottom:8px;border-bottom:1px solid #2A2A2A;margin-bottom:0">핵심 인사이트</div>
<div style="background:#1C1C1E;border-radius:12px;padding:12px 18px;margin-bottom:24px">{ins_rows}</div>
""" if ins_rows else ""

    # 테마 섹션 HTML (감성별 그룹)
    theme_section = ""
    if theme_stats:
        groups: dict = {"부정": [], "중립": [], "긍정": []}
        for stat in theme_stats:
            theme_obj = next((t for t in unified_themes if t.get("name") == stat["name"]), {})
            dom = _dom(stat["pos"], stat["neu"], stat["neg"])
            groups[dom].append((stat, theme_obj))

        pill_styles = {
            "부정": "background:#9B2C2C;color:white",
            "긍정": "background:#276749;color:white",
            "중립": "background:#2D3748;color:white",
        }
        grp_html = ""
        for sent_label in ["부정", "긍정", "중립"]:
            grp = groups[sent_label]
            if not grp: continue
            rows = ""
            for stat, theme_obj in grp:
                desc     = theme_obj.get("desc", "")
                examples = [ex for ex in stat.get("examples", []) if ex]
                ex_text  = "  ".join(f'"{ex}"' for ex in examples[:2])
                combined = f"{desc} {ex_text}".strip()
                rows += f"""
<div style="display:flex;gap:20px;padding:10px 0;border-bottom:1px solid #2A2A2A">
  <div style="font-size:0.84rem;font-weight:700;color:#E0E0E0;min-width:110px;max-width:140px;
              flex-shrink:0;line-height:1.4;word-break:keep-all">{stat['name']}</div>
  <div style="font-size:0.81rem;color:#C0C0C0;line-height:1.6">{combined}</div>
</div>"""
            grp_html += f"""
<div style="margin-bottom:20px">
  <span style="{pill_styles[sent_label]};border-radius:6px;padding:5px 14px;
               font-size:0.8rem;font-weight:700;display:inline-block;margin-bottom:8px">
    {sent_label} · {len(grp)}개 이슈</span>
  {rows}
</div>"""

        theme_section = f"""
<div style="font-size:0.67rem;color:#909090;letter-spacing:0.8px;text-transform:uppercase;font-weight:600;
            padding-bottom:8px;border-bottom:1px solid #2A2A2A;margin-bottom:0">주요 동향 요약</div>
<div style="background:#1C1C1E;border-radius:12px;padding:14px 18px;margin-bottom:24px">{grp_html}</div>"""

    # 채널별 감성 바
    plat_sent_cn = {}
    for plat, items_list in items_by_plat.items():
        if not items_list: continue
        pp = sum(1 for i in items_list if i.get("sentiment") == "긍정")
        pn = sum(1 for i in items_list if i.get("sentiment") == "중립")
        pg = sum(1 for i in items_list if i.get("sentiment") == "부정")
        pt = pp + pn + pg or 1
        plat_sent_cn[plat] = {"count": pt, "pos_pct": round(pp/pt*100), "neu_pct": round(pn/pt*100), "neg_pct": round(pg/pt*100)}

    ch_section = ""
    if len(plat_sent_cn) > 1:
        ch_rows = ""
        for plat, ps in plat_sent_cn.items():
            ch_rows += f"""
<div style="padding:12px 0;border-bottom:1px solid #2A2A2A">
  <div style="font-size:0.85rem;font-weight:700;color:#E0E0E0">{plat}</div>
  <div style="font-size:0.69rem;color:#909090;margin-bottom:8px">{ps['count']:,}건</div>
  <div style="display:flex;align-items:center;gap:8px;margin-bottom:3px">
    <span style="font-size:0.67rem;color:#E53E3E;width:22px">부정</span>
    <div style="flex:1;background:#2A2A2A;border-radius:3px;height:6px">
      <div style="width:{ps['neg_pct']}%;background:#E53E3E;height:6px;border-radius:3px"></div></div>
    <span style="font-size:0.67rem;color:#AAAAAA;width:28px;text-align:right">{ps['neg_pct']}%</span>
  </div>
  <div style="display:flex;align-items:center;gap:8px;margin-bottom:3px">
    <span style="font-size:0.67rem;color:#718096;width:22px">중립</span>
    <div style="flex:1;background:#2A2A2A;border-radius:3px;height:6px">
      <div style="width:{ps['neu_pct']}%;background:#4A5568;height:6px;border-radius:3px"></div></div>
    <span style="font-size:0.67rem;color:#AAAAAA;width:28px;text-align:right">{ps['neu_pct']}%</span>
  </div>
  <div style="display:flex;align-items:center;gap:8px">
    <span style="font-size:0.67rem;color:#38A169;width:22px">긍정</span>
    <div style="flex:1;background:#2A2A2A;border-radius:3px;height:6px">
      <div style="width:{ps['pos_pct']}%;background:#38A169;height:6px;border-radius:3px"></div></div>
    <span style="font-size:0.67rem;color:#AAAAAA;width:28px;text-align:right">{ps['pos_pct']}%</span>
  </div>
</div>"""
        ch_section = f"""
<div style="font-size:0.67rem;color:#909090;letter-spacing:0.8px;text-transform:uppercase;font-weight:600;
            padding-bottom:8px;border-bottom:1px solid #2A2A2A;margin-bottom:0">채널별 감성 비교</div>
<div style="background:#1C1C1E;border-radius:12px;padding:14px 18px;margin-bottom:24px">{ch_rows}</div>"""

    summary_html = f"""
<div style="font-size:0.67rem;color:#909090;letter-spacing:0.8px;text-transform:uppercase;font-weight:600;
            padding-bottom:8px;border-bottom:1px solid #2A2A2A;margin-bottom:12px">종합 동향 요약</div>
<div style="background:#242424;border-left:4px solid #E8720C;border-radius:0 8px 8px 0;
            padding:16px 18px;font-size:0.87rem;color:#CCC;line-height:1.75;margin-bottom:24px">{ins["summary"]}</div>
""" if ins.get("summary") else ""

    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="UTF-8">
<title>{label} 게임 동향 분석 리포트</title>
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ font-family:'Malgun Gothic','Apple SD Gothic Neo',sans-serif;
        background:#141416; color:#E8E8E8; padding:36px; max-width:960px; margin:0 auto; }}
@media print {{ body {{ background:#141416; padding:16px; }} }}
</style></head><body>

<div style="background:#1C1C1E;border-radius:14px;padding:24px 28px;margin-bottom:24px">
  <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px">
    {logo}
    <div>
      <div style="font-size:0.67rem;color:#909090;letter-spacing:1px">● 동향 수집하는 호랑이 · TREND REPORT {datetime.now().year}</div>
      <div style="font-size:1.7rem;font-weight:900;color:#FFFFFF;margin-top:4px;line-height:1.15">
        {label} <span style="color:#E8720C">동향 분석</span></div>
      <div style="font-size:0.74rem;color:#AAAAAA;margin-top:5px">{datetime.now().strftime('%Y년 %m월 %d일')} 기준 · Claude AI 분석</div>
    </div>
  </div>
  <div style="display:flex;gap:7px;flex-wrap:wrap;margin-top:10px">
    {plat_pills}
    <span style="background:#E8720C;border-radius:4px;padding:3px 10px;font-size:0.7rem;font-weight:700;color:white">총 {total:,}건</span>
  </div>
</div>

<div style="font-size:0.67rem;color:#909090;letter-spacing:0.8px;text-transform:uppercase;font-weight:600;
            padding-bottom:8px;border-bottom:1px solid #2A2A2A;margin-bottom:12px">전체 감성 분포</div>
<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:10px">
  <div style="background:#242424;border-radius:10px;padding:18px 16px">
    <div style="font-size:2.6rem;font-weight:800;color:#E53E3E;line-height:1">{neg:,}</div>
    <div style="font-size:0.72rem;color:#BBBBBB;margin-top:5px">부정 · 전체의 {neg_pct}%</div>
    <div style="height:3px;background:#E53E3E;border-radius:2px;margin-top:12px;width:{neg_pct}%"></div>
  </div>
  <div style="background:#242424;border-radius:10px;padding:18px 16px">
    <div style="font-size:2.6rem;font-weight:800;color:#4A5568;line-height:1">{neu:,}</div>
    <div style="font-size:0.72rem;color:#BBBBBB;margin-top:5px">중립 · 전체의 {neu_pct}%</div>
    <div style="height:3px;background:#4A5568;border-radius:2px;margin-top:12px;width:{neu_pct}%"></div>
  </div>
  <div style="background:#242424;border-radius:10px;padding:18px 16px">
    <div style="font-size:2.6rem;font-weight:800;color:#38A169;line-height:1">{pos:,}</div>
    <div style="font-size:0.72rem;color:#BBBBBB;margin-top:5px">긍정 · 전체의 {pos_pct}%</div>
    <div style="height:3px;background:#38A169;border-radius:2px;margin-top:12px;width:{pos_pct}%"></div>
  </div>
</div>
<div style="display:flex;height:7px;border-radius:4px;overflow:hidden;margin-bottom:6px">
  <div style="width:{neg_pct}%;background:#E53E3E"></div>
  <div style="width:{neu_pct}%;background:#4A5568"></div>
  <div style="width:{pos_pct}%;background:#38A169"></div>
</div>
<div style="display:flex;gap:16px;font-size:0.69rem;margin-bottom:24px">
  <span style="color:#E53E3E">부정 {neg_pct}%</span>
  <span style="color:#4A5568">중립 {neu_pct}%</span>
  <span style="color:#38A169">긍정 {pos_pct}%</span>
</div>

{ch_section}
{summary_html}
{theme_section}
{ins_section}

<div style="text-align:center;font-size:0.7rem;color:#333;margin-top:8px;padding-top:16px;border-top:1px solid #2A2A2A">
  동향 수집하는 호랑이 · {_TEAM} · {_VERSION} · 내부용 자료</div>
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
if "yt_video_summaries" not in st.session_state:
    st.session_state.yt_video_summaries = []

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
    <div style="font-size:1rem;color:#333333;margin-top:8px">
      게임 커뮤니티 반응을 플랫폼별로 수집하고 Claude AI로 분석합니다
    </div>
    <div style="font-size:0.72rem;color:#555555;margin-top:6px;letter-spacing:0.3px">
      {_TEAM} &nbsp;·&nbsp; {_VERSION}
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
                st.markdown('<span style="color:#CCCCCC">📺 YouTube 댓글</span>', unsafe_allow_html=True)
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
                st.markdown('<span style="color:#CCCCCC">💬 디시인사이드</span>', unsafe_allow_html=True)
        if use_dc:
            dc1, dc2, dc3 = st.columns([3, 1, 1])
            with dc1:
                dc_gallery_id = st.text_input("갤러리 ID", placeholder="예: lostark", key="dc_gid")
            with dc2:
                dc_is_minor = st.checkbox("마이너", value=True)
            with dc3:
                dc_max_pages = st.number_input(
                    "페이지 수", min_value=1, max_value=20, value=5, key="dc_pages",
                    help="페이지 수가 많을수록 IP 차단 위험 증가. 5 이하 권장."
                )
            # 페이지 수 기반 위험도 안내
            if dc_max_pages <= 5:
                st.caption("✅ 안전 범위 (5페이지 이하)")
            elif dc_max_pages <= 10:
                st.caption("⚠️ 주의 — 반복 수집 시 IP 차단 가능")
            else:
                st.caption("🚨 위험 — IP 차단 가능성 높음. 하루 1회 이하 권장")
        else:
            dc_gallery_id = ""
            dc_is_minor   = True
            dc_max_pages  = 5

    # ── 앱스토어 ──────────────────────────────────────────────
    with st.container(border=True):
        tog_col, title_col = st.columns([1, 8])
        with tog_col:
            use_appstore = st.toggle("", value=True, key="use_app")
        with title_col:
            if use_appstore:
                st.markdown("**🍎 앱스토어**")
            else:
                st.markdown('<span style="color:#CCCCCC">🍎 앱스토어</span>', unsafe_allow_html=True)
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
                st.markdown('<span style="color:#CCCCCC">🤖 플레이스토어</span>', unsafe_allow_html=True)
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
    st.caption("💳 AI 분석은 퍼블리싱마케팅4팀 GCP 비용으로 운영됩니다. 과도한 사용은 지양 부탁드립니다.")

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
        _dc_label = "디시인사이드 ⛔차단" if result.get("dc_blocked") else "디시인사이드"
        c2.metric(_dc_label,      result.get("dc_count",   "—"))
        c3.metric("앱스토어",     result.get("app_count",  "—"))
        c4.metric("플레이스토어", result.get("play_count", "—"))

        if result.get("dc_blocked"):
            st.error(
                "⛔ **디시인사이드 IP 차단 감지** — 수집이 중단됐습니다.  \n"
                "차단 전까지 수집된 데이터는 분석에 포함됩니다.  \n"
                "동일 IP로 재수집은 몇 시간 후 시도하세요.",
                icon="🚫"
            )

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
            # YouTube 영상별 요약 (항상 최상단)
            if st.session_state.yt_video_summaries:
                st.markdown(_CARD_CSS, unsafe_allow_html=True)
                _render_youtube_video_cards(st.session_state.yt_video_summaries)
                st.markdown(
                    '<hr style="border:none;border-top:1px solid #2A2A2A;margin:24px 0">',
                    unsafe_allow_html=True
                )

            # 빠른 요약 결과
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

        # ── 카드뉴스 에이전트용 JSON ──────────────────────────
        st.markdown("---")
        st.markdown("#### 🤖 카드뉴스 에이전트용 데이터")
        if not st.session_state.analysis_result:
            st.info("STEP 2 분석 완료 후 다운로드할 수 있습니다.")
        else:
            _export = {
                "keyword": _label,
                "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "insights": st.session_state.analysis_result.get("insights", {}),
                "unified_themes": st.session_state.analysis_result.get("unified_themes", []),
                "platform_summary": {
                    p: len(items)
                    for p, items in st.session_state.analysis_result.get("platform_items", {}).items()
                    if items
                },
                "youtube_video_summaries": st.session_state.yt_video_summaries or [],
            }
            _json_bytes = json.dumps(_export, ensure_ascii=False, indent=2).encode("utf-8")
            _json_file  = f"{_label}_분석결과_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
            st.download_button(
                label="📥 JSON 다운로드",
                data=_json_bytes,
                file_name=_json_file,
                mime="application/json",
            )
            st.caption("카드뉴스 에이전트에 이 파일을 넣으면 카드뉴스가 자동 생성됩니다.")

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

    # YouTube 영상별 요약 (영상이 있을 때만)
    _cr = st.session_state.collection_result
    if _cr.get("yt_videos") and _cr.get("yt_comments"):
        main_status_placeholder.caption("📺 YouTube 영상별 요약 중...")
        st.session_state.yt_video_summaries = summarize_youtube_videos(
            project_id=_GCP_PROJECT, region=_GCP_REGION,
            yt_videos=_cr["yt_videos"], yt_comments=_cr["yt_comments"],
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

    # YouTube 영상별 요약 (영상이 있을 때만, 아직 없을 때만 실행)
    _cr = st.session_state.collection_result
    if _cr.get("yt_videos") and _cr.get("yt_comments") and not st.session_state.yt_video_summaries:
        main_tiger_placeholder.markdown(_tiger_html("영상 요약 중"), unsafe_allow_html=True)
        st.session_state.yt_video_summaries = summarize_youtube_videos(
            project_id=gcp_project_id, region=gcp_region,
            yt_videos=_cr["yt_videos"], yt_comments=_cr["yt_comments"],
            log_fn=alog_fn,
        )
        main_tiger_placeholder.empty()

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
.header-text p  {{ font-size:0.82rem; color:#BBBBBB; margin-top:4px; }}
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
.hi-samples {{ padding-left:18px; font-size:0.82rem; color:#909090; line-height:1.8; }}
.section-title {{ font-size:1rem; font-weight:700; margin:20px 0 10px; color:#333; }}
.cards-grid {{ display:grid; grid-template-columns:repeat(2,1fr); gap:14px; margin-bottom:24px; }}
.cn-card {{ background:#fff; border-radius:12px; padding:16px 18px;
            box-shadow:0 2px 8px rgba(0,0,0,0.07); }}
.cn-title  {{ font-size:0.95rem; font-weight:700; margin-bottom:6px; }}
.cn-badge  {{ display:inline-block; padding:2px 10px; border-radius:14px;
              font-size:0.73rem; font-weight:600; color:#fff; margin-bottom:8px; }}
.cn-insight {{ font-size:0.85rem; line-height:1.6; color:#333; margin-bottom:6px; }}
.cn-evidence {{ font-size:0.78rem; color:#AAAAAA; background:#F5F5F5;
                border-radius:8px; padding:6px 10px; margin-bottom:5px; }}
.cn-action {{ font-size:0.8rem; color:#1565C0; font-weight:500; }}
.footer {{ text-align:center; font-size:0.72rem; color:#CCCCCC; margin-top:16px; }}
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

# ─── 페이지 푸터 ──────────────────────────────────────────────
st.markdown(
    f'<div style="text-align:center;color:#CCCCCC;font-size:0.72rem;'
    f'padding:32px 0 12px;letter-spacing:0.3px">'
    f'{_TEAM} &nbsp;·&nbsp; 동향 수집하는 호랑이 &nbsp;·&nbsp; {_VERSION}'
    f'</div>',
    unsafe_allow_html=True
)
