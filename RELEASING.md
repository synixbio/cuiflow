# Releasing cuiflow

PyPI never accepts the same version twice, even after a release is deleted. Every step before
the upload exists to catch a mistake while it can still be fixed. Commands are PowerShell, run
from the repository root.

## Before the release

- [ ] **The GitHub repository is public**, and `main` on `origin` holds the release commit. The
  README's links and the project URLs in `pyproject.toml` all point at
  `https://github.com/synixbio/cuiflow`. While the repository is private, every link on the
  PyPI page is a 404.
- [ ] **The working tree is clean** (`git status`), on `main`, up to date with both remotes.
- [ ] **The version is new.** `__version__` in `src/cuiflow/__init__.py` is not on
  <https://pypi.org/project/cuiflow/#history> and has no tag yet (`git tag`).
- [ ] **The changelog is dated.** The top section of `CHANGELOG.md` is `## <version> (YYYY-MM-DD)`,
  not `(unreleased)`, and lists everything since the last release.
- [ ] **Dependency floors are right.** Every extra in `pyproject.toml` resolves from PyPI
  alone, without the sibling checkouts in `../mmlite` and `../umlsmatch`. If a mode needs an
  unreleased engine change, the README says so.
- [ ] **The docs agree on the release.** No README, DESIGN_PLAN or CI text still says the
  package is unpublished, and the README's Status table is current.
- [ ] **The checks pass locally:**

  ```powershell
  .venv\Scripts\ruff check src tests tools examples
  .venv\Scripts\ruff format --check src tests tools examples
  .venv\Scripts\mypy
  .venv\Scripts\python -m pytest -q -m "not integration"
  .venv\Scripts\python -m pytest -q -m integration   # with the CUIFLOW_* indexes set
  ```

## Build and check

- [ ] **Build from an empty `dist/`**, so no older file is uploaded with the new ones:

  ```powershell
  Remove-Item -Recurse -Force dist -ErrorAction SilentlyContinue
  .venv\Scripts\python -m build
  .venv\Scripts\python -m twine check --strict dist/*
  ```

- [ ] **Read the file names:** exactly `cuiflow-<version>.tar.gz` and
  `cuiflow-<version>-py3-none-any.whl`.
- [ ] **The wheel installs clean**, in a fresh environment, with no editable engines:

  ```powershell
  python -m venv $env:TEMP\cf-smoke
  & $env:TEMP\cf-smoke\Scripts\python -m pip install "dist\cuiflow-<version>-py3-none-any.whl[all]"
  & $env:TEMP\cf-smoke\Scripts\cuiflow --help
  & $env:TEMP\cf-smoke\Scripts\python -c "import cuiflow; print(cuiflow.__version__)"
  ```

## Rehearse on TestPyPI

- [ ] Upload to TestPyPI (needs a TestPyPI API token; username `__token__`):

  ```powershell
  .venv\Scripts\python -m twine upload --repository testpypi dist/*
  ```

- [ ] Check <https://test.pypi.org/project/cuiflow/>: the README renders, its links open, and
  the version, extras and project URLs are right.
- [ ] Install from TestPyPI in a fresh environment. The dependencies are only on the real
  index, so add it as an extra index:

  ```powershell
  python -m venv $env:TEMP\cf-test
  & $env:TEMP\cf-test\Scripts\python -m pip install --index-url https://test.pypi.org/simple/ `
      --extra-index-url https://pypi.org/simple/ "cuiflow[all]==<version>"
  & $env:TEMP\cf-test\Scripts\cuiflow --help
  ```

## Tag and publish

A `v<version>` tag makes GitHub CI build, check and smoke-install the sdist and wheel, then
upload those same files to PyPI through Trusted Publishing. No API token is involved. The Gitea
mirror runs the same checks but never publishes.

- [ ] Tag the release commit and push the tag. Push to GitHub last, because that push is the
  upload:

  ```powershell
  git tag -a v<version> -m "cuiflow <version>"
  git push gitea main v<version>
  git push origin main v<version>
  ```

- [ ] Watch the run on GitHub. `test`, then `build` (which also checks the tag against
  `__version__`), then `publish`. If the `pypi` environment has required reviewers, approve
  the `publish` job when it waits.

If `publish` cannot run, for example because the GitHub repository is not set up yet, upload by
hand with an API token scoped to `cuiflow`. Upload the files you checked above:

```powershell
.venv\Scripts\python -m twine upload dist/*
```

### One-time setup for Trusted Publishing

Do this once, before the first tagged release that CI should publish:

1. On PyPI, open **cuiflow → Manage → Publishing** and add a GitHub publisher: owner
   `synixbio`, repository `cuiflow`, workflow `ci.yml`, environment `pypi`.
2. On GitHub, open **Settings → Environments** and create an environment named `pypi`. Adding
   yourself as a required reviewer is recommended, so no tag push reaches PyPI without a click.
   A deployment rule that allows only `v*` tags adds a second guard.
3. Optional, for the rehearsal: add a TestPyPI publisher the same way, with environment
   `testpypi`. Until you do, the TestPyPI step above stays manual.

## After the release

- [ ] `pip install "cuiflow[all]==<version>"` works in a fresh environment, and
  <https://pypi.org/project/cuiflow/> shows the right README and links.
- [ ] Create a GitHub release from the tag, with the changelog section as its notes.
- [ ] Open a new `## Unreleased` section at the top of `CHANGELOG.md`.
