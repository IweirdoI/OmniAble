"""已保存长期记忆分页浏览 API 的正确性回归（TASK-014 / Issue #13）.

Module: testing | Requirement: (7) 量化评测

API 部分验收口径：
  - 用户隔离：列表 user_id 强制注入；详情跨用户一律 404（不泄露存在性）
  - 稳定排序/分页：跨页无重复漏项（本地后端以插入序等价创建序精确保证）
  - 类型/来源/场景筛选；默认只看最新（is_latest），include_superseded 展示版本链
  - items 无 embedding；content 为入库时已脱敏文本
  - 删除后刷新立即一致；匹配总数超浏览上限报 400（不静默漏数）

说明：InMemory 后端验证契约与接线；pymilvus / 麒麟 transport 的分页稳定性
以真实后端实测为准（TASK-007 环境），不据本地推断。
"""

from __future__ import annotations

import tempfile

import pytest
from fastapi.testclient import TestClient

from api.server import app, orchestrator
from src.knowledge.memory_service.in_memory_client import InMemoryMilvusClient

client = TestClient(app)


def setup_function() -> None:
    orchestrator._milvus = InMemoryMilvusClient()


def _insert(
    memory_id: str,
    *,
    user_id: str,
    memory_type: str = "tool_call",
    source: str = "manual",
    scene_id: str | None = None,
    is_latest: bool = True,
    version: int = 1,
    content: str = "记忆正文",
) -> None:
    orchestrator._milvus.insert(
        memory_id=memory_id,
        content=content,
        embedding=[0.0] * 48,
        metadata={
            "user_id": user_id,
            "memory_type": memory_type,
            "source": source,
            "scene_id": scene_id,
            "is_latest": is_latest,
            "version": version,
        },
    )


def _fetch_all_pages(user_id: str, *, limit: int = 10, **params) -> tuple[list[dict], int]:
    items: list[dict] = []
    offset = 0
    total = None
    while True:
        response = client.get(
            f"/memories/{user_id}",
            params={"offset": offset, "limit": limit, **params},
        )
        assert response.status_code == 200
        body = response.json()
        total = body["total"]
        items.extend(body["items"])
        offset += body["returned"]
        if body["returned"] == 0 or offset >= total:
            break
    return items, total


def test_browse_pages_cover_all_without_duplicates() -> None:
    """跨页无重复漏项：页序遍历并集 == 全集；顺序为创建时间倒序。"""
    ids = [f"page-{i:02d}" for i in range(25)]
    for memory_id in ids:
        _insert(memory_id, user_id="page_user", content=f"正文 {memory_id}")

    items, total = _fetch_all_pages("page_user", limit=10)

    assert total == 25
    got_ids = [item["memory_id"] for item in items]
    assert len(got_ids) == len(set(got_ids)), "跨页不得出现重复条目"
    assert set(got_ids) == set(ids), "跨页不得漏项"
    assert got_ids == list(reversed(ids)), "排序应为创建时间倒序（新→旧）"


def test_browse_filters_by_type_source_scene() -> None:
    """类型（含 knowledge 聚合）/来源/场景筛选正确，非法值报 400。"""
    _insert("f-pref", user_id="filter_user", memory_type="preference", source="chat")
    _insert("f-tool-1", user_id="filter_user", memory_type="tool_call", source="manual")
    _insert("f-tool-2", user_id="filter_user", memory_type="tool_call", source="chat")
    _insert("f-doc", user_id="filter_user", memory_type="document_chunk",
            source="manual", scene_id="office")

    knowledge = client.get("/memories/filter_user", params={"memory_type": "knowledge"})
    assert knowledge.status_code == 200
    assert {item["memory_id"] for item in knowledge.json()["items"]} == {"f-tool-1", "f-tool-2", "f-doc"}

    tool = client.get("/memories/filter_user", params={"memory_type": "tool_call"})
    assert tool.json()["total"] == 2
    pref = client.get("/memories/filter_user", params={"memory_type": "preference"})
    assert pref.json()["total"] == 1

    chat = client.get("/memories/filter_user", params={"source": "chat"})
    assert chat.json()["total"] == 2
    pdf = client.get("/memories/filter_user", params={"source": "pdf"})
    assert pdf.status_code == 200 and pdf.json()["total"] == 0

    office = client.get("/memories/filter_user", params={"scene_id": "office"})
    assert office.json()["total"] == 1
    lab = client.get("/memories/filter_user", params={"scene_id": "lab"})
    assert lab.json()["total"] == 0

    bad_type = client.get("/memories/filter_user", params={"memory_type": "nope"})
    assert bad_type.status_code == 400
    bad_source = client.get("/memories/filter_user", params={"source": "nope"})
    assert bad_source.status_code == 400


