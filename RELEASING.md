# Releasing

Elengtis publishes the Python CLI to PyPI and the matching container image to
GHCR from a published GitHub release. The tag is immutable release input; a
PyPI version cannot be replaced after upload.

## One-time setup

The PyPI trusted publisher is:

- project: `elengtis`
- owner: `nicoleman0`
- repository: `elengtis`
- workflow: `publish.yml`
- environment: `pypi`

A pending publisher creates the PyPI project on its first successful upload.
The workflow uses GitHub OIDC, so no PyPI API token belongs in repository
secrets.

GitHub only triggers a `release` workflow when that workflow file exists on
the default branch. Merge the small workflow-bootstrap PR into `main` before
publishing the first release; the release run itself checks out the tagged
CLI-only commit.

After the first GHCR publish, open the package settings and change the package
visibility to public.

## Checklist

1. Run `uv run --offline python -m unittest discover -s tests -v`.
2. Run both Docker smokes from `.github/workflows/checks.yml`.
3. Confirm the version in `pyproject.toml` and date the release in
   `CHANGELOG.md`; run `uv lock --check`.
4. Build and inspect the artifacts:

   ```sh
   uv build
   uvx twine check dist/*
   uv run --no-project --with ./dist/elengtis-*.whl elengtis --help
   ```

5. Build and smoke the image:

   ```sh
   docker build -t elengtis:release .
   docker run --rm elengtis:release --help
   docker run --rm elengtis:release example --out /tmp/results
   docker run --rm --entrypoint docker elengtis:release --version
   ```

6. Merge the release PR into `release/0.1`, then create and push an annotated
   tag from that merge commit:

   ```sh
   git tag -a v0.1.0 -m 'v0.1.0'
   git push origin v0.1.0
   ```

7. Publish a GitHub release from `v0.1.0`. The `publish` workflow builds and
   checks the sdist and wheel, uploads them to PyPI with trusted publishing,
   and pushes `ghcr.io/nicoleman0/elengtis` with `0.1.0`, `0.1`, and `latest`
   tags.
8. Confirm availability after registries finish indexing:

   ```sh
   uv run --no-project --with elengtis==0.1.0 elengtis --help
   docker pull ghcr.io/nicoleman0/elengtis:0.1.0
   ```

If a PyPI artifact is wrong, yank it and publish a new patch version. Never
move the release tag or attempt to replace an existing PyPI version.
