# 太空发射插件测试 —— Copyright (C) 2026 星河拓航工作室 (Galaxy Exploration Studio)
#
# 本文件是太空发射插件的一部分，以 GNU General Public License v3.0 许可发布。
# 完整条款见仓库根目录的 LICENSE 文件。

from datetime import datetime, timezone
from pathlib import Path

import pytest
from plugin.plugins.space_launch import (
    _coerce_limit,
    _describe_launch,
    _humanize_delta,
    _is_still_upcoming,
    _parse_iso,
    _summarize_launch,
)
from plugin.sdk.plugin import SdkError


def test_plugin_manifest_exists() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = root / "plugin.toml"
    assert manifest.is_file()
    text = manifest.read_text(encoding="utf-8")
    assert 'id = "space_launch"' in text
    assert 'entry = "plugin.plugins.space_launch:SpaceLaunchPlugin"' in text


def test_parse_iso_handles_z_suffix() -> None:
    parsed = _parse_iso("2026-09-17T01:00:00Z")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.astimezone(timezone.utc).hour == 1


def test_parse_iso_rejects_garbage() -> None:
    assert _parse_iso("") is None
    assert _parse_iso(None) is None
    assert _parse_iso("not-a-date") is None


def test_humanize_delta_future_and_past() -> None:
    assert _humanize_delta(90).startswith("还有")
    assert "1 分钟" in _humanize_delta(90)

    past = _humanize_delta(-7200)
    assert past.startswith("已过去")
    assert "2 小时" in past


def test_coerce_limit_bounds() -> None:
    assert _coerce_limit(None, 5) == 5
    assert _coerce_limit("", 5) == 5
    assert _coerce_limit(3, 5) == 3
    assert _coerce_limit("3", 5) == 3
    assert _coerce_limit(0, 5) == 1
    assert _coerce_limit(-1, 5) == 1
    assert _coerce_limit(999, 5) == 20

    with pytest.raises(SdkError):
        _coerce_limit("abc", 5)


def test_is_still_upcoming_filters_finished_and_past() -> None:
    assert _is_still_upcoming({"status_abbrev": "Go", "countdown_seconds": 3600})
    assert _is_still_upcoming({"status_abbrev": "TBD", "countdown_seconds": None})
    assert _is_still_upcoming({"status_abbrev": "In Flight", "countdown_seconds": -30})

    # 已经结束的任务，无论时间如何都要剔除
    assert not _is_still_upcoming({"status_abbrev": "Success", "countdown_seconds": -3600})
    assert not _is_still_upcoming({"status_abbrev": "Failure", "countdown_seconds": 60})

    # net 早已过去但状态没刷新的条目也要剔除
    assert not _is_still_upcoming({"status_abbrev": "Go", "countdown_seconds": -7200})


_SAMPLE = {
    "name": "Falcon 9 Block 5 | USSF-259",
    "net": "2099-09-17T01:00:00Z",
    "status": {"name": "Go for Launch", "abbrev": "Go"},
    "launch_service_provider": {"name": "SpaceX", "abbrev": "SpX"},
    "rocket": {"configuration": {"full_name": "Falcon 9 Block 5", "name": "Falcon 9"}},
    "mission": {
        "name": "USSF-259",
        "type": "Government/Top Secret",
        "description": "A classified mission.",
        "orbit": {"name": "Polar Orbit"},
    },
    "pad": {
        "name": "Space Launch Complex 4E",
        "location": {"name": "Vandenberg SFB, CA, USA"},
        "country": {"name": "United States of America"},
    },
    "net_precision": {"name": "Minute"},
    "webcast_live": False,
    "url": "https://ll.thespacedevs.com/2.3.0/launches/example/",
    "image": {"image_url": "https://example.invalid/rocket.jpg"},
}


def test_summarize_launch_extracts_fields() -> None:
    now = datetime(2099, 9, 17, 0, 0, 0, tzinfo=timezone.utc)
    item = _summarize_launch(_SAMPLE, include_description=True, now=now)

    assert item["name"] == "Falcon 9 Block 5 | USSF-259"
    assert item["provider"] == "SpaceX"
    assert item["rocket"] == "Falcon 9 Block 5"
    assert item["location"] == "Vandenberg SFB, CA, USA"
    assert item["orbit"] == "Polar Orbit"
    assert item["description"] == "A classified mission."
    assert item["countdown_seconds"] == 3600
    assert item["countdown"] == "还有 1 小时"
    assert item["net_local"]


def test_summarize_launch_without_description() -> None:
    now = datetime(2099, 9, 17, 0, 0, 0, tzinfo=timezone.utc)
    item = _summarize_launch(_SAMPLE, include_description=False, now=now)
    assert "description" not in item


def test_summarize_launch_tolerates_missing_net() -> None:
    now = datetime(2099, 9, 17, 0, 0, 0, tzinfo=timezone.utc)
    item = _summarize_launch({"name": "TBD"}, include_description=True, now=now)
    assert item["net_local"] == ""
    assert item["countdown"] == ""
    assert item["countdown_seconds"] is None


def test_describe_launch_mentions_key_facts() -> None:
    now = datetime(2099, 9, 17, 0, 0, 0, tzinfo=timezone.utc)
    item = _summarize_launch(_SAMPLE, include_description=False, now=now)
    text = _describe_launch(item)

    assert "Falcon 9 Block 5 | USSF-259" in text
    assert "SpaceX" in text
    assert "Vandenberg SFB, CA, USA" in text
    assert "Go for Launch" in text
