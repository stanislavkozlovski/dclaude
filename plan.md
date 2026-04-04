# Plan: Homebrew Install + Release Versioning

## Goal

Make `dclaude` installable with Homebrew, keep releases versioned with `vX.Y.Z` tags, and wire CI so a release can update the tap with minimal manual work.

## Findings

- The launcher layout already fits Homebrew well: `dclaude` and `dcodex` resolve symlinks to their real install path, so a formula can install the repo into `libexec` and expose both binaries with `bin.install_symlink`.
- This repo currently has no tags, no release workflow, and no tap.
- The current default Docker image tag is `dclaude:local`. That is not good enough for production installs because a `brew upgrade` would still reuse the old image unless the user runs `--rebuild`.
- Unlike Tansu, we do not need per-arch release binaries. One source tarball is enough because Brew is only installing shell scripts, the Dockerfile, and support files.
- A public `LICENSE` file is missing. That should be fixed before publishing a tap.

## Recommended Shape

### 1. Make the repo releaseable

- Add a repo-level `VERSION` file.
- Adopt semver tags: `vX.Y.Z`.
- Teach the wrappers to read `VERSION` and default the Docker image name to something versioned, for example `dclaude:<version>`, while still allowing `DCLAUDE_IMAGE_NAME` to override it.
- Add `--version` to `dclaude` and `dcodex` so installs and formulas have a stable way to verify what was installed.
==STAN: Add an auto bump CI to the github CI too. each merge to main should bump it and release the new thing to homebrew==

### 2. Add CI in the main repo

- PR / main CI:
  - `shellcheck` on the shell scripts
  - `bash -n` on the wrappers and shared scripts
  - `docker build` smoke test for the image
  - `./dclaude --help`, `./dcodex --help`, and `./dclaude --version`
- Tag CI on `v*`:
  - verify the tag matches `VERSION`
  - create a GitHub Release
  - compute the SHA256 for the release tarball the tap will use
  - update the tap repo formula and open or push the bump

### 3. Create a separate tap repo

- Mirror the Tansu layout with a dedicated tap repo, for example `stanislavkozlovski/homebrew-tap`.
- Add `Formula/dclaude.rb`.
- Formula install logic should:
  - install the runtime files into `libexec`
  - symlink `dclaude` and `dcodex` into `bin`
  - keep the package platform-agnostic
- Formula test should only use commands that do not need Docker or a git repo, for example:
  - `dclaude --help`
  - `dcodex --help`
  - `dclaude --version`
- Add caveats that Docker is required and the commands must be run from inside the target git repo.

### 4. Automate formula updates

- Follow the Tansu pattern and keep a tiny generator in the tap repo:
  - `Formula/dclaude.rb.jinja`
  - `util/update_formula.py`
- For this repo the template is simpler than Tansu:
  - one tarball URL
  - one SHA256
  - one formula for both `dclaude` and `dcodex`
- Main repo release CI should run the tap update automatically after a tagged release.
- If cross-repo automation is not ready yet, start with a manual PR into the tap repo, but keep the script in place from day one.

### 5. Update docs together

- Update `README.md`, `HOWRUN.md`, and `ARCHITECTURE.md` in the same change.
- Document:
  - the Homebrew install command
  - the release/tagging process
  - the fact that Docker images are versioned by release now
  - how users force a rebuild when they want one

## Suggested Order

1. Add `LICENSE`, `VERSION`, wrapper `--version`, and versioned Docker image naming.
2. Add main-repo CI for linting, smoke tests, and tagged releases.
3. Create `stanislavkozlovski/homebrew-tap` with the first formula and test block.
4. Add tap-update automation from the main repo release workflow.
5. Cut `v0.1.0`, verify `brew install stanislavkozlovski/tap/dclaude`, then document the public install path.

## Validation

- Local formula test:
  - `brew install --build-from-source ./Formula/dclaude.rb`
  - `brew test dclaude`
- Release dry run:
  - push a temporary tag in a test repo or branch
  - confirm the tap formula SHA updates correctly
  - confirm a clean machine installs and runs `dclaude --help`
- Upgrade test:
  - install `vX.Y.Z`
  - upgrade to `vX.Y.Z+1`
  - confirm the wrapper uses the new versioned Docker image name without needing the old `dclaude:local` image to be deleted manually

## References

- Tansu tap: https://github.com/tansu-io/homebrew-tap
- Tansu release packaging flow: https://github.com/tansu-io/tansu/blob/main/.github/workflows/ci.yml
- Homebrew Formula Cookbook: https://docs.brew.sh/Formula-Cookbook
