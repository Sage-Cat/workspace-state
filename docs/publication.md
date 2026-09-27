# Source checks and automatic releases

Every push to `main` publishes a checked `build-<full commit SHA>` release in that
repository. Login HUD uses `master` and also retains its `v<extension version>`
release workflow. Pull requests run checks without publication permissions.
Publication uses only the repository's `GITHUB_TOKEN` with `contents: write` in the
publish job; no personal token, sibling checkout, or cross-private-repository
access is needed. The existing pinned checkout/Node actions are shared by all
seven repositories.

Before source checks or publication, `.github/scripts/privacy.py` rejects tracked
private instruction files, runtime/log/transcript/checkpoint paths, databases,
credential files, obvious token/private-key signatures, and automated attribution in the
HEAD commit message. Diagnostics show only file paths or commit IDs, never matched
contents. Functional terminal application adapters and extension UUIDs remain allowed. The only
database-fixture exception is a validated `tests/fixtures/*.sqlite.schema.json`
containing schema entries rather than rows. Use `--index` to inspect staged files
before committing, and `--history` to additionally inspect all reachable HEAD
commit messages. This targeted gate supplements human privacy review; it does not
claim to detect every possible secret or private value.

Each release contains a deterministic committed-source `.tar.gz`, `SHA256SUMS`,
and a `.shell-extension.zip` for each extension declared in
`.github/release.json`. GNOME bundles contain their declared runtime files only;
`buildInfo.js` is stamped with the source commit. Workspace State includes its
optional LG edge extension; its source archive also contains the application
companions. No workflow installs into a user's desktop, starts a service, enables
cleanup, or publishes checkout-local logs and ignored files. Tracked files remain
publication inputs and must pass the usual privacy review before committing.

The publisher creates a full-commit tag, creates a draft, uploads missing assets,
verifies their SHA-256 hashes and tag target, then publishes. Rerunning verifies an
existing release without replacing its tag or artifacts. An interrupted draft can resume, but a tag that
points elsewhere, conflicting bytes, unexpected assets, or an incomplete already
published release cause failure. Tags are never moved and assets are never
clobbered. A commit release becomes GitHub's Latest only when its SHA still matches
that repository's remote default branch. Artifact publication remains parallel
per commit and uses `--skip-promotion`; a separate `promote-latest` job serializes
Latest changes per repository. The job selects the current remote head's published
release, independently of its triggering commit, then immediately rechecks the
head before promotion. Thus an older job can safely promote the newest eligible
release if GitHub drops an intermediate pending job. If that head's release is
still missing or draft, promotion waits for its publication job.

Manual `publish` retains immediate head-checked promotion. The separate
`python3 .github/scripts/release.py promote --repo OWNER/REPO` action can repair
Latest after successful publication without replacing any tag or asset. Once the
current release is Latest, reruns make no writes. Explicit semantic-tag
publications retain their intentional Latest promotion.

This is append-only behavior enforced by the publication helper. GitHub's
[immutable release setting](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases)
provides additional server enforcement when enabled; it is not changed or
required by the workflow. The output's `github_immutable` field reports the actual
server flag separately from `append_only_verified`. Without server enforcement,
an authorized administrator could still modify a release outside this helper.

The same self-contained `.github/scripts/release.py` and offline regression tests
are vendored in each repository so private tools remain independently buildable.
When changing publication behavior, update these identical copies together and
run `python3 .github/scripts/test_release.py -v` in each repository. These tests
cover tag integrity, reruns, interrupted drafts, conflicting assets, semantic
tags, and deterministic artifacts built independently of dirty/untracked files.

Workspace State CI runs standalone `make check` plus the LG edge policy test.
Its unit suite does not require sibling repositories. Its broader `make test`
checks sibling GNOME Winctl and Login HUD sources; each repository's own CI checks
those sources instead. Real application/GNOME integration remains a separately
invoked isolated desktop test and is not claimed by these headless checks.

For an authorized manual fallback, first commit the reviewed source and pass the
same checks as the repository workflow, then from that clean checkout run:

```sh
commit=$(git rev-parse HEAD)
python3 .github/scripts/release.py prepare --sha "$commit" --output release-dist
python3 .github/scripts/release.py publish --sha "$commit" --repo OWNER/REPO --output release-dist
```

Use an empty output directory. Preparing is local; publishing is a remote write
and requires authenticated `gh`. A semantic HUD release additionally requires an
existing matching version tag and `--tag vVERSION`. Neither operation installs or
activates the downloaded tools.
