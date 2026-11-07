"""Source registry — maps names to adapter classes."""

from __future__ import annotations

from .base import BaseSource
from .onthemarket import OnTheMarketSource
from .openrent import OpenRentSource
from .rightmove import RightmoveSource
from .spareroom import SpareRoomSource
from .unihomes import UnihomesSource
from .zoopla import ZooplaSource

_REGISTRY: dict[str, type[BaseSource]] = {
    RightmoveSource.name: RightmoveSource,
    ZooplaSource.name: ZooplaSource,
    OnTheMarketSource.name: OnTheMarketSource,
    OpenRentSource.name: OpenRentSource,
    SpareRoomSource.name: SpareRoomSource,
    UnihomesSource.name: UnihomesSource,
}


def available_sources() -> list[str]:
    return sorted(_REGISTRY)


def get_source(name: str) -> BaseSource:
    try:
        cls = _REGISTRY[name]
    except KeyError as exc:
        raise KeyError(f"Unknown source '{name}'. Available: {available_sources()}") from exc
    return cls()


def get_sources(names: list[str] | None = None) -> list[BaseSource]:
    if names is None:
        from ..config import get_settings

        names = get_settings().sources
    return [get_source(n) for n in names if n in _REGISTRY]
