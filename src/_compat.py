"""Compatibility shims that MUST run before the `apify` SDK is imported."""

from __future__ import annotations


def patch_meta_origin() -> None:
    """Teach the pinned apify SDK about run origins it predates (e.g. 'MCP').

    apify 2.7.3's charging manager validates every run's ``meta.origin`` against a
    strict ``MetaOrigin`` enum during ``Actor.init()`` (a module-level
    ``TypeAdapter(ActorRun | None)`` in ``apify._charging``). The platform now
    emits newer origins like ``'MCP'`` (set on MCP-triggered runs) that this SDK
    version doesn't know, so init() raises a pydantic ``ValidationError`` and the
    Actor dies on startup — BEFORE any of our code runs. This bit only the MCP
    path; CLI/API/WEB origins are in the old enum and worked fine.

    We inject the missing members into the enum's internal maps. Must be called
    BEFORE ``import apify`` so the run_validator TypeAdapter compiles with them
    present (pydantic-core bakes the allowed value set in at TypeAdapter build).

    The durable fix is upgrading to apify 3.x (which also drops the pydantic /
    browserforge pins); this keeps the current pinned set alive in the meantime.
    """
    from apify_shared.consts import MetaOrigin

    # Origins the platform may emit that this SDK pin doesn't know about yet.
    for name in ("MCP",):
        if name in MetaOrigin._value2member_map_:
            continue
        member = str.__new__(MetaOrigin, name)
        member._name_ = name
        member._value_ = name
        MetaOrigin._member_map_[name] = member
        MetaOrigin._value2member_map_[name] = member
        MetaOrigin._member_names_.append(name)
