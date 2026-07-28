"""Web service for Agent Liftoff."""

from __future__ import annotations


def create_app(*args, **kwargs):
    """Import lazily so service utility modules have no server side effects."""
    from wheatear.service.app import create_app as _create_app

    return _create_app(*args, **kwargs)


__all__ = ["create_app"]
