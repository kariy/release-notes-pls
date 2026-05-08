import argparse
import concurrent.futures
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from github import Auth, Github
from openai import OpenAI
from pydantic import BaseModel, Field

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "anthropic/claude-sonnet-4.5"

PI_MODEL = "anthropic/claude-sonnet-4.5"
PI_TIMEOUT_SEC = 300
PI_MAX_PARALLEL = 2
CACHE_DIR = Path.home() / ".cache" / "release-notes-pls"


SEMVER_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")
CONVENTIONAL_PREFIX = re.compile(r"^([a-z]+)(?:\(([^)]+)\))?(!)?:")
PR_REF_IN_SUBJECT = re.compile(r"\(#(\d+)\)\s*$")
SKIPPABLE_PREFIXES = ("chore(release):", "release(prepare):", "release: ")


class Bullet(BaseModel):
    text: str = Field(
        description="User-visible behavior change in plain language. Present-tense imperative ('Add X', 'Fix Y'). No leading dash, no PR number suffix."
    )
    prs: list[int] = Field(
        default_factory=list,
        description="PR numbers associated with this user-visible change. May be empty if the underlying commit has no associated PR (e.g., direct push, or a squash-merge GitHub did not link back). One or more if multiple PRs contribute to the same change.",
    )


class CuratedNotes(BaseModel):
    summary: str = Field(
        description="1-2 sentence opening summary describing the character of this release. Tone matches release type."
    )
    breaking_changes: list[Bullet] = Field(default_factory=list)
    features: list[Bullet] = Field(default_factory=list)
    fixes: list[Bullet] = Field(default_factory=list)
    performance: list[Bullet] = Field(default_factory=list)


def resolve_base(base: str, repo) -> str:
    if base != "auto":
        return base
    for release in repo.get_releases():
        if release.draft or release.prerelease:
            continue
        if SEMVER_TAG.match(release.tag_name or ""):
            return release.tag_name
    raise SystemExit("Error: could not auto-detect previous release tag. Pass --base explicitly.")


def infer_release_type(commits) -> str:
    has_breaking = False
    has_feat = False
    for commit in commits:
        message = commit.commit.message
        subject = message.splitlines()[0] if message else ""
        if "BREAKING CHANGE:" in message:
            has_breaking = True
        match = CONVENTIONAL_PREFIX.match(subject)
        if match:
            type_, _scope, bang = match.groups()
            if bang:
                has_breaking = True
            if type_ == "feat":
                has_feat = True
    if has_breaking:
        return "major"
    if has_feat:
        return "minor"
    return "patch"


def is_changelog_worthy(commit) -> bool:
    subject = commit.commit.message.splitlines()[0]
    if subject.startswith(("Merge pull request", "Merge branch", "Merge remote-tracking branch")):
        return False
    if subject.startswith(SKIPPABLE_PREFIXES):
        return False
    return True


def commit_subject_with_pr(commit) -> str:
    subject = commit.commit.message.splitlines()[0]
    if PR_REF_IN_SUBJECT.search(subject):
        return subject
    prs = list(commit.get_pulls())
    if prs:
        return f"{subject} (#{prs[0].number})"
    return subject


def collect_pr_changes(commits) -> dict:
    changes = {}
    for commit in commits:
        for pr in commit.get_pulls():
            entry = changes.setdefault(pr.number, {"pr": pr, "commits": []})
            entry["commits"].append(commit.commit)
    return changes


def get_orphan_commits(commits, changes: dict) -> list:
    seen = {c.sha for change in changes.values() for c in change["commits"]}
    return [c for c in commits if c.sha not in seen and is_changelog_worthy(c)]


