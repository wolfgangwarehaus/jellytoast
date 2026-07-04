"""Parity guard: both concrete providers expose ONE call surface.

Any keyword a caller can pass on one backend must exist — same name, same
default, same kind — on the other, or provider-agnostic app code TypeErrors
on exactly one backend (the class of break the abstraction exists to
prevent; ``get_artists`` had already drifted once before this guard).

The guard walks every public callable declared on ``MediaProvider`` and
asserts the two implementations' signatures match the ABC's. Extra
provider-only PUBLIC methods are also flagged: app code reaching for them
would be a parity leak.
"""

import inspect

import pytest

from jellytoast.providers.base import MediaProvider
from jellytoast.providers.jellyfin import JellyfinProvider
from jellytoast.providers.subsonic import SubsonicProvider

CONCRETE = (JellyfinProvider, SubsonicProvider)


def _public_callables(cls) -> dict:
    out = {}
    for name, member in vars(cls).items():
        if name.startswith("_"):
            continue
        if isinstance(member, (classmethod, staticmethod)):
            out[name] = member.__func__
        elif inspect.isfunction(member):
            out[name] = member
    return out


BASE_SURFACE = _public_callables(MediaProvider)


@pytest.mark.parametrize("name", sorted(BASE_SURFACE))
def test_signatures_match_the_base_surface(name):
    base_sig = inspect.signature(BASE_SURFACE[name])
    for cls in CONCRETE:
        impl = inspect.getattr_static(cls, name)
        if isinstance(impl, (classmethod, staticmethod)):
            impl = impl.__func__
        sig = inspect.signature(impl)
        assert list(sig.parameters) == list(base_sig.parameters), (
            f"{cls.__name__}.{name} drifted from MediaProvider.{name}: "
            f"{sig} != {base_sig} — add the parameter to the ABC + BOTH "
            f"providers (parity guard)"
        )
        for pname, base_param in base_sig.parameters.items():
            got = sig.parameters[pname]
            assert got.default == base_param.default, (
                f"{cls.__name__}.{name}({pname}=...) default drifted: "
                f"{got.default!r} != {base_param.default!r}"
            )
            assert got.kind == base_param.kind, (
                f"{cls.__name__}.{name}({pname}) parameter kind drifted"
            )


def test_no_provider_only_public_methods():
    base_names = set(BASE_SURFACE)
    for cls in CONCRETE:
        extras = {
            n
            for n in _public_callables(cls)
            if n not in base_names and not hasattr(MediaProvider, n)
        }
        assert not extras, (
            f"{cls.__name__} grew public methods missing from MediaProvider: "
            f"{sorted(extras)} — declare them on the ABC (and implement on "
            f"the sibling) or prefix them with _ (parity guard)"
        )
