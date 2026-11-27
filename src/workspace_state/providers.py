"""Categories that capture and restore a recipe of their own, one descriptor each.

A descriptor names the stage and the snapshot key, and wraps the existing
capture and restore functions. Each wrapper looks its function up on the
defining module at call time, so a test patches that module.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any, Callable

from . import file_manager, social_apps


@dataclass(frozen=True)
class Provider:
    name: str  # login stage and CLI category
    snapshot_key: str
    label: str  # as capture problems name it
    # capture(shell, exact ids, scoped ids) -> recipe
    capture: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], dict[str, Any]]
    # restore(snapshot, args, reporter, switch options) -> count
    restore: Callable[[dict[str, Any], argparse.Namespace, Callable[..., None], dict[str, Any]], int]
    # Set for a category whose recipe can name cloud storage: startup defers it
    # until the drives are mounted. `noun` is one saved item, in the messages.
    needs_storage: Callable[[dict[str, Any] | None], bool] | None = None
    noun: str = ""


def _capture_social(shell, exact_ids, _scoped_ids):
    return social_apps.capture_social_apps(shell, **exact_ids)


def _restore_social(snapshot, args, reporter, switch_options):
    return social_apps.restore_social_apps(
        snapshot.get("social_apps"), dry_run=args.dry_run,
        no_place=args.no_place, workspace=args.workspace, reporter=reporter, **switch_options)


def _capture_file_manager(shell, exact_ids, scoped_ids):
    return file_manager.capture_file_manager(shell, **exact_ids, **scoped_ids)


def _restore_file_manager(snapshot, args, reporter, switch_options):
    return file_manager.restore_file_manager(
        snapshot.get("file_manager"), dry_run=args.dry_run,
        no_place=args.no_place, workspace=args.workspace,
        reporter=reporter, timeout=30, **switch_options,
        **({"switch_token": args.switch_token} if switch_options else {}))


def _file_manager_needs_storage(record):
    return file_manager.needs_storage(record)


PROVIDERS = (
    Provider("social-apps", "social_apps", "desktop app", _capture_social, _restore_social),
    Provider("file-manager", "file_manager", "file manager", _capture_file_manager, _restore_file_manager,
             _file_manager_needs_storage, "file manager window"),
)
BY_NAME = {provider.name: provider for provider in PROVIDERS}
