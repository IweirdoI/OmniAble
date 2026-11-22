"""Opt-in local filesystem API, under the server's existing auth middleware."""
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from src.agent.workspace import WorkspaceError, get_workspace, workspace_enabled

router = APIRouter(prefix='/workspace', tags=['workspace'])


def enabled():
    if not workspace_enabled():
        raise WorkspaceError('工作区未启用；请在服务端设置 AGENT_ENABLE_WORKSPACE=1 并指定 AGENT_EXEC_CWD。', 403)


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
