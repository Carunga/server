"""Tests for mapping legacy SlimProto CLI transport commands to Music Assistant actions."""

from __future__ import annotations

from typing import Any

import pytest

from music_assistant.providers.squeezelite.cli_commands import map_transport_command


@pytest.mark.parametrize(
    ("command", "args", "expected"),
    [
        ("play", [], "play"),
        ("stop", [], "stop"),
        # LMS pause semantics: 1 = pause, 0 = resume, no/unknown argument = toggle
        ("pause", ["1"], "pause"),
        ("pause", ["0"], "play"),
        ("pause", [], "play_pause"),
        ("pause", ["2"], "play_pause"),
        ("mode", ["play"], "play"),
        ("mode", ["pause"], "pause"),
        ("mode", ["stop"], "stop"),
        ("mode", [], None),
        ("mode", ["bogus"], None),
        ("button", ["play"], "play"),
        ("button", ["pause"], "pause"),
        ("button", ["jump_fwd"], "next"),
        ("button", ["fwd"], "next"),
        ("button", ["jump_rew"], "previous"),
        ("button", ["rew"], "previous"),
        # non-transport commands are left to aioslimproto / the CLI event path
        ("button", ["preset_1.single"], None),
        ("status", ["-"], None),
        ("mixer", ["volume", 10], None),
        ("time", ["?"], None),
    ],
)
def test_map_transport_command(command: str, args: list[Any], expected: str | None) -> None:
    """Transport commands map to the expected action; others map to None."""
    assert map_transport_command(command, args) == expected
