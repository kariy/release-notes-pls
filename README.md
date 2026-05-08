# release-notes-pls

Generates GitHub release notes from a tag-to-tag (or tag-to-SHA) range using Claude.

The output is a single markdown document with:

1. A title (`# <Project> <head> Release Notes`)
2. An LLM-written 1–2 sentence opening summary
3. Curated, user-facing sections (Breaking Changes / Features / Fixes / Performance) — only sections with relevant changes are emitted
4. A mechanical `## Full Changelog` listing every commit (excluding merge commits and release-prep commits)
5. A `**Full diff:** <compare URL>` line at the bottom

## Usage

```bash
export GITHUB_TOKEN=<github personal access token>
export OPENROUTER_API_KEY=<openrouter api key>

release-notes-pls --base v1.6.1 --head v1.6.2 dojoengine/katana
```

Uses `anthropic/claude-sonnet-4.5` via [OpenRouter](https://openrouter.ai) by default.

### Flags

| Flag             | Description                                                                    |
| ---------------- | ------------------------------------------------------------------------------ |
| `--base`         | Previous release tag (e.g. `v1.6.1`), or `auto` to detect the latest published release. |
| `--head`         | New release tag or commit SHA to compare against.                              |
| `--project-name` | Project name in the title. Defaults to the GitHub repo name.                   |
| `--output`       | Path to write the notes to. Prints to stdout if omitted.                       |
| `repo`           | GitHub repository, as `owner/repo`.                                            |

### Examples

Generate notes for a published release:

```bash
release-notes-pls --base v1.6.1 --head v1.6.2 dojoengine/katana
```

Auto-detect the previous tag and target an unreleased commit:

```bash
release-notes-pls --base auto --head $(git rev-parse HEAD) dojoengine/katana
```

Override the project name and write to a file:

```bash
release-notes-pls \
  --base v1.6.1 --head v1.6.2 \
  --project-name Katana \
  --output release-notes.md \
  dojoengine/katana
```

## Format validation

After generation, the output is validated against the template structure (title, section ordering, bullet formats, diff link, trailing newline). On any mismatch, the file is still written but the tool exits with code `2` and prints line-numbered errors to stderr. This catches structural issues from the LLM (renamed sections, malformed PR refs, missing diff link) without relying on a second AI pass.

## Release type inference

The release type is derived from the commit history between `base` and `head`:

- Any `BREAKING CHANGE:` footer or `!` after the conventional-commit type → **major**
- Any `feat:` commit (with no breaking change) → **minor**
- Otherwise → **patch**

The release type only steers the tone of the opening summary — section scaffolding is driven by the commits themselves.
