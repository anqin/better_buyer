"""会话状态机：显式状态与合法迁移，防止主智能体流程跳步。"""

from __future__ import annotations

from enum import Enum


class SessionState(str, Enum):
    IDLE = "Idle"
    COLLECTING = "Collecting"
    READY = "Ready"
    WATCHING = "Watching"
    DECIDING = "Deciding"
    SELECTING_OFFER = "SelectingOffer"
    PURCHASING = "Purchasing"
    COMPLETED = "Completed"
    EXITED = "Exited"


TRANSITIONS: dict[SessionState, set[SessionState]] = {
    SessionState.IDLE: {SessionState.COLLECTING, SessionState.READY, SessionState.EXITED},
    SessionState.COLLECTING: {
        SessionState.COLLECTING,
        SessionState.READY,
        SessionState.IDLE,
        SessionState.EXITED,
    },
    SessionState.READY: {
        SessionState.WATCHING,
        SessionState.DECIDING,
        SessionState.COLLECTING,
        SessionState.EXITED,
    },
    SessionState.WATCHING: {
        SessionState.DECIDING,
        SessionState.SELECTING_OFFER,
        SessionState.READY,
        SessionState.COMPLETED,
        SessionState.EXITED,
    },
    SessionState.DECIDING: {
        SessionState.SELECTING_OFFER,
        SessionState.WATCHING,
        SessionState.READY,
        SessionState.EXITED,
    },
    SessionState.SELECTING_OFFER: {
        SessionState.PURCHASING,
        SessionState.WATCHING,
        SessionState.DECIDING,
        SessionState.EXITED,
    },
    SessionState.PURCHASING: {
        SessionState.COMPLETED,
        SessionState.SELECTING_OFFER,
        SessionState.WATCHING,
        SessionState.EXITED,
    },
    SessionState.COMPLETED: {SessionState.COLLECTING, SessionState.READY, SessionState.EXITED},
    SessionState.EXITED: {SessionState.IDLE, SessionState.COLLECTING},
}


class InvalidTransition(RuntimeError):
    def __init__(self, source: str, target: str):
        super().__init__(f"非法状态迁移：{source} -> {target}")
        self.source = source
        self.target = target


def can_transition(source: str | SessionState, target: str | SessionState) -> bool:
    try:
        left = SessionState(source)
        right = SessionState(target)
    except ValueError:
        return False
    return right in TRANSITIONS.get(left, set())


def transition(session: object, target: str | SessionState) -> str:
    """把 ChatSession.state 迁移到目标状态；非法迁移抛 InvalidTransition。"""
    current = getattr(session, "state", SessionState.IDLE.value)
    if not can_transition(current, target):
        raise InvalidTransition(str(current), str(target))
    value = SessionState(target).value
    setattr(session, "state", value)
    return value