def format_changes_for_prompt(
    commits,
    changes: dict,
    pi_summaries: dict | None = None,
) -> str:
    parts = []
    for change in changes.values():
        pr = change["pr"]
        parts.append(f"\n--- PR #{pr.number} ---")
        parts.append(f"Title: {pr.title}")
        if pr.body:
            parts.append(f"Description:\n{pr.body}")
        parts.append("Commits:")
        for commit in change["commits"]:
            parts.append(f"- {commit.message.splitlines()[0]}")
        if pi_summaries and pr.number in pi_summaries:
            parts.append(f"Pi exploration summary:\n{pi_summaries[pr.number]}")

    orphans = get_orphan_commits(commits, changes)
    if orphans:
        parts.append("\n--- Direct commits (no associated PR found) ---")
        parts.append("These commits did not match any PR via the GitHub API. If their conventional-commit prefix (feat/fix/perf) implies a curated bullet, include them with an empty prs list.")
        for commit in orphans:
            parts.append(f"- {commit.commit.message.splitlines()[0]}")
            if pi_summaries and commit.sha in pi_summaries:
                parts.append(f"Pi exploration summary:\n{pi_summaries[commit.sha]}")
    return "\n".join(parts)


SUMMARY_HINTS = {
    "patch": (
        '"This release brings stability improvements and a set of important bug fixes." '
        'or "This release addresses a critical bug in <area>."'
    ),
    "minor": '"This release introduces <theme>, alongside several improvements and fixes."',
    "major": '"This is a major release that <high-level shift>. See Breaking Changes below before upgrading."',
}


def generate_curated_notes(client, project: str, release_type: str, changes_text: str) -> CuratedNotes:
    prompt = f"""You are an expert technical writer producing release notes for {project}.

The release is a {release_type} release based on its commit history.

Below are the pull requests merged since the previous release. For each PR you have its title, description, and the commits associated with it. Some PRs and orphan commits also include a "Pi exploration summary" section: this is a deeper analysis of the actual diff and surrounding code produced by a separate agent. When present, lean on it heavily to write user-facing bullets - it is the most accurate description of user-visible impact.

{changes_text}

Produce structured release notes:

1. summary: 1-2 sentence opening that describes the character of this release. Match the tone to a {release_type} release. Examples: {SUMMARY_HINTS[release_type]}

2. Categorize user-visible changes into the four buckets:
   - breaking_changes: Anything that requires users to change setup, configuration, or code
   - features: New user-facing capabilities (typically feat: commits)
   - fixes: Bug fixes that affect user-visible behavior (typically fix: commits)
   - performance: Performance improvements (typically perf: commits)

Rules for bullets:
- Use present-tense imperative ('Add X', 'Fix Y', 'Improve Z'). No leading dash. No PR number suffix.
- Describe each change in plain user-facing language. Do NOT regurgitate commit messages.
- Group multiple PRs that contribute to the same user-visible change into ONE bullet with all PR numbers.

REQUIRED inclusions - every commit whose subject starts with one of these conventional-commit prefixes MUST appear as a bullet in the matching curated section, even if it looks narrow, dev-only, or like internal tooling. The maintainer used the prefix to signal user-visibility; honor that signal and do not second-guess it:
- `feat:` or `feat(<scope>):` -> Features
- `fix:` or `fix(<scope>):` -> Fixes
- `perf:` or `perf(<scope>):` -> Performance
- Any `!:` after the type, or a `BREAKING CHANGE:` footer in the body -> Breaking Changes (in addition to its base category)

If you find yourself wanting to skip a `feat:` / `fix:` / `perf:` commit because it seems too small or too internal, DON'T - include it. A short bullet describing what was added/fixed is fine.

SKIP entirely (these belong only in the Full Changelog, which is assembled separately): `refactor:`, `docs:`, `ci:`, `test:`, `chore:`, `style:`, `build:`, dependency bumps, and `Revert` commits.

If a curated category has no matching commits, return an empty list for it.
"""

    response = client.beta.chat.completions.parse(
        model=DEFAULT_MODEL,
        max_tokens=16000,
        messages=[{"role": "user", "content": prompt}],
        response_format=CuratedNotes,
    )
    parsed = response.choices[0].message.parsed
    if parsed is None:
        raise SystemExit("Error: model did not return a parseable response.")
    return parsed


def render_section(name: str, bullets: list[Bullet]) -> str:
    lines = [f"## {name}", ""]
    for bullet in bullets:
        pr_refs = " ".join(f"(#{n})" for n in sorted(bullet.prs))
        lines.append(f"- {bullet.text} {pr_refs}".rstrip())
    return "\n".join(lines)


