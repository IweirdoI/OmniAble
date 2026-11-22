"""Storage transport contract regressions; substitutes do not validate native SDK performance."""

import pytest

from src.core.orchestrator import MemoryOrchestrator
from src.knowledge.memory_service.milvus_client import MilvusClient, MemoryBackendUnavailable
from src.knowledge.memory_service.kylin_vector_client import KylinVectorClient, _SdkTransport


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


@pytest.mark.parametrize("total", [0, 100_005])
def test_kylin_count_uses_uncapped_id_projection_without_unsupported_aggregate(total):
    class Bridge:
        def call(self, op, **payload):
            assert op == "query"
            assert payload["output_fields"] == ["memory_id"]
            assert 'user_id == "alice"' in payload["filter"]
            assert "is_deleted == false" in payload["filter"]
            return [{"memory_id": str(i)} for i in range(total)]

    client = object.__new__(KylinVectorClient)
    client.client = _SdkTransport(Bridge())
    client.bridge = client.client.bridge
    client.collection = "test"
    assert client.count({"user_id": "alice"}) == total


def test_milvus_count_does_not_turn_invalid_aggregate_into_zero():
    class Transport:
        def query(self, **kwargs):
            return [{"unexpected": 12}]

    client = object.__new__(MilvusClient)
    client.client = Transport()
    client.collection = "test"
    with pytest.raises(MemoryBackendUnavailable, match="count"):
        client.count({"user_id": "alice"})
