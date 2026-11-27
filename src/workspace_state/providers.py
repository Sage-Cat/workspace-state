"""Categories that capture and restore a recipe of their own, one descriptor each.

A descriptor names the stage and the snapshot key, and wraps the existing
capture and restore functions. Each wrapper looks its function up on the
defining module at call time, so a test patches that module.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any, Callable

from . import social_apps


@dataclass(frozen=True)
class Provider:
    name: str  # login stage and CLI category
    snapshot_key: str
    label: str  # as capture problems name it
    # capture(shell, exact ids, scoped ids) -> recipe
    capture: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], dict[str, Any]]
    # restore(snapshot, args, reporter, switch options) -> count
    restore: Callable[[dict[str, Any], argparse.Namespace, Callable[..., None], dict[str, Any]], int]


def _capture_social(shell, exact_ids, _scoped_ids):
    return social_apps.capture_social_apps(shell, **exact_ids)


def _restore_social(snapshot, args, reporter, switch_options):
    return social_apps.restore_social_apps(
        snapshot.get("social_apps"), dry_run=args.dry_run,
        no_place=args.no_place, workspace=args.workspace, reporter=reporter, **switch_options)


PROVIDERS = (
    Provider("social-apps", "social_apps", "desktop app", _capture_social, _restore_social),
)
