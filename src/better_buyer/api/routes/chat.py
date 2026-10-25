"""主交互与 SSE 事件流。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ..deps import get_container, get_current_user, user_from_query_token
from ..schemas import ChatMessageRequest
from ..sse import event_stream

router = APIRouter(tags=["chat"])


@router.post("/chat/sessions")
def create_session(request: Request, user: dict = Depends(get_current_user)) -> dict:
    container = get_container(request)
    session = container.main.create_session(user["id"])
    return {
        "session_id": session.id,
        "state": session.state,
        "greeting": container.main.greeting(),
        "categories": container.registry.summary(),
    }


@router.post("/chat/sessions/{session_id}/messages")
async def send_message(
    session_id: str,
    body: ChatMessageRequest,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    container = get_container(request)
    try:
        return await container.main.handle(session_id, body.text, user["id"])
    except KeyError:
        raise HTTPException(status_code=404, detail="会话不存在")


@router.get("/chat/sessions/{session_id}/messages")
def history(
    session_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    container = get_container(request)
    session = container.repos.sessions.get(session_id, user["id"])
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"session_id": session_id, "messages": container.repos.chat.history(session_id, limit)}


@router.get("/events")
async def events(
    request: Request,
    token: str = Query(default=""),
    cursor: int = Query(default=0, ge=0),
):
    container = get_container(request)
    user_from_query_token(request, token)
    keepalive = int((container.settings.web_config.get("sse") or {}).get("keepalive_seconds", 15))
    return StreamingResponse(
        event_stream(container.event_bus, cursor=cursor, keepalive=keepalive),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
