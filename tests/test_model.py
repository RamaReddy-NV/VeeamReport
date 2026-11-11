from datetime import timezone

import pytest

from vdr.model import Repo, parse_ts


def test_parse_ts_none():
    assert parse_ts(None) is None


def test_parse_ts_empty():
    assert parse_ts("") is None


def test_parse_ts_iso_zulu():
    dt = parse_ts("2021-06-05T01:04:29Z")

    assert dt.year == 2021
    assert dt.month == 6
    assert dt.day == 5
    assert dt.tzinfo == timezone.utc


def test_parse_ts_fractional_seconds():
    dt = parse_ts("2021-06-05T01:04:29.56Z")

    assert dt.microsecond == 560000


def test_parse_ts_truncates_fraction_to_microseconds():
    dt = parse_ts("2021-06-05T01:04:29.123456789Z")

    assert dt.microsecond == 123456


def test_parse_ts_naive_assumed_utc():
    dt = parse_ts("2021-06-05T01:04:29")

    assert dt.tzinfo == timezone.utc


def test_parse_ts_timezone_conversion():
    dt = parse_ts("2021-06-05T06:34:29+05:30")

    assert dt.hour == 1
    assert dt.minute == 4
    assert dt.second == 29
    assert dt.tzinfo == timezone.utc


def test_parse_ts_invalid():
    with pytest.raises(ValueError):
        parse_ts("invalid-date")


def test_parse_ts_invalid_month():
    with pytest.raises(ValueError):
        parse_ts("2024-99-01T00:00:00Z")


def test_repo_free_pct_normal():
    repo = Repo(
        server="vbr01",
        name="Repo1",
        capacity=100,
        free=25,
    )

    assert repo.free_pct == 25.0


def test_repo_free_pct_zero_capacity():
    repo = Repo(
        server="vbr01",
        name="Repo1",
        capacity=0,
        free=0,
    )

    assert repo.free_pct == 100.0


def test_repo_free_pct_full():
    repo = Repo(
        server="vbr01",
        name="Repo1",
        capacity=100,
        free=100,
    )

    assert repo.free_pct == 100.0


def test_repo_free_pct_half():
    repo = Repo(
        server="vbr01",
        name="Repo1",
        capacity=200,
        free=100,
    )

    assert repo.free_pct == 50.0