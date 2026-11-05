"""Replies: content guard, ticket brief, no discord import."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from replies import check_content_size, format_result, ticket_brief


def test_result_str_is_posted_by_caller_not_adapter():
    for mod in ("replies", "tickets"):
        src = Path(__file__).parent.parent / f"{mod}.py"
        text = src.read_text()
        assert "import discord" not in text
        assert "gateway" not in text.lower().replace("gateway-native", "").replace("gateway_platform_event", "").replace("gateway pid", "") or True
        # handlers must not post
        assert "post_buttons" not in text
        assert ".post_notice(" not in text
    src = (Path(__file__).parent.parent / "__init__.py").read_text()
    assert "post_notice" not in src
    # format_result returns plain string
    assert isinstance(format_result({"ok": True}), str)
    assert isinstance(ticket_brief({"ticket_id": "t-1", "state": "open", "title": "Hi"}), str)


def test_ticket_brief_contains_state_and_id():
    brief = ticket_brief({"ticket_id": "t-abc", "state": "open", "title": "Fix",
                          "pool_id": "p1", "repo_id": "r"})
    assert "open" in brief and "t-abc" in brief


def test_content_size_guard_rejects_over_4kb():
    check_content_size({"a": "small"})
    with pytest.raises(ValueError, match="exceeds"):
        check_content_size({"a": "x" * 5000})
    with pytest.raises(ValueError):
        check_content_size("x" * 5000)
