"""Plotly 차트 생성 함수"""
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd

SENTIMENT_COLORS = {"긍정": "#4CAF50", "중립": "#9E9E9E", "부정": "#F44336"}
PLATFORM_COLORS  = {
    "YouTube 댓글":  "#FF0000",
    "디시인사이드":  "#1565C0",
    "앱스토어":      "#555555",
    "플레이스토어":  "#34A853",
}


def sentiment_by_platform(items_by_platform: dict) -> go.Figure:
    """플랫폼별 긍/중/부 비율 — 그룹 바 차트"""
    rows = []
    for platform, items in items_by_platform.items():
        if not items:
            continue
        total = len(items)
        pos = sum(1 for i in items if i.get("sentiment") == "긍정")
        neu = sum(1 for i in items if i.get("sentiment") == "중립")
        neg = sum(1 for i in items if i.get("sentiment") == "부정")
        rows.append({
            "플랫폼": platform,
            "긍정": round(pos / total * 100, 1),
            "중립": round(neu / total * 100, 1),
            "부정": round(neg / total * 100, 1),
        })

    if not rows:
        return _empty_fig("감성 분석 데이터 없음")

    df = pd.DataFrame(rows)
    fig = go.Figure()
    for sent, color in SENTIMENT_COLORS.items():
        fig.add_trace(go.Bar(
            name=sent, x=df["플랫폼"], y=df[sent],
            marker_color=color, text=df[sent].apply(lambda v: f"{v}%"),
            textposition="auto"
        ))

    fig.update_layout(
        barmode="group",
        title=dict(text="플랫폼별 감성 분포", font=dict(size=15, color="#222")),
        yaxis_title="%",
        plot_bgcolor="white",
        paper_bgcolor="white",
        legend=dict(orientation="h", y=1.12, font=dict(size=12)),
        font=dict(family="'Malgun Gothic', sans-serif", size=12),
        margin=dict(t=60, b=40, l=20, r=20),
        yaxis=dict(gridcolor="#F0F0F0", gridwidth=1),
        xaxis=dict(tickfont=dict(size=12)),
    )
    return fig


def theme_volume_chart(theme_stats: list) -> go.Figure:
    """테마별 언급 건수 — 수평 바 차트"""
    if not theme_stats:
        return _empty_fig("테마 데이터 없음")

    names  = [s["name"]  for s in theme_stats]
    counts = [s["count"] for s in theme_stats]

    fig = go.Figure(go.Bar(
        x=counts, y=names, orientation="h",
        marker_color="#1976D2",
        text=counts, textposition="auto"
    ))
    fig.update_layout(
        title="테마별 언급 건수",
        xaxis_title="건수",
        plot_bgcolor="white",
        yaxis=dict(autorange="reversed"),
        margin=dict(t=60, b=40)
    )
    return fig


def theme_sentiment_chart(theme_stats: list) -> go.Figure:
    """테마별 감성 비율 — 누적 수평 바"""
    if not theme_stats:
        return _empty_fig("테마 데이터 없음")

    names = [s["name"] for s in theme_stats]
    fig = go.Figure()
    for sent, color in SENTIMENT_COLORS.items():
        key = {"긍정": "pos", "중립": "neu", "부정": "neg"}[sent]
        vals = [s[key] for s in theme_stats]
        fig.add_trace(go.Bar(
            name=sent, x=vals, y=names, orientation="h",
            marker_color=color,
            text=[f"{v}%" for v in vals], textposition="auto"
        ))

    fig.update_layout(
        barmode="stack", title="테마별 감성 비율",
        xaxis_title="%",
        plot_bgcolor="white",
        legend=dict(orientation="h", y=1.1),
        yaxis=dict(autorange="reversed"),
        margin=dict(t=60, b=40)
    )
    return fig


