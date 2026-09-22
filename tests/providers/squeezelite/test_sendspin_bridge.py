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
    sendspin_audible_unix,
)

_VALID_MAC = "b8:27:eb:c2:4f:73"
_INVALID_ID = "not-a-mac"


def _make_bridge(player_id: str) -> SendspinSqueezeliteBridge:
    provider = SimpleNamespace(mass=object(), logger=logging.getLogger("test"))
    player = SimpleNamespace(player_id=player_id, display_name=player_id)
    return SendspinSqueezeliteBridge(provider, player, sendspin_server=object())


def test_pcm_content_type_reports_the_bridge_format() -> None:
    """The content type carries the rate/channels/depth aioslimproto parses."""
    assert BRIDGE_PCM_CONTENT_TYPE == "audio/pcm;rate=44100;channels=2;bitrate=16"


def test_sendspin_audible_unix_transfers_only_the_future_offset() -> None:
    """The Sendspin instant maps to unix using the delta from now, not an epoch."""
    # first sample is 1.5 s in the future on the Sendspin clock
    assert sendspin_audible_unix(1_500_000, 0, 1000.0) == 1001.5
    # already-past instant maps to a unix time before now
    assert sendspin_audible_unix(500_000, 1_000_000, 1000.0) == 999.5


def test_build_play_metadata_uses_the_sendspin_media() -> None:
    """The now-playing metadata comes from the Sendspin player's current media."""
    bridge = _make_bridge(_VALID_MAC)
    bridge._bridge_client_id = "spb_x"
    media = SimpleNamespace(
        uri="library://track/1",
        title="Title",
        artist="Artist",
        album="Album",
        image_url="http://host/art.png",
        duration=123,
        stream_duration=None,
    )
    bridge.mass = SimpleNamespace(
        players=SimpleNamespace(get_player=lambda _pid: SimpleNamespace(current_media=media))
    )

    metadata = bridge._build_play_metadata()

    assert metadata == {
        "item_id": "library://track/1",
        "title": "Title",
        "artist": "Artist",
        "album": "Album",
        "image_url": "http://host/art.png",
        "duration": 123,
    }


def test_build_play_metadata_falls_back_to_a_placeholder() -> None:
    """Without Sendspin media the bridge still names the stream."""
    bridge = _make_bridge(_VALID_MAC)
    bridge._bridge_client_id = "spb_x"
    bridge.mass = SimpleNamespace(
        players=SimpleNamespace(get_player=lambda _pid: SimpleNamespace(current_media=None))
    )

    metadata = bridge._build_play_metadata()

    assert metadata["item_id"] == "sendspin-bridge"
    assert metadata["title"] == _VALID_MAC


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