def test_browse_default_latest_only_and_include_superseded() -> None:
    """默认只看最新（is_latest=true）；include_superseded 展示版本链。"""
    _insert("ver-v1", user_id="ver_user", memory_type="document_chunk",
            is_latest=False, version=1, content="旧版事实")
    _insert("ver-v2", user_id="ver_user", memory_type="document_chunk",
            is_latest=True, version=2, content="新版事实")

    default_page = client.get("/memories/ver_user")
    assert default_page.json()["total"] == 1
    assert default_page.json()["items"][0]["memory_id"] == "ver-v2"
    assert default_page.json()["items"][0]["version"] == 2

    all_versions = client.get("/memories/ver_user", params={"include_superseded": "true"})
    assert all_versions.json()["total"] == 2
    assert {item["memory_id"] for item in all_versions.json()["items"]} == {"ver-v1", "ver-v2"}


def test_browse_items_have_no_embedding() -> None:
    """items 不得携带 embedding；content 为入库脱敏文本。"""
    _insert("emb-1", user_id="emb_user", content="这是入库时的脱敏正文")
    response = client.get("/memories/emb_user")
    item = response.json()["items"][0]
    assert "embedding" not in item
    assert item["content"] == "这是入库时的脱敏正文"


def test_browse_after_delete_refreshes() -> None:
    """软删（遗忘路径同款删除）后，列表与总数立即一致。"""
    for memory_id in ("del-1", "del-2", "del-3"):
        _insert(memory_id, user_id="del_user")
    orchestrator._milvus.delete("del-2", soft=True)

    response = client.get("/memories/del_user")
    body = response.json()
    assert body["total"] == 2
    assert {item["memory_id"] for item in body["items"]} == {"del-1", "del-3"}


def test_browse_cross_user_isolation_and_detail_404() -> None:
    """列表按用户隔离；详情跨用户/不存在一律 404。"""
    _insert("iso-a", user_id="user_a", content="user_a 的记忆")
    _insert("iso-b", user_id="user_b", content="user_b 的记忆")

    only_a = client.get("/memories/user_a")
    assert only_a.json()["total"] == 1
    assert only_a.json()["items"][0]["memory_id"] == "iso-a"

    own = client.get("/memories/user_a/iso-a")
    assert own.status_code == 200
    assert own.json()["memory"]["content"] == "user_a 的记忆"

    leaked = client.get("/memories/user_b/iso-a")
    assert leaked.status_code == 404
    missing = client.get("/memories/user_a/does-not-exist")
    assert missing.status_code == 404


def test_browse_offset_beyond_total_returns_empty_page() -> None:
    """offset 超出总数：空页 + total 保留（UI 据此判断翻到底）。"""
    _insert("edge-1", user_id="edge_user")
    response = client.get("/memories/edge_user", params={"offset": 5, "limit": 10})
    body = response.json()
    assert body["total"] == 1
    assert body["returned"] == 0
    assert body["items"] == []


def test_browse_limit_offset_out_of_range_rejected() -> None:
    """limit<1 / offset<0 由 Query 校验拒绝（422）。"""
    assert client.get("/memories/any_user", params={"limit": 0}).status_code == 422
    assert client.get("/memories/any_user", params={"limit": 101}).status_code == 422
    assert client.get("/memories/any_user", params={"offset": -1}).status_code == 422


def test_browse_total_cap_returns_400(monkeypatch) -> None:
    """匹配总数超浏览上限：orchestrator 统一口径显式 400（不静默漏数）。"""
    orchestrator._milvus = InMemoryMilvusClient()

    def _capped_list_page(filters, *, offset=0, limit=20, output_fields=None):
        return {"total": 20_000, "offset": offset, "limit": limit,
                "returned": 0, "items": [], "order": "created_desc"}

    monkeypatch.setattr(orchestrator._milvus, "list_page", _capped_list_page)
    response = client.get("/memories/cap_user")
    assert response.status_code == 400
    assert "浏览上限" in response.json()["detail"]


def test_browse_invalid_params_rejected_at_orchestrator() -> None:
    """非法分页参数在适配器层即拒绝（ValueError）。"""
    with pytest.raises(ValueError):
        orchestrator._milvus.list_page({"user_id": "x"}, offset=-1, limit=10)
    with pytest.raises(ValueError):
        orchestrator._milvus.list_page({"user_id": "x"}, offset=0, limit=0)
