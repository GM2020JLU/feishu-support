from __future__ import annotations

import hashlib
import json
import uuid
from contextvars import ContextVar
from typing import Any


_replay_id_factory: ContextVar[Any] = ContextVar('replay_id_factory', default=None)


def new_id(prefix: str) -> str:
    factory = _replay_id_factory.get()
    if factory is not None:
        return factory(prefix)
    return f"{prefix}_{uuid.uuid4().hex}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def inbound_key(source: str, identity: str, external_id: str) -> str:
    # A Feishu IM message can arrive through both the bot event stream and the
    # user search poller. message_id is immutable across those two transports,
    # so the durable key must not encode the transport or identity.
    if source in {"feishu_bot_im", "feishu_user_poll"}:
        return f"feishu_im:message:{external_id}"
    return f"{source}:{identity}:{external_id}"
