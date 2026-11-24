"""Opt-in local filesystem API, under the server's existing auth middleware."""
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from src.agent.workspace import WorkspaceError, get_workspace, workspace_enabled

router = APIRouter(prefix='/workspace', tags=['workspace'])


def enabled():
    if not workspace_enabled():
        raise WorkspaceError('工作区未启用；请在服务端设置 AGENT_ENABLE_WORKSPACE=1 并指定 AGENT_EXEC_CWD。', 403)


def agent_tools_enabled() -> bool:
    """与模型侧 ChatModel 相同的工具主开关（AGENT_ENABLE_TOOLS）。

    面板按钮与模型共用授权（Issue #29）：模型不可用的动作，本 API 同样拒绝。
    """
    return os.getenv('AGENT_ENABLE_TOOLS', '0') == '1'


def read_only_actions():
    """列出当前可直调的只读动作（模型同目录、同开关；变更类不列出）。"""
    from src.agent.actions import ActionContext, actions_available

    ctx = ActionContext(tools_enabled=agent_tools_enabled())
    return [action for action in actions_available(ctx)
            if not action.mutates and action.name != 'search_memory']


class FileVersion(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    revision: str | None = Field(..., pattern=r'^[a-f0-9]{64}$')


class FileSave(FileVersion):
    content: str = Field(max_length=65536)


@router.get('')
def info():
    return {'enabled': workspace_enabled(), 'name': get_workspace().root.name if workspace_enabled() else '',
            'max_bytes': 65536}


@router.get('/tree', dependencies=[Depends(enabled)])
def tree(path: str = '.', offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
    return get_workspace().listing(path, offset, limit)


@router.get('/file', dependencies=[Depends(enabled)])
def read(path: str):
    return get_workspace().read(path)


@router.put('/file', dependencies=[Depends(enabled)])
def save(req: FileSave):
    return get_workspace().write(req.path, req.content, req.revision)


@router.delete('/file', dependencies=[Depends(enabled)])
def delete(req: FileVersion):
    return get_workspace().delete(req.path, req.revision)


@router.get('/changes', dependencies=[Depends(enabled)])
def changes():
    return {'changes': get_workspace().changes()}


@router.get('/diff', dependencies=[Depends(enabled)])
def diff(path: str):
    return get_workspace().diff(path)


@router.post('/restore', dependencies=[Depends(enabled)])
def restore(req: FileVersion):
    return get_workspace().resolve_change(req.path, req.revision, True)


@router.post('/accept', dependencies=[Depends(enabled)])
def accept(req: FileVersion):
    return get_workspace().resolve_change(req.path, req.revision, False)


# ---------------------------------------------------------------------------
# 统一动作入口（TASK-021 / Issue #29）：面板按钮与模型共用目录与授权。
# 只开放只读动作；变更类动作需宿主会话与逐次确认（TASK-020），不在本入口。
# ---------------------------------------------------------------------------

class ActionCall(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


@router.get('/actions')
def list_actions():
    return {'enabled': agent_tools_enabled(),
            'actions': [{'name': a.name, 'mutates': a.mutates} for a in read_only_actions()]}


@router.post('/actions/{name}', dependencies=[Depends(enabled)])
def call_action(name: str, call: ActionCall):
    """与模型同一 `dispatch_action`：同一只读动作经任一入口返回等价结果。"""
    from src.agent.actions import ActionContext, actions_available, dispatch_action, get_action

    action = get_action(name)
    # 开关与模型侧完全一致：主开关 + exec/web 各自独立开关
    ctx = ActionContext(
        tools_enabled=agent_tools_enabled(),
        exec_enabled=os.getenv('AGENT_ENABLE_EXEC_TOOL', '0') == '1',
        web_enabled=os.getenv('AGENT_ENABLE_WEB_TOOL', '0') == '1',
    )
    if action is None:
        raise HTTPException(status_code=404, detail=f'动作不存在：{name}')
    if action.mutates or action.name in {'exec_terminal', 'web_fetch', 'search_memory'}:
        # 宿主专属动作（拒绝理由不随开关波动）：变更类需宿主会话与逐次
        # 确认（TASK-020）；exec/web 需宿主级许可；search_memory 需请求级
        # 记忆上下文。本机 API 不代发（Issue #29）。
        raise HTTPException(status_code=403, detail='该动作需要宿主会话与执行确认，不能经本机 API 直调。')
    if action not in actions_available(ctx):
        raise HTTPException(status_code=404, detail=f'动作未开启：{name}')
    ok, content, error_code = dispatch_action(name, call.arguments, ctx)
    if not ok:
        return JSONResponse(status_code=400, content={
            'ok': False, 'action': name, 'error_code': error_code, 'detail': content})
    return {'ok': True, 'action': name, 'result': content}
