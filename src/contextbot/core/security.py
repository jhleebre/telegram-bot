"""Access control.

Input arrives only from the owner's own Saved Messages, so single-user security is inherent: the
client subscribes to the self peer and drops anything else. :func:`is_saved_messages` is the guard
that enforces this defensively.
"""

from __future__ import annotations


def is_saved_messages(peer_id: int | None, my_id: int | None) -> bool:
    """Return True only for messages in the owner's own Saved Messages chat.

    In Telegram, a user's Saved Messages is the private chat with themselves, so the peer/chat id
    equals the user's own id.
    """
    return peer_id is not None and my_id is not None and peer_id == my_id