def render_full_changelog(commits) -> str:
    lines = ["## Full Changelog", ""]
    for commit in commits:
        if not is_changelog_worthy(commit):
            continue
        lines.append(f"* {commit_subject_with_pr(commit)}")
    return "\n".join(lines)


def assemble_notes(
    *,
    project: str,
    head: str,
    base: str,
    repo_url: str,
    curated: CuratedNotes,
    commits,
) -> str:
    blocks = [f"# {project} {head} Release Notes", curated.summary]
    if curated.breaking_changes:
        blocks.append(render_section("Breaking Changes", curated.breaking_changes))
    if curated.features:
        blocks.append(render_section("Features", curated.features))
    if curated.fixes:
        blocks.append(render_section("Fixes", curated.fixes))
    if curated.performance:
        blocks.append(render_section("Performance", curated.performance))
    blocks.append(render_full_changelog(commits))
    blocks.append(f"**Full diff:** {repo_url}/compare/{base}...{head}")
    return "\n\n".join(blocks) + "\n"


ALLOWED_SECTIONS = ("Breaking Changes", "Features", "Fixes", "Performance", "Full Changelog")
SECTION_ORDER = {name: i for i, name in enumerate(ALLOWED_SECTIONS)}

TITLE_RE = re.compile(r"^# .+ Release Notes$")
SECTION_HEADER_RE = re.compile(r"^## (.+)$")
CURATED_BULLET_RE = re.compile(r"^- \S.*$")
CHANGELOG_BULLET_RE = re.compile(r"^\* .+$")
DIFF_LINK_RE = re.compile(
    r"^\*\*Full diff:\*\* https://github\.com/[^/\s]+/[^/\s]+/compare/\S+\.\.\.\S+$"
)


def validate(text: str) -> list[str]:
    """Return a list of validation error messages. Empty list means valid."""
    errors: list[str] = []

    if not text.endswith("\n"):
        errors.append("document must end with a trailing newline")

    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]

    if len(lines) < 7:
        errors.append(f"document too short ({len(lines)} lines, expected at least 7)")
        return errors

    if not TITLE_RE.match(lines[0]):
        errors.append(f"line 1: title must match '# <project> <version> Release Notes', got: {lines[0]!r}")
    if lines[1] != "":
        errors.append(f"line 2: expected blank line after title, got: {lines[1]!r}")
    if not lines[2].strip():
        errors.append("line 3: summary line is blank")
    if lines[3] != "":
        errors.append(f"line 4: expected blank line after summary, got: {lines[3]!r}")

    section_starts: list[tuple[int, str]] = []
    diff_link_found = False
    for i, line in enumerate(lines):
        m = SECTION_HEADER_RE.match(line)
        if m:
            section_starts.append((i, m.group(1)))
        elif DIFF_LINK_RE.match(line):
            diff_link_found = True

    if not diff_link_found:
        errors.append("missing or malformed '**Full diff:** https://github.com/<owner>/<repo>/compare/<base>...<head>' line")

    last_idx = -1
    for line_idx, name in section_starts:
        if name not in SECTION_ORDER:
            errors.append(
                f"line {line_idx+1}: unknown section '{name}' (allowed: {', '.join(ALLOWED_SECTIONS)})"
            )
            continue
        idx = SECTION_ORDER[name]
        if idx <= last_idx:
            errors.append(
                f"line {line_idx+1}: section '{name}' is out of order "
                f"(expected: {' -> '.join(ALLOWED_SECTIONS)})"
            )
        last_idx = idx

    if not any(name == "Full Changelog" for _, name in section_starts):
        errors.append("missing required section '## Full Changelog'")

    for idx, (start_line, name) in enumerate(section_starts):
        next_start = section_starts[idx + 1][0] if idx + 1 < len(section_starts) else len(lines)
        body = lines[start_line + 1 : next_start]
        if not body or body[0] != "":
            errors.append(f"line {start_line+2}: expected blank line after '## {name}'")
        bullet_re = CHANGELOG_BULLET_RE if name == "Full Changelog" else CURATED_BULLET_RE
        bullet_count = 0
        for offset, line in enumerate(body[1:], start=2):
            if line == "":
                continue
            if DIFF_LINK_RE.match(line):
                continue
            if not bullet_re.match(line):
                expected = "* <subject>" if name == "Full Changelog" else "- <text> [optional (#NNN) refs]"
                errors.append(
                    f"line {start_line+offset}: '{name}' bullet doesn't match '{expected}', got: {line!r}"
                )
            else:
                bullet_count += 1
        if bullet_count == 0:
            errors.append(f"section '## {name}' has no bullets")

    return errors


