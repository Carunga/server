"""Tests for the SqueezePlay library menu handler."""

from __future__ import annotations

import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import QueueOption
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.squeezelite.library_menu import (
    HOME_NODE,
    SqueezeliteLibraryMenu,
)


def _make_menu(play_media: Any = None) -> SqueezeliteLibraryMenu:
    """Build a menu handler over a minimal fake provider."""
    mass = types.SimpleNamespace(
        player_queues=types.SimpleNamespace(play_media=play_media or AsyncMock()),
    )
    provider = types.SimpleNamespace(mass=mass, logger=MagicMock())
    return SqueezeliteLibraryMenu(provider)


def _cmd(command: str, player_id: str = "aa:bb", **kwargs: Any) -> Any:
    """Build a SlimCLICommand-like object."""
    return types.SimpleNamespace(player_id=player_id, command=command, args=[], kwargs=kwargs)


async def test_playlistcontrol_swallows_empty_playback_error() -> None:
    """A failed/empty selection must not raise: the CLI request needs an answer."""
    play_media = AsyncMock(side_effect=MediaNotFoundError("No playable items found"))
    menu = _make_menu(play_media)
    await menu._handle_playlistcontrol(
        _cmd("playlistcontrol", uri="library://playlist/1", cmd="play")
    )
    play_media.assert_awaited_once()


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("play", QueueOption.PLAY), ("add", QueueOption.ADD), ("insert", QueueOption.NEXT)],
)
async def test_playlistcontrol_maps_modes(mode: str, expected: QueueOption) -> None:
    """The playlistcontrol mode maps to the matching queue option."""
    play_media = AsyncMock()
    menu = _make_menu(play_media)
    await menu._handle_playlistcontrol(
        _cmd("playlistcontrol", uri="library://playlist/1", cmd=mode)
    )
    assert play_media.call_args.kwargs["option"] == expected


async def test_browse_returns_placeholder_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty browse result yields a placeholder instead of an empty list."""
    menu = _make_menu()
    monkeypatch.setattr(menu, "_browse_items", AsyncMock(return_value=[]))
    result = await menu._handle_browse(_cmd("ma_browse", view="artists"))
    assert result["count"] == 1
    assert result["item_loop"] == [{"text": "No items found"}]


async def test_artist_items_are_navigable() -> None:
    """Artist rows carry an id/node and open their albums."""
    menu = _make_menu()
    artist = types.SimpleNamespace(item_id="1", name="An Artist")

    async def _iter_artists() -> Any:
        yield artist

    menu.mass.music = types.SimpleNamespace(
        artists=types.SimpleNamespace(iter_library_items=_iter_artists)
    )
    items = await menu._artist_items()
    assert items[0]["id"] == "artist-1"
    assert items[0]["node"] == HOME_NODE
    assert items[0]["actions"]["go"]["params"] == {"view": "albums", "artist_id": "1"}


async def test_album_items_fall_back_to_tracks() -> None:
    """An artist without library albums lists its library tracks instead."""
    menu = _make_menu()
    track = types.SimpleNamespace(name="A Track", uri="library://track/1")
    artists = types.SimpleNamespace(
        albums=AsyncMock(return_value=[]),
        tracks=AsyncMock(return_value=[track]),
    )
    menu.mass.music = types.SimpleNamespace(artists=artists)
    items = await menu._album_items("1")
    assert items[0]["text"] == "A Track"
    assert items[0]["actions"]["play"]["params"]["uri"] == "library://track/1"


def test_play_action_sets_next_window() -> None:
    """Play actions carry the LMS nextWindow so the UI leaves the loading state."""
    play = SqueezeliteLibraryMenu._play_action("library://playlist/1", "play")
    add = SqueezeliteLibraryMenu._play_action("library://playlist/1", "add")
    assert play["nextWindow"] == "nowPlaying"
    assert add["nextWindow"] == "refresh"
