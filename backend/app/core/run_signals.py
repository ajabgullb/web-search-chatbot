"""Cross-replica cancellation signals over Redis pub/sub.

A run executes in exactly one API process. A cancel request (or a conversation
delete) can land on any other. The database already records the cancellation
durably, and the owning executor sees it when it next writes, but only after
its model call has finished and been billed. This channel tells the owning
process at once, so it cancels its task and the model stream stops.

Delivery is best effort by design. Pub/sub drops messages for a subscriber
that is briefly disconnected. The database remains the record, so a lost
signal costs promptness, never correctness.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

CHANNEL = "run-cancel"

# How long a subscriber waits before resubscribing after losing Redis.
_RECONNECT_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class CancelRequest:
    """Stop one run, or every run in a conversation."""

    reason: str
    run_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None

    def encode(self) -> str:
        return json.dumps(
            {
                "reason": self.reason,
                "run_id": str(self.run_id) if self.run_id else None,
                "conversation_id": str(self.conversation_id) if self.conversation_id else None,
            }
        )

    @classmethod
    def decode(cls, raw: bytes | str) -> CancelRequest | None:
        """None for anything malformed: a bad message is dropped, never fatal."""
        try:
            data = json.loads(raw)
            run_id = data.get("run_id")
            conversation_id = data.get("conversation_id")
            request = cls(
                reason=str(data["reason"]),
                run_id=uuid.UUID(run_id) if run_id else None,
                conversation_id=uuid.UUID(conversation_id) if conversation_id else None,
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            return None
        if request.run_id is None and request.conversation_id is None:
            return None
        return request


class RedisCancelSignal:
    def __init__(self, client: Redis) -> None:
        self._client = client
        # Set while subscribed. Lets startup and tests wait until a signal
        # sent now would be heard.
        self.subscribed = asyncio.Event()

    async def publish(self, request: CancelRequest) -> None:
        """Never raises: the database already holds the cancellation."""
        try:
            await self._client.publish(CHANNEL, request.encode())
        except RedisError:
            logger.warning("run_cancel_signal_failed", extra={"reason": request.reason})

    async def listen(self, handler: Callable[[CancelRequest], None]) -> None:
        """Deliver every signal to ``handler`` until cancelled.

        Survives Redis restarts by resubscribing. Polls with a timeout rather
        than blocking forever, because a blocking read would trip the client's
        socket timeout on a quiet channel.
        """
        while True:
            try:
                async with self._client.pubsub() as pubsub:
                    await pubsub.subscribe(CHANNEL)
                    self.subscribed.set()
                    while True:
                        message = await pubsub.get_message(
                            ignore_subscribe_messages=True, timeout=1.0
                        )
                        if message is None:
                            continue
                        request = CancelRequest.decode(message["data"])
                        if request is None:
                            logger.warning("run_cancel_signal_malformed")
                            continue
                        try:
                            handler(request)
                        except Exception:
                            # One bad delivery must not end the subscription.
                            logger.exception("run_cancel_signal_handler_failed")
            except RedisError:
                self.subscribed.clear()
                logger.warning("run_cancel_signal_disconnected")
                await asyncio.sleep(_RECONNECT_SECONDS)
