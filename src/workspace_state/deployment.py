"""Content-addressed desktop releases. Installation never activates applications."""
from __future__ import annotations

import sys
if sys.version_info < (3, 11):
    raise RuntimeError("workspace-state deployment requires Python 3.11 or later")

import argparse
import ast
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

try:
    from ._build_info import BUILD_REVISION
except ImportError:
    BUILD_REVISION = 'development'

SCHEMA_VERSION = 1
_EXCLUDED = {'.git', 'node_modules', '__pycache__', 'profiles', 'logs', 'snapshots', 'sessions', '.env'}


class ActivationDeferred(RuntimeError):
    """Activation is still pending because the current desktop must be preserved."""

    def __init__(self, pending: dict[str, Any]):
        self.pending = dict(pending)
        super().__init__('desktop activation deferred: ' + str(pending['error']))


def build_fingerprint() -> dict[str, Any]:
    """Import-time identity: a later current-symlink change cannot relabel this code."""
    return {'revision': BUILD_REVISION, 'source_identity_known': BUILD_REVISION != 'development'}


@dataclass(frozen=True)
class Locations:
    home: Path
    data: Path
    config: Path
    state: Path
    prefix: Path
    runtime: Path | None = None

    @classmethod
    def environment(cls):
        home = Path.home()
        return cls(home, Path(os.environ.get('XDG_DATA_HOME', home / '.local/share')),
                   Path(os.environ.get('XDG_CONFIG_HOME', home / '.config')),
                   Path(os.environ.get('XDG_STATE_HOME', home / '.local/state')), home / '.local',
                   Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')))

    @property
    def releases(self):
        return self.data / 'workspace-state/desktop-releases'

    def substitutions(self):
        return {key: str(getattr(self, key)) for key in ('home', 'data', 'config', 'state', 'prefix')}


def _json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    try:
        with temp.open('x') as stream:
            temp.chmod(0o600)
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _load(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _relative(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError(f'release path must be relative: {value}')
    return path


def _source_files(root: Path, patterns: list[str]) -> dict[str, Path]:
    selected = {}
    for pattern in patterns:
        _relative(pattern)
        for path in root.glob(pattern):
            relative = path.relative_to(root)
            if any(part in _EXCLUDED for part in relative.parts):
                continue
            if path.is_symlink():
                raise ValueError(f'refusing source symlink: {path}')
            if path.is_file():
                # A symlinked parent could point at unrelated private data.
                if any(parent.is_symlink() for parent in path.parents if parent != root and root in parent.parents):
                    raise ValueError(f'refusing source through symlink: {path}')
                selected[relative.as_posix()] = path
    return dict(sorted(selected.items()))


def _file_info(path: Path) -> dict[str, Any]:
    return {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'executable': bool(path.stat().st_mode & 0o111)}


def _git_revision(root: Path) -> str | None:
    try:
        value = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                               capture_output=True, text=True, timeout=2)
        return value.stdout.strip() if value.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def load_manifest(path: Path) -> dict:
    value = tomllib.loads(path.read_text())
    if value.get('schema_version') != SCHEMA_VERSION:
        raise ValueError('unsupported desktop release manifest')
    seen = set()
    for component in value.get('components', []):
        name = component.get('name', '')
        if not re.fullmatch(r'[a-z][a-z0-9-]*', name) or name in seen:
            raise ValueError('component names must be unique safe identifiers')
        seen.add(name)
        if not isinstance(component.get('files'), list) or not component['files']:
            raise ValueError(f'component {name} has no explicit file inventory')
    if not seen:
        raise ValueError('release has no components')
    return value


@contextlib.contextmanager
def _lock(locations: Locations):
    locations.releases.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (locations.releases / '.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def _inventory(manifest: dict, source_root: Path) -> tuple[dict, dict]:
    records, sources = {}, {}
    for component in manifest['components']:
        name = component['name']
        root = Path(component['source']).expanduser()
        root = root if root.is_absolute() else source_root / root
        if not root.is_dir():
            if component.get('optional'):
                continue
            raise ValueError(f'missing required component: {root}')
        if root.is_symlink():
            raise ValueError(f'refusing component source symlink: {root}')
        files = _source_files(root, component['files'])
        if not files:
            if component.get('optional'):
                continue
            raise ValueError(f'empty required component: {name}')
        file_info = {key: _file_info(path) for key, path in files.items()}
        records[name] = {'git_revision': _git_revision(root), 'content_digest': _digest(file_info),
                         'files': file_info, 'source': str(root.resolve())}
        sources[name] = files
    return records, sources


def stage(manifest_path: Path, source_root: Path, locations: Locations) -> dict:
    manifest = load_manifest(manifest_path)
    records, sources = _inventory(manifest, source_root)
    # The absolute checkout location is diagnostic, not part of release identity.
    identity = {'manifest': manifest, 'host_paths': locations.substitutions(), 'components': {name: {key: value for key, value in item.items() if key != 'source'}
                for name, item in records.items()}}
    revision = 'r-' + _digest(identity)[:24]
    destination = locations.releases / revision
    with _lock(locations):
        if destination.exists():
            return verify_release(destination)
        temporary = Path(tempfile.mkdtemp(prefix='.stage-', dir=locations.releases))
        try:
            for component in manifest['components']:
                name = component['name']
                if name not in sources:
                    continue
                root = temporary / 'components' / name
                for relative, source in sources[name].items():
                    target = root / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                    target.chmod(0o755 if records[name]['files'][relative]['executable'] else 0o644)
                    if _file_info(target) != records[name]['files'][relative]:
                        raise ValueError(f'source changed while staging: {source}')
                for launcher in component.get('launchers', []):
                    target = root / _relative(launcher['path'])
                    entrypoint = _relative(launcher['python'])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    depth = len(Path(launcher['path']).parts) - 1
                    target.write_text('#!/usr/bin/python3\nimport runpy\nfrom pathlib import Path\n' +
                                      f'runpy.run_path(str(Path(__file__).resolve().parents[{depth}] / {str(entrypoint)!r}), run_name="__main__")\n')
                    target.chmod(0o755)
                for render in component.get('renders', []):
                    content = (root / _relative(render['source'])).read_text()
                    for token, replacement in render.get('replacements', {}).items():
                        content = content.replace(token, replacement.format(**locations.substitutions()))
                    target = root / _relative(render['target'])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content)
                for stamp in component.get('stamps', []):
                    target = root / _relative(stamp['path'])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    formats = {'python': f'BUILD_REVISION = {revision!r}\n',
                               'esm': f'export const BUILD_REVISION = {revision!r};\n',
                               'chrome': f'globalThis.WSCTL_BUILD_REVISION = {revision!r};\n',
                               'json': json.dumps({'revision': revision}) + '\n'}
                    target.write_text(formats[stamp['format']])
            # Recheck the input inventory after copying to reject mixed generations.
            if _inventory(manifest, source_root)[0] != records:
                raise ValueError('source changed while staging; retry with a stable checkout')
            files = {path.relative_to(temporary).as_posix(): _file_info(path)
                     for path in sorted(temporary.rglob('*')) if path.is_file()}
            release = {'schema_version': SCHEMA_VERSION, 'revision': revision,
                       'manifest': manifest, 'components': records, 'identity': identity, 'files': files}
            _json(temporary / 'release.json', release)
            for path in temporary.rglob('*'):
                path.chmod(0o555 if path.is_dir() or path.stat().st_mode & 0o111 else 0o444)
            temporary.chmod(0o555)
            temporary.rename(destination)
            return release
        finally:
            if temporary.exists():
                for path in temporary.rglob('*'):
                    if path.is_dir(): path.chmod(0o755)
                temporary.chmod(0o755)
                shutil.rmtree(temporary)


def verify_release(path: Path) -> dict:
    release = _load(path / 'release.json')
    if not isinstance(release, dict) or release.get('schema_version') != SCHEMA_VERSION:
        raise ValueError('invalid staged release')
    if 'r-' + _digest(release.get('identity'))[:24] != release.get('revision') or release.get('identity', {}).get('manifest') != release.get('manifest'):
        raise ValueError('release identity changed')
    if path.name != release.get('revision'):
        raise ValueError('release directory/revision mismatch')
    actual = {item.relative_to(path).as_posix() for item in path.rglob('*') if item.is_file() and item.name != 'release.json'}
    if actual != set(release['files']):
        raise ValueError('release inventory changed')
    for relative, expected in release['files'].items():
        candidate = path / _relative(relative)
        if candidate.is_symlink() or _file_info(candidate) != expected:
            raise ValueError(f'release content changed: {relative}')
    return release


def _pointer(locations: Locations, name: str) -> Path | None:
    path = locations.releases / name
    if not path.exists():
        return None
    if not path.is_symlink():
        raise ValueError(f'{name} release pointer is not a symlink')
    target = path.resolve()
    if target.parent != locations.releases.resolve():
        raise ValueError(f'{name} pointer escapes release directory')
    return target


def _symlink(path: Path, target: str | Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    try:
        temporary.symlink_to(target)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _containers(release: dict, locations: Locations, profiles: tuple[str, ...]) -> set[Path]:
    result = set()
    for component in release['manifest']['components']:
        if component['name'] not in release['components'] or (component.get('profile') and component['profile'] not in profiles):
            continue
        for value in component.get('containers', []):
            path = Path(value.format(**locations.substitutions()))
            if not path.is_absolute() or '..' in path.parts or not any(path.is_relative_to(base) and path != base for base in (locations.home, locations.data, locations.config, locations.prefix)):
                raise ValueError('container escapes owned user roots')
            if not any(path.parent.resolve().is_relative_to(base.resolve()) for base in (locations.home, locations.data, locations.config, locations.prefix)):
                raise ValueError('container parent escapes owned user roots')
            if path.exists() and not path.is_symlink() and not path.is_dir():
                raise ValueError(f'container is not a directory: {path}')
            result.add(path)
    return result


def _bindings(release: dict, release_path: Path, locations: Locations, profiles: tuple[str, ...]) -> dict[str, str]:
    result = {}
    containers = _containers(release, locations, profiles)
    for component in release['manifest']['components']:
        if component['name'] not in release['components'] or (component.get('profile') and component['profile'] not in profiles):
            continue
        root = release_path / 'components' / component['name']
        for entry in component.get('install', []):
            pattern = _relative(entry['source'])
            candidates = [root] if str(pattern) == '.' else sorted(root.glob(str(pattern)))
            if not candidates:
                raise ValueError(f'install asset missing from release: {component["name"]}/{pattern}')
            for source in candidates:
                target = Path(entry['target'].format(**locations.substitutions(), name=source.name))
                if not target.is_absolute() or '..' in target.parts:
                    raise ValueError('install destination must be an absolute owned path')
                if not any(target.is_relative_to(base) and target != base for base in (locations.home, locations.data, locations.config, locations.prefix)):
                    raise ValueError(f'install destination escapes user roots: {target}')
                if not any(target.is_relative_to(container) for container in containers) and not any(target.parent.resolve().is_relative_to(base.resolve()) for base in (locations.home, locations.data, locations.config, locations.prefix)):
                    raise ValueError(f'install destination parent escapes user roots: {target}')
                value = str(locations.releases / 'current' / source.relative_to(release_path))
                if str(target) in result and result[str(target)] != value:
                    raise ValueError(f'conflicting install destination: {target}')
                result[str(target)] = value
    targets = [Path(value) for value in result]
    if any(left != right and left.is_relative_to(right) for left in targets for right in targets):
        raise ValueError('nested owned install paths are ambiguous')
    return result


def install(revision: str, locations: Locations, *, profiles: tuple[str, ...] = (), development: bool = False) -> dict:
    if not re.fullmatch(r'r-[0-9a-f]{24}', revision):
        raise ValueError('invalid release revision')
    with _lock(locations):
        release_path = locations.releases / revision
        release = verify_release(release_path)
        containers = _containers(release, locations, profiles)
        bindings = _bindings(release, release_path, locations, profiles)
        if development:
            for target, link in list(bindings.items()):
                relative = Path(link).relative_to(locations.releases / 'current/components')
                component = next(item for item in release['manifest']['components'] if item['name'] == relative.parts[0])
                source = Path(release['components'][relative.parts[0]]['source']).joinpath(*relative.parts[1:])
                if source.exists():
                    bindings[target] = str(source)
                elif relative.as_posix().split('/', 1)[1] in {stamp['path'] for stamp in component.get('stamps', [])}:
                    bindings.pop(target)
        old = _pointer(locations, 'current')
        old_previous = _pointer(locations, 'previous')
        state_path = locations.releases / 'installation.json'
        previous_install = _load(state_path, {})
        old_bindings = previous_install.get('bindings', {})
        originals = dict(previous_install.get('originals', {}))
        old_containers = previous_install.get('containers', {})
        container_originals = {str(path): old_containers.get(str(path)) for path in containers}
        # Refuse changes to a managed link after installation rather than erase edits.
        for target, expected in old_bindings.items():
            path = Path(target)
            if not path.is_symlink() or os.readlink(path) != expected:
                raise ValueError(f'installed path changed; preserved: {path}')
        transaction = locations.releases / 'backups' / uuid.uuid4().hex
        changes = []
        inventory_path = locations.config / 'workspace-state/alerts.d/owned-systems.toml'
        inventory_source = release_path / 'components/workspace-state/config/owned-systems.toml'
        inventory_update = None
        if inventory_source.is_file():
            if inventory_path.is_symlink():
                raise ValueError('preserving symlinked inventory; migrate its authoritative target separately')
            current_text = inventory_path.read_text() if inventory_path.exists() else None
            inventory_update = _migrated_inventory(current_text if current_text is not None else inventory_source.read_text())
            if inventory_update == current_text:
                inventory_update = None
        if (old == release_path and previous_install.get('bindings') == bindings
                and previous_install.get('profiles', []) == list(profiles) and inventory_update is None
                and previous_install.get('mode') == ('development' if development else 'release')
                and all(path.is_dir() and not path.is_symlink() for path in containers)):
            return previous_install
        try:
            for path in sorted(containers):
                if path.is_symlink():
                    backup = transaction / str(len(changes))
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    path.rename(backup)
                    changes.append((path, backup))
                    container_originals[str(path)] = str(backup)
                    path.mkdir(parents=True)
                elif not path.exists():
                    changes.append((path, None))
                    path.mkdir(parents=True)
            for target in sorted(set(bindings) | set(old_bindings)):
                path = Path(target)
                if path.is_symlink() and target in bindings and os.readlink(path) == bindings[target]:
                    continue
                backup = None
                if path.exists() or path.is_symlink():
                    backup = transaction / str(len(changes))
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    path.rename(backup)
                changes.append((path, backup))
                if target in bindings:
                    if target not in old_bindings and backup is not None:
                        originals[target] = str(backup)
                    _symlink(path, bindings[target])
                elif target in originals:
                    original = Path(originals.pop(target))
                    if original.is_symlink():
                        _symlink(path, os.readlink(original))
                    elif original.is_dir():
                        shutil.copytree(original, path, symlinks=True)
                    else:
                        shutil.copy2(original, path)
            for old_container, original_path in old_containers.items():
                path = Path(old_container)
                if path in containers or not original_path:
                    continue
                backup = transaction / str(len(changes))
                backup.parent.mkdir(parents=True, exist_ok=True)
                path.rename(backup)
                changes.append((path, backup))
                original = Path(original_path)
                if original.is_symlink():
                    _symlink(path, os.readlink(original))
                else:
                    shutil.copytree(original, path, symlinks=True)
            if inventory_update is not None:
                backup = None
                mode = 0o600
                if inventory_path.exists():
                    mode = stat.S_IMODE(inventory_path.stat().st_mode)
                    backup = transaction / str(len(changes))
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    inventory_path.rename(backup)
                changes.append((inventory_path, backup))
                inventory_path.parent.mkdir(parents=True, exist_ok=True)
                inventory_path.write_text(inventory_update)
                inventory_path.chmod(mode)
            # The final pointer switch publishes all new component code together.
            if old and old != release_path:
                _symlink(locations.releases / 'previous', old.name)
            _symlink(locations.releases / 'current', revision)
            installed = {'revision': revision, 'mode': 'development' if development else 'release', 'profiles': list(profiles), 'bindings': bindings,
                         'originals': originals, 'containers': container_originals, 'backup_directory': str(transaction), 'backups': {str(path): str(backup) for path, backup in changes if backup is not None}, 'activation': 'pending',
                         'previous': (old_previous.name if old_previous else None) if old == release_path else old.name if old else None}
            _json(state_path, installed)
        except BaseException:
            if old:
                _symlink(locations.releases / 'current', old.name)
            else:
                (locations.releases / 'current').unlink(missing_ok=True)
            if old_previous:
                _symlink(locations.releases / 'previous', old_previous.name)
            else:
                (locations.releases / 'previous').unlink(missing_ok=True)
            for path, backup in reversed(changes):
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
                if backup is not None:
                    backup.rename(path)
            raise
        return installed


@contextlib.contextmanager
def _activation_lock(locations: Locations):
    locations.releases.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (locations.releases / '.activation.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def _unit_quote(value: str | Path) -> str:
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def _atomic_text(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    try:
        temporary.write_text(text)
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _pending_record(path: Path) -> dict:
    details = path.lstat()
    if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid() or details.st_mode & 0o077 or details.st_size > 65536:
        raise ValueError('pending release receipt must be a private regular file')
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get('schema_version') != 1 or not re.fullmatch(r'r-[0-9a-f]{24}', str(value.get('revision', ''))):
        raise ValueError('invalid pending release receipt')
    return value


def schedule(revision: str, locations: Locations, *, profiles: tuple[str, ...] = (), reload_manager: bool = False) -> dict:
    """Prepare next-login activation. Never starts/restarts any service."""
    with _activation_lock(locations), _lock(locations):
        if not re.fullmatch(r'r-[0-9a-f]{24}', revision):
            raise ValueError('invalid release revision')
        directory = locations.releases / revision
        verify_release(directory)
        helper = directory / 'components/workspace-state/scripts/desktop-release'
        if not helper.is_file():
            raise ValueError('scheduled release lacks its immutable activation helper')
        receipt = locations.releases / 'pending-install.json'
        command = ' '.join(_unit_quote(value) for value in ('/usr/bin/python3', '-I', helper, 'apply-pending', '--receipt', receipt))
        unit = ('[Unit]\nDescription=Apply a staged desktop release before the next graphical login\n'
                'DefaultDependencies=no\nAfter=basic.target\n'
                'Before=graphical-session-pre.target org.gnome.Shell@wayland.service org.gnome.Shell@x11.service wsctl-gnome-session.service\n'
                'PartOf=graphical-session.target\n\n[Service]\nType=oneshot\n'
                f'ExecStart={command}\nTimeoutStartSec=60s\nUMask=0077\n')
        dropin = '[Unit]\nWants=wsctl-release-activate.service\nAfter=wsctl-release-activate.service\n'
        files = {locations.config / 'systemd/user/wsctl-release-activate.service': unit}
        for name in ('org.gnome.Shell@wayland.service', 'org.gnome.Shell@x11.service', 'wsctl-gnome-session.service'):
            files[locations.config / f'systemd/user/{name}.d/40-wsctl-release-activation.conf'] = dropin
        backups = locations.releases / 'schedule-backups' / uuid.uuid4().hex
        changed = []
        old_receipt = receipt.read_bytes() if receipt.exists() else None
        try:
            for path, content in files.items():
                prior = None
                if path.exists() or path.is_symlink():
                    prior = backups / str(len(changed))
                    prior.parent.mkdir(parents=True, exist_ok=True)
                    if path.is_symlink():
                        prior.symlink_to(os.readlink(path))
                    else:
                        shutil.copy2(path, prior)
                changed.append((path, prior))
                _atomic_text(path, content)
            value = {'schema_version': 1, 'revision': revision, 'profiles': list(profiles),
                     'state': 'scheduled', 'attempts': 0,
                     'locations': {key: str(getattr(locations, key)) if getattr(locations, key) is not None else None
                                   for key in ('home', 'data', 'config', 'state', 'prefix', 'runtime')},
                     'unit_backups': {str(path): str(prior) for path, prior in changed if prior is not None}}
            _json(receipt, value)
        except BaseException:
            for path, prior in reversed(changed):
                path.unlink(missing_ok=True)
                if prior is not None:
                    if prior.is_symlink(): _symlink(path, os.readlink(prior))
                    else: shutil.copy2(prior, path)
            if old_receipt is not None:
                _atomic_text(receipt, old_receipt.decode(), 0o600)
            else:
                receipt.unlink(missing_ok=True)
            raise
    if reload_manager:
        _reload_user_manager()
    return value


def _reload_user_manager() -> None:
    result = subprocess.run(['systemctl', '--user', 'daemon-reload'], capture_output=True, text=True, timeout=5)
    if result.returncode:
        raise RuntimeError('user-manager definition reload failed: ' + result.stderr.strip())


def _activation_blockers(locations: Locations) -> list[str]:
    blockers = []
    for unit in ('org.gnome.Shell@wayland.service', 'org.gnome.Shell@x11.service', 'wsctl-gnome-session.service'):
        try:
            result = subprocess.run(['systemctl', '--user', 'show', '--property=LoadState', '--property=ActiveState', '--property=MainPID', unit],
                                    capture_output=True, text=True, timeout=2)
            values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
            if values.get('LoadState') == 'not-found':
                continue
            if result.returncode or not values:
                blockers.append(f'cannot verify {unit}')
            elif int(values.get('MainPID', '0')) > 0:
                blockers.append(f'{unit} still has a running process')
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            blockers.append(str(error))
    try:
        shell = subprocess.run(['gdbus', 'call', '--session', '--dest', 'org.freedesktop.DBus',
                                '--object-path', '/org/freedesktop/DBus', '--method',
                                'org.freedesktop.DBus.NameHasOwner', 'org.gnome.Shell'],
                               capture_output=True, text=True, timeout=1)
        if shell.returncode or shell.stdout.strip() not in {'(false,)', '(true,)'}:
            blockers.append('cannot verify the Shell bus owner')
        elif shell.stdout.strip() == '(true,)':
            blockers.append('a GNOME Shell process still owns its session bus name')
    except (OSError, subprocess.TimeoutExpired) as error:
        blockers.append(str(error))
    # A receipt also protects a coordinator registered outside the expected unit.
    root = (locations.runtime or locations.state / 'runtime') / 'workspace-state'
    coordinator = _coordinator_state(root / 'coordinator-build.json')
    if coordinator.get('pid'):
        blockers.append('a registered coordinator process is still alive')
    return blockers


def apply_pending(locations: Locations, *, blocker_reader: Callable[[Locations], list[str]] = _activation_blockers, reload_manager: bool = False) -> dict:
    """Transactional pre-login application; failure leaves the old desktop usable."""
    receipt = locations.releases / 'pending-install.json'
    with _activation_lock(locations):
        if not receipt.exists():
            return {'state': 'none'}
        value = _pending_record(receipt)
        if value.get('state') == 'applied':
            return value
        expected = {key: str(getattr(locations, key)) if getattr(locations, key) is not None else None
                    for key in ('home', 'data', 'config', 'state', 'prefix', 'runtime')}
        if value.get('locations') != expected:
            raise ValueError('pending release belongs to different installation roots')
        blockers = blocker_reader(locations)
        if blockers:
            value.update(state='waiting', error='; '.join(blockers))
            _json(receipt, value)
            raise ActivationDeferred(value)
        value.update(state='applying', attempts=int(value.get('attempts', 0)) + 1)
        _json(receipt, value)
        try:
            current = _pointer(locations, 'current')
            if value.get('installed_revision') == value['revision'] and current and current.name == value['revision']:
                verify_release(current)
                installed = {'revision': current.name}
            else:
                installed = install(value['revision'], locations, profiles=tuple(value.get('profiles', [])))
        except Exception as error:
            value.update(state='failed', error=str(error))
            _json(receipt, value)
            raise
        value.update(state='installed-pending-reload', installed_revision=installed['revision'])
        _json(receipt, value)
        if reload_manager:
            try:
                _reload_user_manager()
            except Exception as error:
                value.update(error=str(error), failure_phase='manager-reload')
                _json(receipt, value)
                raise
        value.update(state='applied')
        value.pop('failure_phase', None)
        value.pop('error', None)
        _json(receipt, value)
        return value


def rollback(locations: Locations) -> dict:
    previous = _pointer(locations, 'previous')
    if previous is None:
        raise ValueError('no previous installed release')
    state = _load(locations.releases / 'installation.json', {})
    return install(previous.name, locations, profiles=tuple(state.get('profiles', [])))


def _migrated_inventory(original: str) -> str:
    tomllib.loads(original)
    blocks = re.split(r'(?=^\[\[sources\]\]\s*$)', original, flags=re.MULTILINE)
    changed = False
    old_uuid = 'input-source-popup-guard@sagecat.local'
    new_uuid = 'input-source-popup-guard-v2@sagecat.local'
    old_source = '~/.local/share/gnome-shell/extensions/' + old_uuid
    new_source = '~/.local/share/gnome-shell/extensions/' + new_uuid
    for index, block in enumerate(blocks):
        if not block.startswith('[[sources]]'):
            continue
        entry = tomllib.loads(block).get('sources', [{}])[0]
        if (entry.get('id') != 'input-source-popup-guard' or entry.get('kind') != 'gnome-extension'
                or entry.get('ownership') != 'first-party'
                or entry.get('uuid') not in {old_uuid, new_uuid}):
            continue
        for key, old, new in (('uuid', old_uuid, new_uuid), ('source_ref', old_source, new_source)):
            if entry.get(key) != old:
                continue
            pattern = r'(^\s*' + key + r'\s*=\s*)(["\'])' + re.escape(old) + r'\2'
            block, count = re.subn(pattern,
                lambda match: match[1] + match[2] + new + match[2], block, count=1, flags=re.MULTILINE)
            changed = changed or bool(count)
        blocks[index] = block
    if not changed:
        return original
    updated = ''.join(blocks)
    tomllib.loads(updated)
    return updated


def migrate_inventory(path: Path) -> bool:
    """Migrate known owned UUID/source values, retaining comments/custom paths."""
    if not path.exists():
        return False
    original = path.read_text()
    updated = _migrated_inventory(original)
    if updated == original:
        return False
    backup = path.with_name(path.name + '.before-input-guard-v2')
    if not backup.exists():
        shutil.copy2(path, backup)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    try:
        temporary.write_text(updated)
        temporary.chmod(stat.S_IMODE(path.stat().st_mode))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def _runtime_state(spec: dict) -> dict:
    # Runtime probes share an absolute deadline, including every companion endpoint.
    deadline = min(float(spec.get('_deadline', time.monotonic() + 2)), time.monotonic() + 2)
    def remaining():
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError('runtime diagnostic deadline exceeded')
        return left
    if spec.get('kind') == 'coordinator':
        return _coordinator_state(Path(spec['runtime_directory']) / 'coordinator-build.json')
    if spec.get('kind') in {'chrome', 'native-host', 'vscode'}:
        if spec['kind'] == 'vscode':
            from .vscode import _request
            directory = Path(spec.get('runtime_directory', str(Locations.environment().runtime / 'workspace-state'))) / 'vscode'
            paths = sorted(directory.glob('*.sock'))
            if len(paths) > 64:
                return {'unavailable': 'too many VS Code endpoints to verify within the diagnostic budget'}
            request = lambda path: _request(path, 'state', timeout=min(1, remaining()))
        else:
            from .browser import _request_path, _host_paths
            paths = _host_paths()
            if len(paths) > 32:
                return {'unavailable': 'too many Chrome endpoints to verify within the diagnostic budget'}
            request = lambda path: _request_path(path, 'ping', timeout=min(1, remaining()))
        values, errors = [], []
        for path in paths:
            try:
                remaining()
                value = request(path)
                if not isinstance(value, dict):
                    raise ValueError('invalid companion diagnostic response')
                values.append(value)
            except (OSError, ValueError, RuntimeError) as error:
                errors.append(str(error))
            if time.monotonic() >= deadline:
                errors.append('runtime diagnostic deadline exceeded')
                break
        if errors:
            return {'unavailable': '; '.join(errors)[:500], 'instances': len(values)}
        if spec.get('kind') == 'native-host':
            values = [{'build': value.get('native_host_build', {})} for value in values]
        return _combined_runtime(values)
    # No Eval, activation, or private Shell calls.
    result = subprocess.run(['gdbus', 'call', '--session', '--dest', spec['destination'],
                             '--object-path', spec['path'], '--method', spec['interface'] + '.GetState'],
                            capture_output=True, text=True, timeout=remaining())
    if result.returncode:
        return {'unavailable': result.stderr.strip()[:300]}
    value = ast.literal_eval(result.stdout.strip())
    state = json.loads(value[0])
    return state if isinstance(state, dict) else {'unavailable': 'invalid runtime state'}


def _process_start(pid: int) -> str:
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    return str(int(fields[19]))


def _boot_id() -> str:
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def record_coordinator_build(login_generation: str) -> dict:
    """Called by the coordinator itself; never infers another process's build."""
    root = Locations.environment().runtime / 'workspace-state'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    details = root.lstat()
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid() or details.st_mode & 0o077:
        raise ValueError('coordinator receipt directory is not private')
    value = {'schema_version': 1, 'pid': os.getpid(), 'start_tick': _process_start(os.getpid()),
             'boot_id': _boot_id(), 'login_generation': str(login_generation), 'build': build_fingerprint()}
    _json(root / 'coordinator-build.json', value)
    return value


def _coordinator_state(path: Path) -> dict:
    try:
        details = path.lstat()
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid() or details.st_mode & 0o077 or details.st_size > 32768:
            raise ValueError('coordinator receipt is not a private regular file')
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or not isinstance(value.get('build'), dict) or value.get('schema_version') != 1 or type(value.get('pid')) is not int or value['pid'] <= 1:
            raise ValueError('invalid coordinator receipt')
        if value.get('boot_id') != _boot_id() or value.get('start_tick') != _process_start(value['pid']):
            raise ValueError('coordinator receipt belongs to an expired process or boot')
        return {'build': value.get('build', {}), 'pid': value['pid'], 'login_generation': value.get('login_generation')}
    except (OSError, ValueError, IndexError) as error:
        return {'unavailable': str(error)}


def _combined_runtime(values: list[dict]) -> dict:
    if not values:
        return {'unavailable': 'no running companion endpoints'}
    revisions = {(value.get('build') or {}).get('revision') for value in values}
    protocols = {value.get('protocol_version', value.get('version')) for value in values}
    capabilities = set.intersection(*(set(value.get('capabilities', [])) for value in values))
    return {'build': {'revision': next(iter(revisions)) if len(revisions) == 1 else None},
            'protocol_version': next(iter(protocols)) if len(protocols) == 1 else None,
            'capabilities': sorted(capabilities), 'instances': len(values),
            'mixed_builds': len(revisions) > 1}


def _diagnostic_json(path: Path) -> dict:
    try:
        value = _load(path, {})
        return value if isinstance(value, dict) else {'_read_error': 'expected a JSON object'}
    except (OSError, ValueError) as error:
        return {'_read_error': str(error)}


def _chrome_registration(locations: Locations) -> list[dict]:
    expected = locations.data / 'workspace-state/chrome-extension'
    result = []
    for path in sorted((locations.config / 'google-chrome').glob('*/Preferences'))[:32]:
        try:
            if path.stat().st_size > 32 * 1024 * 1024:
                continue
            value = json.loads(path.read_text())
            entry = value.get('extensions', {}).get('settings', {}).get('gnccboicpdhhhpdcogeleiegokieocmn', {})
            registered = entry.get('path')
            if not registered:
                continue
            managed = Path(registered).absolute() == expected.absolute() and not expected.is_symlink()
            result.append({'profile': path.parent.name, 'registered_path': registered, 'managed_path': str(expected),
                           'managed': managed, 'activation_action': None if managed else
                           'Re-register Load unpacked from the managed directory using the unchanged manifest key/ID; reloading the checkout path cannot activate this release.'})
        except (OSError, ValueError, TypeError):
            continue
    return result


def doctor(manifest_path: Path, source_root: Path, locations: Locations,
           *, runtime_reader: Callable[[dict], dict] = _runtime_state) -> dict:
    manifest = load_manifest(manifest_path)
    source_error = None
    try:
        source, _ = _inventory(manifest, source_root)
    except (OSError, ValueError) as error:
        source, source_error = {}, str(error)
    current = _pointer(locations, 'current')
    integrity_error = None
    try:
        installed = verify_release(current) if current else None
    except (OSError, ValueError) as error:
        installed, integrity_error = None, str(error)
    state = _diagnostic_json(locations.releases / 'installation.json')
    runtime_root = (locations.runtime or locations.state / 'runtime') / 'workspace-state'
    deadline = time.monotonic() + 8
    pending = _diagnostic_json(locations.releases / 'pending-install.json')
    scheduled = None
    if pending.get('state') != 'applied' and re.fullmatch(r'r-[0-9a-f]{24}', str(pending.get('revision', ''))):
        try:
            scheduled = verify_release(locations.releases / pending['revision'])
        except (OSError, ValueError) as error:
            pending = {**pending, 'integrity_error': str(error)}
    entries = []
    diagnostics = []
    for component in manifest['components']:
        diagnostics.append(component)
        diagnostics.extend({**probe, 'name': component['name'] + '/' + probe['name'], 'owner': component['name']} for probe in component.get('probes', []))
    for component in diagnostics:
        name = component['name']
        owner = component.get('owner', name)
        runtime = {}
        if component.get('runtime'):
            try:
                if time.monotonic() >= deadline:
                    raise TimeoutError('overall runtime diagnostic deadline exceeded')
                runtime = runtime_reader({**component['runtime'], '_deadline': deadline, 'runtime_directory': str(runtime_root)})
            except (OSError, ValueError, RuntimeError, SyntaxError, subprocess.TimeoutExpired) as error:
                runtime = {'unavailable': str(error)}
        revision = (runtime.get('build') or {}).get('revision')
        known = revision not in {None, '', 'development', 'unknown'}
        owner_definition = next(item for item in manifest['components'] if item['name'] == owner)
        installed_revision = installed['revision'] if owner_definition.get('install') and installed and owner in installed['components'] and not (component.get('profile') and component['profile'] not in state.get('profiles', [])) and state.get('mode') != 'development' else None
        capabilities = runtime.get('capabilities')
        missing = sorted(set(component.get('capabilities', [])) - set(capabilities)) if isinstance(capabilities, list) and all(isinstance(value, str) for value in capabilities) else None
        running_protocol = runtime.get('protocol_version', runtime.get('interface_version'))
        required_protocol = component.get('protocol')
        entries.append({'component': name, 'source': {key: value for key, value in source.get(owner, {}).items() if key != 'files'} or None,
                        'source_matches_installed': (source.get(owner, {}).get('content_digest') == (installed or {}).get('components', {}).get(owner, {}).get('content_digest')) if owner in source and installed and owner in installed['components'] else None,
                        'scheduled_revision': scheduled['revision'] if scheduled and owner in scheduled['components'] else None,
                        'source_matches_scheduled': (source.get(owner, {}).get('content_digest') == scheduled['components'].get(owner, {}).get('content_digest')) if scheduled and owner in source and owner in scheduled['components'] else None,
                        'installed_revision': installed_revision, 'running_revision': revision if known else None,
                        'running_identity': 'known' if known else 'unknown',
                        'pending_activation': installed_revision is not None and (not known or revision != installed_revision),
                        'required_protocol': required_protocol, 'running_protocol': running_protocol,
                        'protocol_compatible': (running_protocol >= required_protocol) if type(running_protocol) is int and type(required_protocol) is int else None,
                        'required_capabilities': component.get('capabilities', []), 'missing_capabilities': missing,
                        'runtime': {key: value for key, value in runtime.items() if key in {
                            'build', 'capabilities', 'protocol_version', 'interface_version', 'unavailable',
                            'instances', 'mixed_builds', 'enabled', 'enable_epoch', 'recovery_pending', 'pid', 'login_generation'}}})
    runtime_root = (locations.runtime or locations.state / 'runtime') / 'workspace-state'
    status = _diagnostic_json(runtime_root / 'login-hud-status.json')
    operation = _diagnostic_json(runtime_root / 'current-operation.json')
    checkpoint = _diagnostic_json(locations.data / 'workspace-state/snapshots/current.json')
    changed = [path for path, link in state.get('bindings', {}).items()
               if not Path(path).is_symlink() or os.readlink(path) != link]
    return {'schema_version': SCHEMA_VERSION, 'python_build': build_fingerprint(),
            'source_error': source_error, 'installed_integrity_error': integrity_error,
            'scheduled_revision': pending.get('revision') if pending.get('state') != 'applied' else None,
            'pending_install': pending or None, 'activation_pending': bool(pending and pending.get('state') != 'applied'),
            'mode': state.get('mode', 'uninstalled'), 'current': current.name if current else None, 'previous': (_pointer(locations, 'previous') or Path('')).name or None,
            'components': entries, 'changed_installed_paths': changed,
            'chrome_registration': _chrome_registration(locations),
            'operation_context': operation.get('operation_context', status.get('operation_context')), 'operation_state': status.get('operation_state'),
            'checkpoint_version': checkpoint.get('version'),
            'diagnostic_errors': {name: value['_read_error'] for name, value in
                                  {'installation': state, 'status': status, 'operation': operation, 'checkpoint': checkpoint}.items() if '_read_error' in value}, 'activation': ('release installation scheduled for next graphical login; current session is not restarted'
                                                 if pending and pending.get('state') != 'applied' else
                                                 'no release installation scheduled; runtime reload or next login may still be needed')}


def _default_manifest() -> Path:
    return Path(__file__).resolve().parents[2] / 'config/desktop-release.toml'


def _command(args) -> int:
    locations = Locations.environment()
    if args.deployment_action == 'apply-pending' and args.receipt:
        receipt = Path(args.receipt)
        pending = _pending_record(receipt)
        locations = Locations(**{key: Path(value) if value is not None else None for key, value in pending['locations'].items()})
        if receipt.absolute() != locations.releases / 'pending-install.json':
            raise ValueError('pending receipt path does not match its installation roots')
    manifest = Path(args.manifest).expanduser()
    source_root = Path(args.source_root).expanduser() if args.source_root else manifest.resolve().parents[2]
    if not args.source_root and len(manifest.resolve().parents) > 3:
        packaged = _load(manifest.resolve().parents[3] / 'release.json', {})
        original = packaged.get('components', {}).get('workspace-state', {}).get('source')
        if original:
            source_root = Path(original).parent
    if args.deployment_action in {'dev-link', 'deploy'}:
        release = stage(manifest, source_root, locations)
        result = (install(release['revision'], locations, profiles=tuple(args.profile), development=True)
                  if args.deployment_action == 'dev-link' else schedule(release['revision'], locations, profiles=tuple(args.profile), reload_manager=True))
    elif args.deployment_action == 'schedule':
        result = schedule(args.revision, locations, profiles=tuple(args.profile), reload_manager=not args.no_daemon_reload)
    elif args.deployment_action == 'apply-pending':
        try:
            result = apply_pending(locations, reload_manager=True)
        except ActivationDeferred as error:
            # A dependency may run this helper again while the desktop is live.
            # Successful deferral is not an installation: retain the waiting
            # receipt and report it explicitly without failing the systemd unit.
            result = {**error.pending, 'deferred': True}
    elif args.deployment_action == 'stage':
        result = stage(manifest, source_root, locations)
        result = {'revision': result['revision'], 'components': list(result['components']), 'installed': False}
    elif args.deployment_action == 'install':
        blockers = _activation_blockers(locations)
        if blockers:
            raise RuntimeError('use deployment schedule while a desktop/coordinator is active: ' + '; '.join(blockers))
        result = install(args.revision, locations, profiles=tuple(args.profile))
    elif args.deployment_action == 'rollback':
        blockers = _activation_blockers(locations)
        if blockers:
            raise RuntimeError('schedule the previous revision while a desktop/coordinator is active: ' + '; '.join(blockers))
        result = rollback(locations)
    elif args.deployment_action == 'migrate-inventory':
        path = Path(args.path) if args.path else locations.config / 'workspace-state/alerts.d/owned-systems.toml'
        result = {'changed': migrate_inventory(path), 'path': str(path)}
    elif args.deployment_action == 'fingerprint':
        result = build_fingerprint()
    else:
        result = doctor(manifest, source_root, locations)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def add_parser(subparsers):
    parser = subparsers.add_parser('deployment', help='stage, install, inspect or roll back desktop releases')
    parser.add_argument('--manifest', default=str(_default_manifest()))
    parser.add_argument('--source-root')
    commands = parser.add_subparsers(dest='deployment_action', required=True)
    for name in ('stage', 'doctor', 'rollback', 'fingerprint'):
        commands.add_parser(name).set_defaults(func=_command)
    install_parser = commands.add_parser('install')
    install_parser.add_argument('revision')
    install_parser.add_argument('--profile', action='append', default=[], choices=['host'])
    install_parser.add_argument('--host-integration', action='append_const', const='host', dest='profile')
    install_parser.set_defaults(func=_command)
    scheduled = commands.add_parser('schedule', help='apply before the next graphical login, without restarting this session')
    scheduled.add_argument('revision')
    scheduled.add_argument('--profile', action='append', default=[], choices=['host'])
    scheduled.add_argument('--host-integration', action='append_const', const='host', dest='profile')
    scheduled.add_argument('--no-daemon-reload', action='store_true')
    scheduled.set_defaults(func=_command)
    apply = commands.add_parser('apply-pending')
    apply.add_argument('--receipt')
    apply.set_defaults(func=_command)
    deploy = commands.add_parser('deploy', help='stage and schedule one coherent production release')
    deploy.add_argument('--profile', action='append', default=[], choices=['host'])
    deploy.add_argument('--host-integration', action='append_const', const='host', dest='profile')
    deploy.set_defaults(func=_command)
    development = commands.add_parser('dev-link', help='explicitly link mutable checkouts; runtime identity remains development')
    development.add_argument('--profile', action='append', default=[], choices=['host'])
    development.add_argument('--host-integration', action='append_const', const='host', dest='profile')
    development.set_defaults(func=_command)
    migration = commands.add_parser('migrate-inventory')
    migration.add_argument('--path')
    migration.set_defaults(func=_command)
    return parser


def main(argv=None):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest='command', required=True)
    add_parser(subparsers)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    raise SystemExit(main())
