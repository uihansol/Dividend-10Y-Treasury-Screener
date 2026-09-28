"""새로고침 쿨다운: 장중 가격이 마감 뒤에도 종가처럼 남지 않는지 확인."""
from datetime import datetime

from pipeline.cache import KST, _within_cooldown


def _meta(updated: str) -> dict:
    return {"updated_at": updated, "last_error": None}


def test_within_cooldown_same_session():
    now = datetime(2026, 9, 28, 14, 5, tzinfo=KST)
    assert _within_cooldown(_meta("2026-09-28T14:00:00+09:00"), now) is True


def test_cooldown_expires():
    now = datetime(2026, 9, 28, 14, 11, tzinfo=KST)
    assert _within_cooldown(_meta("2026-09-28T14:00:00+09:00"), now) is False


def test_cooldown_bypassed_across_close_cutoff():
    """15:55 장중 수집 → 16:01 요청: 10분 안이어도 종가를 받으러 다시 수집한다."""
    now = datetime(2026, 9, 28, 16, 1, tzinfo=KST)
    assert _within_cooldown(_meta("2026-09-28T15:55:00+09:00"), now) is False


def test_cooldown_after_close_still_applies():
    now = datetime(2026, 9, 28, 16, 9, tzinfo=KST)
    assert _within_cooldown(_meta("2026-09-28T16:05:00+09:00"), now) is True


def test_cooldown_new_day():
    now = datetime(2026, 9, 29, 0, 1, tzinfo=KST)
    assert _within_cooldown(_meta("2026-09-28T23:58:00+09:00"), now) is False
