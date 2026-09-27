#!/usr/bin/env python3
"""Package and install the local workspace-state VS Code companion."""
import argparse, json, os, subprocess, tempfile, zipfile
from urllib.parse import urlparse
from xml.etree import ElementTree
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / 'vscode-extension'

def make_vsix(destination=None):
    if not (EXT / 'package.json').is_file() or not (EXT / 'extension.js').is_file():
        raise RuntimeError('incomplete vscode-extension directory')
    if destination is None:
        fd, name = tempfile.mkstemp(prefix='workspace-state-', suffix='.vsix'); os.close(fd); destination = Path(name)
    destination = Path(destination)
    package = json.loads((EXT / 'package.json').read_text(encoding='utf-8'))
    publisher = str(package.get('publisher', ''))
    name = str(package.get('name', ''))
    version = str(package.get('version', ''))
    if not publisher or not name or not version:
        raise RuntimeError('package.json needs publisher, name, and version')
    ElementTree.register_namespace('', 'http://schemas.microsoft.com/developer/vsx-schema/2011')
    ns = 'http://schemas.microsoft.com/developer/vsx-schema/2011'
    root = ElementTree.Element(f'{{{ns}}}PackageManifest', {'Version': '2.0.0'})
    metadata = ElementTree.SubElement(root, f'{{{ns}}}Metadata')
    ElementTree.SubElement(metadata, f'{{{ns}}}Identity', {'Id': name, 'Version': version, 'Publisher': publisher})
    for tag, value in [('DisplayName', package.get('displayName', name)), ('Description', package.get('description', ''))]:
        ElementTree.SubElement(metadata, f'{{{ns}}}{tag}').text = str(value)
    ElementTree.SubElement(metadata, f'{{{ns}}}Tags').text = 'workspace'
    ElementTree.SubElement(metadata, f'{{{ns}}}Categories').text = 'Other'
    props = ElementTree.SubElement(metadata, f'{{{ns}}}Properties')
    ElementTree.SubElement(props, f'{{{ns}}}Property', {'Id': 'Microsoft.VisualStudio.Code.Manifest', 'Value': 'extension/package.json'})
    install = ElementTree.SubElement(root, f'{{{ns}}}Installation')
    ElementTree.SubElement(install, f'{{{ns}}}InstallationTarget', {'Id': 'Microsoft.VisualStudio.Code', 'Version': '^1.85.0'})
    ElementTree.SubElement(root, f'{{{ns}}}Dependencies')
    assets = ElementTree.SubElement(root, f'{{{ns}}}Assets')
    ElementTree.SubElement(assets, f'{{{ns}}}Asset', {'Type': 'Microsoft.VisualStudio.Code.Manifest', 'Path': 'extension/package.json'})
    manifest = ElementTree.tostring(root, encoding='utf-8', xml_declaration=True)
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as out:
        out.writestr('[Content_Types].xml', '''<?xml version="1.0" encoding="utf-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="json" ContentType="application/json"/><Default Extension="js" ContentType="text/javascript"/><Override PartName="/extension.vsixmanifest" ContentType="text/xml"/></Types>''')
        out.writestr('extension.vsixmanifest', manifest)
        for p in EXT.rglob('*'):
            if p.is_file(): out.write(p, Path('extension') / p.relative_to(EXT))
    return destination

def existing_profiles(user_data_dir):
    """Return verified named profiles; never infer/create unknown profiles."""
    root = Path(user_data_dir) if user_data_dir else Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'Code'
    candidates = [root / 'User' / 'globalStorage' / 'storage.json', root / 'storage.json']
    names = set()
    for registry in candidates:
        try:
            if registry.stat().st_size > 8 * 1024 * 1024:
                raise RuntimeError('VS Code profile registry is unexpectedly large')
            data = json.loads(registry.read_text(encoding='utf-8'))
        except (OSError, ValueError): continue
        entries = data.get('userDataProfiles', []) if isinstance(data, dict) else []
        if not isinstance(entries, list): continue
        for item in entries:
            if not isinstance(item, dict): continue
            name = item.get('name')
            location = item.get('location')
            location_path = None
            if isinstance(location, dict): location = location.get('path')
            if isinstance(location, str):
                parsed = urlparse(location)
                location_path = parsed.path if parsed.scheme == 'file' else location
            profile_id = item.get('id') or (Path(location_path).name if location_path else None)
            verified = profile_id and Path(str(profile_id)).name == profile_id and (root / 'User' / 'profiles' / profile_id).is_dir()
            if isinstance(name, str) and name and verified: names.add(name)
    return sorted(names)

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--code', default='code')
    parser.add_argument('--user-data-dir')
    parser.add_argument('--extensions-dir')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--package-only', action='store_true')
    parser.add_argument('--profile')
    parser.add_argument('--all-profiles', action='store_true')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args(argv)
    vsix = make_vsix(args.output)
    if not args.package_only:
        profiles = [args.profile] if args.profile else ([None] + existing_profiles(args.user_data_dir) if args.all_profiles else [None])
        if args.profile and args.profile not in existing_profiles(args.user_data_dir):
            raise SystemExit(f'profile does not exist in the verified registry: {args.profile}')
        for profile in profiles:
            cmd = [args.code, '--install-extension', str(vsix)]
            if args.force: cmd.append('--force')
            if args.user_data_dir: cmd += ['--user-data-dir', args.user_data_dir]
            if args.extensions_dir: cmd += ['--extensions-dir', args.extensions_dir]
            if profile: cmd += ['--profile', profile]
            subprocess.run(cmd, check=True, timeout=90)
    print(vsix)
    return 0

if __name__ == '__main__': raise SystemExit(main())
