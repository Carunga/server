"""Tests for the Squeezelite Sendspin bridge audio path."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from music_assistant.providers.sendspin.constants import BRIDGE_PREFIX
from music_assistant.providers.squeezelite.sendspin_bridge import (
    BRIDGE_PCM_CONTENT_TYPE,
    SendspinSqueezeliteBridge,
    get_bridge_client_id,
)

_VALID_MAC = "b8:27:eb:c2:4f:73"
_INVALID_ID = "not-a-mac"


def _make_bridge(player_id: str) -> SendspinSqueezeliteBridge:
    provider = SimpleNamespace(mass=object(), logger=logging.getLogger("test"))
    player = SimpleNamespace(player_id=player_id)
    return SendspinSqueezeliteBridge(provider, player, sendspin_server=object())


def test_pcm_content_type_reports_the_bridge_format() -> None:
    """The content type carries the rate/channels/depth aioslimproto parses."""
    assert BRIDGE_PCM_CONTENT_TYPE == "audio/pcm;rate=44100;channels=2;bitrate=16"


def test_bridge_client_id_uses_the_mac_only_when_valid() -> None:
    """The bridge client id derives from the MAC and rejects non-MAC player ids."""
    bridge = _make_bridge(_VALID_MAC)
    client_id = get_bridge_client_id(bridge.squeezelite_player)
    assert client_id is not None
    assert client_id.startswith(BRIDGE_PREFIX)
    assert client_id == f"{BRIDGE_PREFIX}b827ebc24f73"

    assert get_bridge_client_id(_make_bridge(_INVALID_ID).squeezelite_player) is None


async def test_enqueue_pcm_drops_the_oldest_chunk_when_full() -> None:
    """Overflow drops the oldest chunk so the buffer cannot grow unbounded."""
    bridge = _make_bridge(_VALID_MAC)
    bridge._pcm_queue = asyncio.Queue(maxsize=2)

    bridge._enqueue_pcm(b"a")
    bridge._enqueue_pcm(b"b")
    bridge._enqueue_pcm(b"c")

    assert bridge._pcm_queue.qsize() == 2
    assert bridge._pcm_queue.get_nowait() == b"b"
    assert bridge._pcm_queue.get_nowait() == b"c"


async def test_signal_pcm_end_pushes_the_sentinel() -> None:
    """The end sentinel reaches the HTTP source even when the buffer is full."""
    bridge = _make_bridge(_VALID_MAC)
    bridge._pcm_queue = asyncio.Queue(maxsize=1)
    bridge._enqueue_pcm(b"a")

    bridge._signal_pcm_end()

    assert bridge._pcm_queue.get_nowait() is None
