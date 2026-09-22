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
    learn_lead_ms,
    resolve_lead_ms,
    sendspin_audible_unix,
)

_VALID_MAC = "b8:27:eb:c2:4f:73"
_INVALID_ID = "not-a-mac"


def _make_bridge(player_id: str) -> SendspinSqueezeliteBridge:
    mass = SimpleNamespace(
        config=SimpleNamespace(get_raw_player_config_value=lambda _pid, _key, default=None: default)
    )
    provider = SimpleNamespace(mass=mass, logger=logging.getLogger("test"))
    player = SimpleNamespace(player_id=player_id, display_name=player_id)
    return SendspinSqueezeliteBridge(provider, player, sendspin_server=object())


def test_pcm_content_type_reports_the_bridge_format() -> None:
    """The content type carries the rate/channels/depth aioslimproto parses."""
    assert BRIDGE_PCM_CONTENT_TYPE == "audio/pcm;rate=44100;channels=2;bitrate=16"


def test_resolve_lead_ms_uses_less_lead_for_a_warm_transport() -> None:
    """A warm transport needs less lead, never below the floor."""
    assert resolve_lead_ms(1800, warm=False) == 1800
    assert resolve_lead_ms(1800, warm=True) == 1650
    assert resolve_lead_ms(1300, warm=True) == 1200


def test_learn_lead_ms_moves_toward_zero_start_error() -> None:
    """A negative start error (device early) increases the lead, and vice versa."""
    # device started 108 ms early -> add ~108 ms (full gain)
    assert learn_lead_ms(1750, -108.0, gain=1.0) == 1858
    # damped gain only closes 70% of the gap
    assert learn_lead_ms(1750, -108.0, gain=0.7) == 1826
    # device started 200 ms late -> reduce the lead
    assert learn_lead_ms(1800, 200.0, gain=1.0) == 1600
    # clamped to sane bounds
    assert learn_lead_ms(1250, 500.0, gain=1.0) == 1200
    assert learn_lead_ms(2500, -500.0, gain=1.0) == 2600


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


async def test_maybe_update_now_playing_pushes_once_per_track() -> None:
    """A track change pushes metadata once; identical media is not re-pushed."""
    bridge = _make_bridge(_VALID_MAC)
    bridge._bridge_client_id = "spb_x"
    media = SimpleNamespace(
        queue_item_id="q1",
        uri="library://track/1",
        title="Title",
        artist="Artist",
        album="Album",
        image_url="http://host/art.png",
        duration=1,
        stream_duration=None,
    )
    calls: list[dict[str, object]] = []

    class _Client:
        async def update_now_playing(self, metadata: dict[str, object]) -> None:
            calls.append(metadata)

    bridge.squeezelite_player = SimpleNamespace(
        player_id=_VALID_MAC, display_name="x", client=_Client()
    )
    bridge.mass = SimpleNamespace(
        players=SimpleNamespace(get_player=lambda _pid: SimpleNamespace(current_media=media)),
        create_task=asyncio.ensure_future,
    )

    bridge._maybe_update_now_playing()
    await asyncio.sleep(0)
    bridge._maybe_update_now_playing()
    await asyncio.sleep(0)

    assert len(calls) == 1
    assert calls[0]["title"] == "Title"
