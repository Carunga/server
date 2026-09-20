"""Tests for the SqueezePlay library menu handler."""

from __future__ import annotations

import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType, QueueOption
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.squeezelite.library_menu import (
    HOME_NODE,
    ROOT_NODE,
    SqueezeliteLibraryMenu,
)


def _make_menu(play_media: Any = None, hass: Any = None) -> SqueezeliteLibraryMenu:
    """Build a menu handler over a minimal fake provider."""
    mass = types.SimpleNamespace(
        player_queues=types.SimpleNamespace(play_media=play_media or AsyncMock()),
        get_provider=lambda _domain: hass,
        streams=types.SimpleNamespace(base_url="http://192.0.2.10:8098"),
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


def test_home_entries_place_ha_scripts_at_root() -> None:
    """The HA scripts entry is at the root; the library entries stay under My Music."""
    menu = _make_menu(hass=object())
    items = {item["id"]: item for item in menu._home_entries()}
    assert items["ma_playlists"]["node"] == HOME_NODE
    assert items["ma_artists"]["node"] == HOME_NODE
    assert items["ma_discover"]["node"] == HOME_NODE
    assert items["ma_discover"]["text"] == "Discover"
    assert items["ma_ha_scripts"]["node"] == ROOT_NODE
    assert items["ma_ha_scripts"]["weight"] == 15
    assert items["ma_ha_scripts"]["icon"] == "http://192.0.2.10:8098/slimproto/ha_icon.png"


def test_home_entries_omit_ha_scripts_without_hass() -> None:
    """Without the hass plugin the HA scripts entry is not offered."""
    menu = _make_menu()
    assert all(item["id"] != "ma_ha_scripts" for item in menu._home_entries())


async def test_ha_script_items_sorted_alphabetically() -> None:
    """Scripts are listed alphabetically by friendly name."""
    hass = types.SimpleNamespace(
        hass=types.SimpleNamespace(
            get_entity_registry=AsyncMock(
                return_value=[
                    {"entity_id": "script.zulu", "name": "Zulu", "labels": ["l1"]},
                    {"entity_id": "script.alpha", "name": "Alpha", "labels": ["l1"]},
                    {"entity_id": "script.mike", "name": "Mike", "labels": ["l1"]},
                ]
            ),
            send_command=AsyncMock(return_value=[{"label_id": "l1", "name": "squeeze"}]),
        ),
        get_states=AsyncMock(
            return_value=[
                {"entity_id": "script.zulu", "attributes": {"friendly_name": "Zulu"}},
                {"entity_id": "script.alpha", "attributes": {"friendly_name": "Alpha"}},
                {"entity_id": "script.mike", "attributes": {"friendly_name": "Mike"}},
            ]
        ),
    )
    menu = _make_menu(hass=hass)
    items = await menu._ha_script_items()
    assert [item["text"] for item in items] == ["Alpha", "Mike", "Zulu"]


async def test_recommendation_folders_are_navigable() -> None:
    """Discover rows open their items, and disabled rows are skipped."""
    menu = _make_menu()
    folder = types.SimpleNamespace(
        provider="tidal", item_id="row1", name="For You", enabled_by_default=True
    )
    hidden = types.SimpleNamespace(
        provider="tidal", item_id="row2", name="Hidden", enabled_by_default=False
    )
    menu.mass.music = types.SimpleNamespace(
        recommendations=types.SimpleNamespace(
            get_recommendations=AsyncMock(return_value=[folder, hidden])
        )
    )
    items = await menu._recommendation_folders()
    assert [item["text"] for item in items] == ["For You"]
    assert items[0]["node"] == HOME_NODE
    assert items[0]["actions"]["go"]["params"] == {
        "view": "recommendation_items",
        "provider": "tidal",
        "item_id": "row1",
    }


async def test_recommendation_items_render_playable() -> None:
    """Row items are playable, and folder items are expanded one level."""
    menu = _make_menu()
    track = types.SimpleNamespace(media_type=MediaType.TRACK, uri="tidal://track/1", name="A Track")
    nested = types.SimpleNamespace(
        media_type=MediaType.TRACK, uri="tidal://track/2", name="Nested Track"
    )
    folder = types.SimpleNamespace(media_type=MediaType.FOLDER, name="Folder", items=[nested])
    menu.mass.music = types.SimpleNamespace(
        recommendations=types.SimpleNamespace(
            get_recommendation_items=AsyncMock(return_value=[track, folder])
        )
    )
    items = await menu._recommendation_items("tidal", "row1")
    assert [item["text"] for item in items] == ["A Track", "Nested Track"]
    assert items[0]["actions"]["play"]["params"]["uri"] == "tidal://track/1"
