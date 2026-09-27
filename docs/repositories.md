# Repository layout

![Public repository dependencies](repository-dependencies.svg)

[PlantUML source](repository-dependencies.puml).

## Current layout

The repositories are separate sibling checkouts. There are no Git submodules.

- `workspace-state` calls `gnome-winctl` and supplies progress to `login-hud`.
- `login-hud` sends scoped actions back and optionally reads `gc-profiled` status.
- The default installer requires the window service, HUD and both small extensions.
- `gc-profiled` is optional. The small extensions do not depend on the coordinator.

[The release manifest](../config/desktop-release.toml) lists source paths and
packaged files. Staging records source commits and file hashes. It packages the
files currently checked out, including local edits; it does not fetch a pinned
set of repository revisions.

## Suggested next step: an umbrella repository

A separate public repository could pin all six tools as Git submodules:

```text
desktop-workspace/          # proposed parent repository
  workspace-state/          # submodule
  gnome-winctl/             # submodule
  login-hud/                # submodule
  hide-suspend/             # submodule
  input-source-popup-guard/ # submodule
  gc-profiled/              # submodule
```

This preserves the sibling paths used by the installer and integration harnesses.
Each tool keeps its own history and releases. The parent records a tested set of
commits; cloning it with `--recurse-submodules` retrieves that set.
[Git submodule documentation](https://git-scm.com/docs/gitsubmodules).

A component update would need a parent pointer update and integration checks.
The parent should advance only after those checks pass, rather than follow every
child branch automatically. Keep private or machine-specific components outside
the public clone requirements.

Submodules inside `workspace-state` are also possible, but would require changing
source-root resolution, sibling test paths and release packaging. The current
source archive uses `git archive` and would need explicit dependency packaging.
The privacy checks would also need to handle and audit submodule entries.

This is a proposal; the current checkout layout and release process are unchanged.
