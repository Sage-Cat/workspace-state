# Repository layout

![Public repository dependencies](repository-dependencies.svg)

[PlantUML source](repository-dependencies.puml).

## Clone the tested set

[Desktop Workspace](https://github.com/Sage-Cat/desktop-workspace) pins the six
public tools as Git submodules. Each tool keeps its own history and releases.

```sh
git clone --recurse-submodules https://github.com/Sage-Cat/desktop-workspace.git
cd desktop-workspace
npm --prefix login-hud ci
make check
make integration
```

```text
desktop-workspace/
  workspace-state/
  gnome-winctl/
  login-hud/
  hide-suspend/
  input-source-popup-guard/
  gc-profiled/
```

The sibling paths match the installer and integration harnesses. `make pins`
shows the recorded versions. `make stage` packages them; `make install` schedules
installation for the next graphical login. Both require clean, initialized
components at the recorded commits. Unlisted siblings are excluded from the
parent's deployment manifest and public source archive.

## Dependencies

- `workspace-state` calls `gnome-winctl` and supplies progress to `login-hud`.
- `login-hud` sends scoped actions back and optionally reads `gc-profiled` status.
- The default installer requires the window service, HUD and both small extensions.
- The small extensions and scheduler also work independently.

## Updates

Commit and push a component change in its own repository first. Then update its
parent gitlink with `git add COMPONENT`, run the parent's source and integration
checks, and commit/push the pin update. Child branches do not silently advance
parent pins. See the [parent development guide](https://github.com/Sage-Cat/desktop-workspace/blob/main/docs/development.md).

A standalone workspace-state checkout still supports `make check`. Existing
sibling checkouts also work with the original installer. Its
[release manifest](../config/desktop-release.toml) defines source paths and files;
staging records their current commits and content hashes, including local edits.
Use the parent workflow when you need a clean, pinned public component set.
