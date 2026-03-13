"""전처리 함수 - 노이즈 필터링 및 날짜 필터"""
import re
from datetime import datetime, timedelta


def is_noise(text: str, min_length: int = 10) -> bool:
    if not text or len(text.strip()) < min_length:
        return True
    # 반복 문자 제거 후 너무 짧으면 노이즈 (ㅋㅋㅋ, ㅠㅠㅠ 등)
    clean = re.sub(r"(.)\1{4,}", "", text)
    if len(clean.strip()) < 5:
        return True
    # 이모지/특수문자만 있는 경우
    emoji_pattern = re.compile(
        "[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF"
        "\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF]+",
        flags=re.UNICODE
    )
    if not emoji_pattern.sub("", text).strip():
        return True
    return False


def filter_noise(items: list, text_key: str, min_length: int = 10) -> list:
    return [item for item in items if not is_noise(str(item.get(text_key, "")), min_length)]


def get_date_range(period: str, date_from=None, date_to=None):
    """기간 설정에 따라 (date_from, date_to) 문자열 반환 (YYYY-MM-DD)"""
    today = datetime.now().date()
    if period == "최근 7일":
        return str(today - timedelta(days=7)), str(today)
    elif period == "최근 30일":
        return str(today - timedelta(days=30)), str(today)
    elif period == "직접 입력" and date_from and date_to:
        return str(date_from), str(date_to)
    return None, None  # 전체


def apply_date_filter(items: list, date_key: str, date_from: str = None, date_to: str = None) -> list:
    if not date_from and not date_to:
        return items
    filtered = []
    for item in items:
        d = str(item.get(date_key, ""))[:10]
        if not d:
            continue
        if date_from and d < date_from:
            continue
        if date_to and d > date_to:
            continue
        filtered.append(item)
    return filtered
