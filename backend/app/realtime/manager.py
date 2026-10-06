"""WebSocket connection registry and broadcast helpers.

Flask-SocketIO is configured with a Redis message queue, so any gunicorn
worker can emit to a socket held by another worker. Room naming is
deterministic (``user:<id>`` and ``conversation:<id>``) precisely so that it
works across processes; per-process state would silently drop messages under
horizontal scaling.
"""

from __future__ import annotations

from typing import Any

from flask_socketio import emit, join_room, leave_room

from ..utils.logging import get_logger, log_event
from ..utils.metrics import realtime_events_total

logger = get_logger("harmony.realtime")

#: The namespace every ``emit_*`` helper below addresses.
#:
#: Flask-SocketIO's module-level ``emit`` reads ``flask.request.namespace`` to
#: work out where to send, and falls back to ``flask.request.sid`` when no room
#: is given. Both attributes exist only inside a Socket.IO *event handler*.
#: Every caller of these helpers is an ordinary HTTP request handler - the one
#: that just created a post or a comment - so the lookups raised AttributeError
#: and nothing was ever broadcast.
#:
#: Naming the namespace explicitly skips the first lookup. The helpers below
#: always pass a room, so the second is never reached.
_HTTP_NAMESPACE = "/"

#: sid -> {"user_id", "username", "rooms"}
_connections: dict[str, dict[str, Any]] = {}


def user_room(user_id: int) -> str:
    return f"user:{user_id}"


def conversation_room(conversation_id: int) -> str:
    return f"conversation:{conversation_id}"


def register(sid: str, user: Any) -> dict[str, set[str]]:
    """Track a socket and put it in the user's personal room."""
    _connections[sid] = {"user_id": user.id, "username": user.username, "rooms": {user_room(user.id)}}
    join_room(user_room(user.id))
    log_event(logger, "DEBUG", "ws.connected", user_id=user.id, sid=sid[:8])
    return _connections[sid]


def unregister(sid: str) -> None:
    entry = _connections.pop(sid, None)
    if entry:
        log_event(logger, "DEBUG", "ws.disconnected", user_id=entry["user_id"], sid=sid[:8])


def add_to_conversation(sid: str, conversation_id: int) -> None:
    room = conversation_room(conversation_id)
    entry = _connections.get(sid)
    if entry is not None:
        entry["rooms"].add(room)
    join_room(room)


def remove_from_conversation(sid: str, conversation_id: int) -> None:
    room = conversation_room(conversation_id)
    entry = _connections.get(sid)
    if entry is not None:
        entry["rooms"].discard(room)
    leave_room(room)


def emit_to_user(user_id: int, event: str, payload: Any) -> None:
    realtime_events_total.labels(event=event).inc()
    emit(event, payload, to=user_room(user_id), namespace=_HTTP_NAMESPACE)


def emit_to_conversation(conversation: Any, event: str, payload: Any, *, exclude_sid: str | None = None) -> None:
    room = conversation_room(conversation.id if hasattr(conversation, "id") else int(conversation))
    realtime_events_total.labels(event=event).inc()
    kwargs: dict[str, Any] = {"to": room, "namespace": _HTTP_NAMESPACE}
    if exclude_sid:
        # `skip_sid`, not `include_self=False`. With include_self and no skip_sid
        # flask_socketio resolves "self" by reading `flask.request.sid`, which a
        # plain HTTP request does not have - the same failure as above, one
        # branch further on. The two are equivalent when the caller names the
        # socket it means, which is exactly what `exclude_sid` is.
        kwargs["skip_sid"] = exclude_sid
    emit(event, payload, **kwargs)


def emit_global(event: str, payload: Any) -> None:
    """Broadcast to every connected socket.

    Used only for low-frequency public events (a new post appearing in the
    global feed). A high-frequency event must never use this path — it would
    wake every idle connection and turn one busy user into a fleet-wide load.
    """
    realtime_events_total.labels(event=event).inc()
    # `broadcast=True` as well as the namespace: with no room to address,
    # flask_socketio falls back to `flask.request.sid` to reply to the
    # originator, which is the same missing-attribute problem one line further
    # down. Broadcasting to everyone is what this function is for.
    emit(event, payload, namespace=_HTTP_NAMESPACE, broadcast=True)


def online_user_ids() -> list[int]:
    return sorted({entry["user_id"] for entry in _connections.values()})


def is_online(user_id: int) -> bool:
    """Whether this account currently holds at least one socket.

    O(connections) rather than O(1), which is the wrong shape for something
    called once per user per response. It stays a scan because callers ask about
    a single account: building a set of every id for every avatar in a page of
    results would be the larger cost.
    """
    return any(entry["user_id"] == user_id for entry in _connections.values())


def connection_count() -> int:
    return len(_connections)


def clear_registry() -> None:
    """Test hook."""
    _connections.clear()


__all__ = [
    "add_to_conversation",
    "clear_registry",
    "connection_count",
    "conversation_room",
    "emit_global",
    "emit_to_conversation",
    "emit_to_user",
    "is_online",
    "online_user_ids",
    "register",
    "remove_from_conversation",
    "unregister",
    "user_room",
]
