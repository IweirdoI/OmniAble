"""Storage transport contract regressions; transport substitutes verify queries and projection."""

import pytest

from src.core.orchestrator import MemoryOrchestrator
from src.knowledge.memory_service.milvus_client import MilvusClient, MemoryBackendUnavailable


class StrictQueryTransport:
    """Enforce the explicit schema used by the real wrapper, including nested metadata."""

    def __init__(self):
        self.fields = None
        self.row = {
            "memory_id": "pref", "user_id": "alice", "memory_type": "preference",
            "preference_key": "habit.reply_language", "preference_value": "中文",
            "scene_id": "office", "confidence": 0.9, "version": 2, "is_latest": True,
            "metadata": {"scope": "scene", "n_observations": 7,
                         "confidence_source": "explicit"},
        }

    def query(self, *, collection_name, filter, output_fields, limit):
        assert set(output_fields) <= set(MilvusClient._OUTPUT_FIELDS)
        self.fields = output_fields
        return [{k: self.row[k] for k in output_fields if k in self.row}]


def test_profile_projection_uses_schema_fields_and_keeps_nested_metadata():
    client = object.__new__(MilvusClient)
    client.client = StrictQueryTransport()
    client.collection = "test"
    profile = MemoryOrchestrator(milvus=client).get_preference_profile("alice", "office")
    assert "content" not in client.client.fields
    assert "metadata" in client.client.fields
    feature = profile["features"][0]
    assert feature["value"] == "中文"
    assert feature["scope"] == "scene"
    assert feature["n_observations"] == 7
    assert feature["source"] == "explicit"


def test_milvus_count_does_not_turn_invalid_aggregate_into_zero():
    class Transport:
        def query(self, **kwargs):
            return [{"unexpected": 12}]

    client = object.__new__(MilvusClient)
    client.client = Transport()
    client.collection = "test"
    with pytest.raises(MemoryBackendUnavailable, match="count"):
        client.count({"user_id": "alice"})