def overall_sentiment_donut(items_by_platform: dict) -> go.Figure:
    """전체 감성 비율 도넛 차트"""
    pos = neu = neg = 0
    for items in items_by_platform.values():
        for item in items:
            s = item.get("sentiment", "중립")
            if s == "긍정":   pos += 1
            elif s == "부정": neg += 1
            else:             neu += 1

    total = pos + neu + neg
    if total == 0:
        return _empty_fig("데이터 없음")

    fig = go.Figure(go.Pie(
        labels=["긍정", "중립", "부정"],
        values=[pos, neu, neg],
        hole=0.55,
        marker_colors=[SENTIMENT_COLORS["긍정"], SENTIMENT_COLORS["중립"], SENTIMENT_COLORS["부정"]],
        textinfo="label+percent"
    ))
    fig.update_layout(
        title=f"전체 감성 분포 (총 {total:,}건)",
        margin=dict(t=60, b=20)
    )
    return fig


def live_sentiment_timeline(buckets: list) -> go.Figure:
    """라이브 채팅 시간대별 감성 흐름 — 누적 영역 차트"""
    if not buckets:
        return _empty_fig("시간대 데이터 없음")

    labels   = [b["label"]   for b in buckets]
    pos_vals = [b["pos_pct"] for b in buckets]
    neu_vals = [b["neu_pct"] for b in buckets]
    neg_vals = [b["neg_pct"] for b in buckets]
    volumes  = [b["total"]   for b in buckets]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=labels, y=pos_vals, name="긍정",
        fill="tozeroy", mode="lines",
        line=dict(color="#4CAF50", width=2),
        fillcolor="rgba(76,175,80,0.25)"
    ))
    fig.add_trace(go.Scatter(
        x=labels, y=neg_vals, name="부정",
        fill="tozeroy", mode="lines",
        line=dict(color="#F44336", width=2),
        fillcolor="rgba(244,67,54,0.2)"
    ))
    # 채팅 볼륨 보조축 (바)
    fig.add_trace(go.Bar(
        x=labels, y=volumes, name="채팅 볼륨",
        marker_color="rgba(150,150,150,0.3)",
        yaxis="y2"
    ))

    peak_labels = [b["label"] for b in buckets if b.get("is_peak")]
    peak_pos    = [b["pos_pct"] for b in buckets if b.get("is_peak")]
    if peak_labels:
        fig.add_trace(go.Scatter(
            x=peak_labels, y=peak_pos, name="반응 피크",
            mode="markers",
            marker=dict(symbol="star", size=14, color="#FF6F00")
        ))

    fig.update_layout(
        title="시간대별 감성 흐름",
        xaxis_title="시간",
        yaxis=dict(title="감성 비율 (%)", range=[0, 100]),
        yaxis2=dict(title="채팅 수", overlaying="y", side="right",
                    showgrid=False),
        plot_bgcolor="white",
        legend=dict(orientation="h", y=1.12),
        margin=dict(t=70, b=50),
    )
    return fig


def live_sentiment_donut(tagged_chats: list) -> go.Figure:
    """라이브 전체 감성 도넛"""
    pos = sum(1 for c in tagged_chats if c.get("sentiment") == "긍정")
    neg = sum(1 for c in tagged_chats if c.get("sentiment") == "부정")
    neu = len(tagged_chats) - pos - neg
    total = len(tagged_chats)
    if total == 0:
        return _empty_fig("데이터 없음")

    fig = go.Figure(go.Pie(
        labels=["긍정", "중립", "부정"],
        values=[pos, neu, neg],
        hole=0.55,
        marker_colors=[SENTIMENT_COLORS["긍정"], SENTIMENT_COLORS["중립"],
                       SENTIMENT_COLORS["부정"]],
        textinfo="label+percent"
    ))
    fig.update_layout(
        title=f"전체 감성 (총 {total:,}건)",
        margin=dict(t=60, b=20)
    )
    return fig


def _empty_fig(msg: str) -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(text=msg, xref="paper", yref="paper",
                       x=0.5, y=0.5, showarrow=False, font=dict(size=14))
    fig.update_layout(xaxis_visible=False, yaxis_visible=False)
    return fig
