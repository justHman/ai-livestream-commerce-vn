"""Engine selection and vocabulary for the avatar service."""

from __future__ import annotations

import pytest

from avatar.engines.avatarforcing import AvatarForcingEngine
from avatar.engines.base import AvatarEngine


def test_avatarforcing_requires_model() -> None:
    with pytest.raises(ValueError, match="requires AVATAR_MODEL"):
        AvatarForcingEngine.from_config({})


def test_avatarforcing_from_config() -> None:
    engine = AvatarForcingEngine.from_config({"model": "some-model"})
    assert engine.name == "avatarforcing"
    assert isinstance(engine, AvatarEngine)


def test_avatarforcing_session_lifecycle() -> None:
    engine = AvatarForcingEngine.from_config({"model": "m"})
    from avatar.engines.base import StartOptions

    result = engine.start(StartOptions(avatar_id="a1"))
    assert result.session_id == "av-a1"
    assert engine.session_status("av-a1") == "active"
    engine.interrupt("av-a1")
    assert engine.session_status("av-a1") == "interrupted"
    engine.stop("av-a1")
    with pytest.raises(KeyError):
        engine.session_status("av-a1")


def test_avatarforcing_unknown_session() -> None:
    engine = AvatarForcingEngine.from_config({"model": "m"})
    with pytest.raises(KeyError):
        engine.session_status("missing")