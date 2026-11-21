"""Knowledge conflict ordering is deterministic across clock resolutions."""

from datetime import datetime, timedelta

import pytest

from src.common.models import MemoryItem, MemoryLayer, MemoryType
from src.core.conflict_resolver import ConflictAction, ConflictResolver


@pytest.mark.parametrize("seconds", [-1, 0, 1])
@pytest.mark.parametrize(
    "old_source,new_source,can_replace",
    [
        ("conversation", "conversation", True),
        ("manual_config", "conversation", True),
        ("manual", "chat", True),
        ("conversation", "manual_config", True),
        ("manual_config", "behavior", False),
        ("manual_config", "tool_result", False),
        ("conversation", "behavior", False),
        ("conversation", "tool_result", False),
        ("chat", "tool_result", False),
    ],
)
def test_knowledge_orders_facts_without_weakening_observation_guard(
    seconds, old_source, new_source, can_replace
):
    timestamp = datetime(2026, 1, 1)
    old = MemoryItem(
        id="old", memory_type=MemoryType.KNOWLEDGE, layer=MemoryLayer.LONG,
        content="port 8080", user_id="ordering-test", version=3,
        updated_at=timestamp,
        metadata={"knowledge_key": "service.port", "source_type": old_source},
    )
    new = MemoryItem(
        id="new", memory_type=MemoryType.KNOWLEDGE, layer=MemoryLayer.LONG,
        content="port 9090", user_id="ordering-test",
        updated_at=timestamp + timedelta(seconds=seconds),
        metadata={"knowledge_key": "service.port", "source_type": new_source},
    )
    decision = ConflictResolver().resolve_knowledge(old, new)
    if seconds >= 0 and can_replace:
        assert decision.action == ConflictAction.REPLACE
        assert decision.result is new
        assert new.version == 4
    else:
        assert decision.action == ConflictAction.KEEP_OLD
        assert decision.result is old
        assert old.version == 3
        assert new.version == 1
        assert "conflict_action" not in new.metadata
