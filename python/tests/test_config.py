"""Tests for config/.env handling.

The governing rule: a bad value is never applied. A typo in .env must not be able to
hand out unlimited YouTube, and must not stop the watcher from starting either.
"""

from __future__ import annotations

from safehouse_ytbudget.config import Config, from_json, load, merge, parse_env, render, to_json


# --- .env parsing ------------------------------------------------------------


def test_parses_simple_assignment():
    values, warnings = parse_env("YB_LIMIT_MIN=180\n")
    assert values == {"limit_min": 180}
    assert warnings == []


def test_ignores_comments_and_blank_lines():
    values, warnings = parse_env("# a comment\n\n   \nYB_LIMIT_MIN=60\n")
    assert values == {"limit_min": 60}
    assert warnings == []


def test_strips_quotes_and_whitespace():
    values, _ = parse_env('  YB_MIN_THROUGHPUT_KBPS = "128"  \n')
    assert values == {"min_throughput_kbps": 128}


def test_rejects_non_integer():
    values, warnings = parse_env("YB_LIMIT_MIN=abc\n")
    assert values == {}
    assert "must be an integer" in warnings[0]


def test_rejects_negative():
    values, warnings = parse_env("YB_LIMIT_MIN=-5\n")
    assert values == {}
    assert "must be >= 0" in warnings[0]


def test_rejects_zero_sample_sec_but_allows_zero_limit():
    """A 0s sample would spin the loop. A 0m limit is a legitimate 'never allowed'."""
    values, warnings = parse_env("YB_SAMPLE_SEC=0\nYB_LIMIT_MIN=0\n")
    assert values == {"limit_min": 0}
    assert any("YB_SAMPLE_SEC" in w for w in warnings)


def test_reports_unknown_key():
    values, warnings = parse_env("YB_TYPO=1\n")
    assert values == {}
    assert "unknown key" in warnings[0]


def test_reports_malformed_line():
    values, warnings = parse_env("this has no equals sign\n")
    assert values == {}
    assert "not KEY=VALUE" in warnings[0]


def test_one_bad_line_does_not_discard_good_ones():
    values, warnings = parse_env("YB_LIMIT_MIN=abc\nYB_SAMPLE_SEC=30\n")
    assert values == {"sample_sec": 30}
    assert len(warnings) == 1


def test_warnings_carry_line_numbers():
    _, warnings = parse_env("# c\nYB_LIMIT_MIN=oops\n")
    assert "line 2" in warnings[0]


# --- config.json -------------------------------------------------------------


def test_default_daily_limit_is_one_hour():
    assert Config().limit_min == 60


def test_from_json_reads_known_fields():
    values, warnings = from_json('{"limit_min": 90, "sample_sec": 15}')
    assert values == {"limit_min": 90, "sample_sec": 15}
    assert warnings == []


def test_from_json_survives_corruption():
    values, warnings = from_json("{ not json")
    assert values == {}
    assert "not valid JSON" in warnings[0]


def test_from_json_rejects_bool_as_int():
    values, warnings = from_json('{"limit_min": true}')
    assert values == {}
    assert "must be an integer" in warnings[0]


def test_from_json_ignores_unknown_keys():
    values, warnings = from_json('{"limit_min": 90, "some_future_key": 7}')
    assert values == {"limit_min": 90}
    assert warnings == []


# --- precedence --------------------------------------------------------------


def test_env_overrides_live_config(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text('{"limit_min": 120, "sample_sec": 25}')
    env_path = tmp_path / ".env"
    env_path.write_text("YB_LIMIT_MIN=180\n")

    cfg, warnings = render(cfg_path, env_path)
    assert cfg.limit_min == 180  # .env wins
    assert cfg.sample_sec == 25  # untouched by .env, keeps its live value
    assert warnings == []


def test_missing_env_keeps_live_config(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text('{"limit_min": 120}')
    cfg, warnings = render(cfg_path, tmp_path / ".env")
    assert cfg.limit_min == 120
    assert any("not found" in w for w in warnings)


def test_corrupt_live_config_falls_back_to_defaults_plus_env(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{ corrupt")
    env_path = tmp_path / ".env"
    env_path.write_text("YB_LIMIT_MIN=180\n")

    cfg, warnings = render(cfg_path, env_path)
    assert cfg.limit_min == 180
    assert cfg.sample_sec == Config().sample_sec
    assert any("not valid JSON" in w for w in warnings)


def test_load_missing_file_uses_defaults(tmp_path):
    cfg, warnings = load(tmp_path / "nope.json")
    assert cfg == Config()
    assert any("not found" in w for w in warnings)


def test_roundtrip_json(tmp_path):
    cfg = Config(limit_min=45, sample_sec=10)
    p = tmp_path / "config.json"
    p.write_text(to_json(cfg))
    again, warnings = load(p)
    assert again == cfg
    assert warnings == []


def test_limit_sec_property():
    assert Config(limit_min=180).limit_sec == 10800


def test_merge_is_pure():
    base = Config()
    merged = merge(base, {"limit_min": 5})
    assert base.limit_min == Config().limit_min
    assert merged.limit_min == 5