def check_pi_installed() -> None:
    if shutil.which("pi") is None:
        raise SystemExit(
            "Error: 'pi' CLI not found on PATH. Install via:\n"
            "  npm install -g @earendil-works/pi-coding-agent\n"
            "or:\n"
            "  curl -fsSL https://pi.dev/install.sh | sh"
        )


def ensure_local_clone(repo_full: str, head_ref: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    repo_dir = CACHE_DIR / repo_full.replace("/", "__")
    if not (repo_dir / ".git").exists():
        print(f"Cloning {repo_full} to {repo_dir} (first time, may take a while)...", file=sys.stderr)
        subprocess.run(
            ["git", "clone", f"https://github.com/{repo_full}.git", str(repo_dir)],
            check=True,
        )
    else:
        subprocess.run(
            ["git", "-C", str(repo_dir), "fetch", "--all", "--tags", "--quiet"],
            check=True,
        )
    subprocess.run(
        ["git", "-C", str(repo_dir), "checkout", "--quiet", "--force", head_ref],
        check=True,
    )
    return repo_dir


def use_existing_repo(repo_dir_arg: str) -> Path:
    repo_dir = Path(repo_dir_arg).expanduser().resolve()
    if not (repo_dir / ".git").exists():
        raise SystemExit(f"Error: --repo-dir {repo_dir} is not a git repository.")
    return repo_dir


PI_PROMPT_TEMPLATE = """You are exploring a git repository to understand the user-visible impact of a discrete change in an upcoming release.

The change is grouped under {unit_label}.

{pr_context}

Commits in this unit:
{commit_list}

Your task:
1. Run `git show <sha>` (or `git show --stat <sha>` for large diffs) on each commit to see the actual diff. Use `git log --oneline` if you need to navigate.
2. If the diff references symbols or files whose purpose is unclear, read them with the `read` tool to understand them.
3. Output a 2-4 sentence summary of the user-visible impact, in plain language.

Constraints:
- Do not restate commit messages verbatim.
- Focus on what end users (or API/CLI consumers) can observe: new behavior, changed APIs, fixed bugs, performance.
- If the change is purely internal (refactor, test scaffolding, CI), say so concisely in one sentence.
- Do not modify any files.

Output the summary as plain prose. No preamble, no markdown headings.
"""


def explore_unit_with_pi(
    *,
    repo_dir: Path,
    unit_label: str,
    pr_context: str,
    commit_list: str,
    openrouter_key: str,
) -> str:
    prompt = PI_PROMPT_TEMPLATE.format(
        unit_label=unit_label,
        pr_context=pr_context,
        commit_list=commit_list,
    )
    cmd = [
        "pi", "-p",
        "--no-session",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        "--tools", "read,bash,grep,find,ls",
        "--provider", "openrouter",
        "--model", PI_MODEL,
        prompt,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(repo_dir),
            env={**os.environ, "OPENROUTER_API_KEY": openrouter_key},
            timeout=PI_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return f"(pi exploration timed out after {PI_TIMEOUT_SEC}s)"
    if result.returncode != 0:
        return f"(pi exploration failed: {result.stderr.strip()[:200]})"
    return result.stdout.strip() or "(pi returned empty output)"


def run_pi_explorations(
    *,
    repo_dir: Path,
    changes: dict,
    orphans: list,
    openrouter_key: str,
) -> dict:
    """Returns a dict keyed by PR number (int) for PR units and commit SHA (str) for orphans."""
    units: list[dict] = []
    for pr_number, change in changes.items():
        pr = change["pr"]
        commit_list = "\n".join(
            f"- {c.sha[:8]}: {c.message.splitlines()[0]}" for c in change["commits"]
        )
        pr_context = f"PR #{pr.number}: {pr.title}"
        if pr.body:
            pr_context += f"\n\n{pr.body}"
        units.append({
            "key": pr.number,
            "unit_label": f"PR #{pr.number}",
            "pr_context": pr_context,
            "commit_list": commit_list,
        })
    for commit in orphans:
        sha = commit.sha
        units.append({
            "key": sha,
            "unit_label": f"a direct commit ({sha[:8]})",
            "pr_context": "(no PR description available)",
            "commit_list": f"- {sha[:8]}: {commit.commit.message.splitlines()[0]}",
        })

    if not units:
        return {}

    summaries: dict = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=PI_MAX_PARALLEL) as exe:
        futures = {
            exe.submit(
                explore_unit_with_pi,
                repo_dir=repo_dir,
                unit_label=u["unit_label"],
                pr_context=u["pr_context"],
                commit_list=u["commit_list"],
                openrouter_key=openrouter_key,
            ): u["key"]
            for u in units
        }
        for fut in concurrent.futures.as_completed(futures):
            key = futures[fut]
            try:
                summaries[key] = fut.result()
            except Exception as e:
                summaries[key] = f"(pi exploration error: {e})"
    return summaries


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate AI-powered release notes.")
    parser.add_argument(
        "--base",
        required=True,
        help="Previous release tag (e.g. v1.6.1), or 'auto' to detect the latest published release.",
    )
    parser.add_argument(
        "--head",
        required=True,
        help="New release tag or commit SHA to compare against.",
    )
    parser.add_argument(
        "--project-name",
        help="Project name shown in the title. Defaults to the GitHub repo name.",
    )
    parser.add_argument(
        "--output",
        help="Path to write the release notes to. Prints to stdout if omitted.",
    )
    parser.add_argument(
        "--explore",
        action="store_true",
        help="Run the 'pi' coding agent over each PR (and orphan commit) before summarizing, so the LLM has deeper context than commit messages alone. Requires 'pi' on PATH; clones the repo to ~/.cache/release-notes-pls (override with --repo-dir).",
    )
    parser.add_argument(
        "--repo-dir",
        help="Path to an existing local clone of the target repo. Used only with --explore; skips the cache clone and fetch. Working tree is left untouched - pi reads commits via 'git show' rather than checking out.",
    )
    parser.add_argument("repo", help="GitHub repository (owner/repo).")
    args = parser.parse_args()

    github_token = os.environ.get("GITHUB_TOKEN")
    api_key = os.environ.get("OPENROUTER_API_KEY")

    if not github_token:
        print("Error: GITHUB_TOKEN environment variable must be set.", file=sys.stderr)
        sys.exit(1)

    if not api_key:
        print("Error: OPENROUTER_API_KEY environment variable must be set.", file=sys.stderr)
        sys.exit(1)

    gh = Github(auth=Auth.Token(github_token))
    repo = gh.get_repo(args.repo)

    base = resolve_base(args.base, repo)
    head = args.head
    project = args.project_name or repo.name

    commits = list(repo.compare(base, head).complete().commits)
    if not commits:
        raise SystemExit(f"Error: no commits found between {base} and {head}.")

    release_type = infer_release_type(commits)
    changes = collect_pr_changes(commits)

    pi_summaries: dict | None = None
    if args.explore:
        check_pi_installed()
        if args.repo_dir:
            repo_dir = use_existing_repo(args.repo_dir)
        else:
            repo_dir = ensure_local_clone(args.repo, head)
        orphans = get_orphan_commits(commits, changes)
        print(
            f"Running pi exploration on {len(changes)} PR(s) and {len(orphans)} orphan commit(s)...",
            file=sys.stderr,
        )
        pi_summaries = run_pi_explorations(
            repo_dir=repo_dir,
            changes=changes,
            orphans=orphans,
            openrouter_key=api_key,
        )

    changes_text = format_changes_for_prompt(commits, changes, pi_summaries)

    client = OpenAI(api_key=api_key, base_url=OPENROUTER_BASE_URL)
    curated = generate_curated_notes(client, project, release_type, changes_text)

    output = assemble_notes(
        project=project,
        head=head,
        base=base,
        repo_url=repo.html_url,
        curated=curated,
        commits=commits,
    )

    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
    else:
        print(output)

    errors = validate(output)
    if errors:
        print("Generated release notes failed validation:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
