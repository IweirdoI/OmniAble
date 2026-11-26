"""数据保留边界契约（TASK-034 / Issue #42）。

划清「流程/插件运行数据」与「长期记忆」的边界，让验收项变成可执行检查：

- ``RETENTION_POLICY``：按数据类别列出归属（per_user/workspace）、保存位置、
  保留期限、遗忘传播与是否可摄入长期记忆。是 docs/DATA_RETENTION.md 的
  机器可读事实来源，两者同步更新。
- ``long_term_ingestible(kind)``：某类数据是否允许进入长期记忆；录音、表格
  行、源码、任务日志等流程/插件数据默认 **False**（Issue #42：日志只留
  最小结果，录音/表格值/源码默认不被摄入长期记忆）。
- ``forget_propagation_targets()``：遗忘一条记忆后必须同步清理的存储清单
  （敏感索引、短/中/长期层、向量后端、摄取暂存、偏好版本索引、会话引用）。
  orchestrator.forget 与 api /forget 已按此清单接线；本模块供回归测试
  与未来插件任务对照，防止新增存储后遗漏传播。

本模块只声明契约与查询，不承载运行时状态；新增数据类别时先改这里、
再写回归，最后同步文档。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class OwnerScope(str, Enum):
    """数据归属：按用户隔离，或属于工作区/流程本身。"""

    PER_USER = "per_user"        # 两名用户之间必须互相不可见
    WORKSPACE = "workspace"      # 工作区级共享（如代码索引、模板库）


@dataclass(frozen=True)
class RetentionPolicy:
    """一个数据类别的保留边界。

    Attributes:
        kind: 稳定类别标识（英文 snake_case，供测试与未来插件任务引用）。
        label: 中文显示名（与文档表格一致）。
        owner_scope: 归属（per_user / workspace）。
        location: 保存位置（相对项目根；``None`` 表示当前尚无实现，
            由未来的插件/运行记录任务落地）。
        retention: 保留期限语义（session / persistent / until_forget）。
        forget_propagation: 遗忘/删除是否必须同步清理本类别。
        long_term_ingestible: 默认是否允许写入长期记忆。流程/插件数据
            （录音、模板、表格行、任务日志、代码索引）一律 False。
    """

    kind: str
    label: str
    owner_scope: OwnerScope
    location: str | None
    retention: str
    forget_propagation: bool
    long_term_ingestible: bool


RETENTION_POLICY: tuple[RetentionPolicy, ...] = (
    # ---- 长期记忆侧（现有实现，全部按用户隔离、支持遗忘传播） ----
    RetentionPolicy(
        kind="long_term_memory", label="长期记忆（偏好/知识）",
        owner_scope=OwnerScope.PER_USER, location="向量后端（memory / milvus）",
        retention="until_forget", forget_propagation=True, long_term_ingestible=True,
    ),
    RetentionPolicy(
        kind="preference_profile", label="偏好画像",
        owner_scope=OwnerScope.PER_USER,
        location="向量后端投影；版本索引 data/preferences/<user_id>.json",
        retention="until_forget", forget_propagation=True, long_term_ingestible=True,
    ),
    RetentionPolicy(
        kind="sensitive_index", label="脱敏索引（加密原文）",
        owner_scope=OwnerScope.PER_USER, location="security_index.json",
        retention="until_forget", forget_propagation=True, long_term_ingestible=False,
    ),
    RetentionPolicy(
        kind="agent_session", label="Agent 会话与消息",
        owner_scope=OwnerScope.PER_USER, location="data/agent_sessions.db",
        retention="persistent（用户可删除）", forget_propagation=True,
        long_term_ingestible=False,
    ),
    RetentionPolicy(
        kind="ingestion_staging", label="摄取暂存审计",
        owner_scope=OwnerScope.PER_USER, location="data/ingestion_staging.db",
        retention="persistent（遗忘时清洗）", forget_propagation=True,
        long_term_ingestible=False,
    ),
    # ---- 流程/插件数据侧（未来任务落地；默认不进长期记忆） ----
    RetentionPolicy(
        kind="recording", label="操作录音/录屏",
        owner_scope=OwnerScope.PER_USER, location=None,
        retention="session（任务结束即清理，除非用户显式保存）",
        forget_propagation=True, long_term_ingestible=False,
    ),
    RetentionPolicy(
        kind="template", label="流程模板",
        owner_scope=OwnerScope.WORKSPACE, location=None,
        retention="persistent（显式保存，与执行授权分开存放）",
        forget_propagation=False, long_term_ingestible=False,
    ),
    RetentionPolicy(
        kind="table_row", label="批量录入的表格行值",
        owner_scope=OwnerScope.PER_USER, location=None,
        retention="session（运行明细随任务清理）",
        forget_propagation=True, long_term_ingestible=False,
    ),
    RetentionPolicy(
        kind="task_log", label="任务执行日志",
        owner_scope=OwnerScope.PER_USER, location="data/agent_runs.db",
        retention="persistent（只留最小结果：状态/耗时/错误码，不留正文）",
        forget_propagation=False, long_term_ingestible=False,
    ),
    RetentionPolicy(
        kind="code_index", label="代码解析索引",
        owner_scope=OwnerScope.WORKSPACE, location=None,
        retention="persistent（可重建）", forget_propagation=False,
        long_term_ingestible=False,
    ),
    RetentionPolicy(
        kind="execution_authorization", label="执行授权记录",
        owner_scope=OwnerScope.PER_USER, location=None,
        retention="persistent（独立于模板存放；模板不含授权）",
        forget_propagation=False, long_term_ingestible=False,
    ),
)

_POLICY_BY_KIND: dict[str, RetentionPolicy] = {p.kind: p for p in RETENTION_POLICY}

# 流程/插件数据在 ingestion 的 source 白名单之外：DataSource 目前仅允许
# conversation / document / tool_result / user_behavior / manual_config 五类，
# 录音、表格行、源码、模板等以未登记 source 传入即被拒（invalid_source）。
# 未来插件任务若需新来源，必须先在此处与 DataSource 枚举同步评审。
PLUGIN_DATA_KINDS: frozenset[str] = frozenset({
    "recording", "template", "table_row", "task_log", "code_index",
})

# 遗忘一条记忆后必须同步的存储目标（orchestrator.forget 的接线清单）。
FORGET_PROPAGATION_TARGETS: tuple[str, ...] = (
    "sensitive_index",        # security.delete_sensitive_entries
    "short_term_layer",       # layer_manager.delete_short
    "mid_term_layer",         # layer_manager.delete_mid
    "long_term_layer",        # layer_manager.delete_long
    "vector_backend",         # milvus delete_memory
    "ingestion_staging",      # staging.forget_memory（事件清洗/原始删除）
    "preference_version_index",  # _sync_pref_index_delete
    "agent_session_references",  # session_store.forget_memories（墓碑+抑制）
)


def get_policy(kind: str) -> RetentionPolicy | None:
    """按类别查询保留策略；未知类别返回 None。"""
    return _POLICY_BY_KIND.get(kind)


def long_term_ingestible(kind: str) -> bool:
    """某类数据默认是否允许写入长期记忆（流程/插件数据一律 False）。"""
    policy = _POLICY_BY_KIND.get(kind)
    return bool(policy and policy.long_term_ingestible)
