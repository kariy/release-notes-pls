import os
import sys
import argparse
from github import Github, Auth
from openai import OpenAI


def get_pull_requests_and_commits(repo, current_tag, previous_tag, github_token):
    # Use GitHub API to fetch PRs merged between previous_tag and current_tag
    # and their associated commits.
    # This will be the most complex part of the script, handling pagination,
    # filtering, and gathering descriptions.
    # Example: https://api.github.com/repos/{owner}/{repo}/pulls?state=closed&base={branch}&sort=updated&direction=desc
    # You'll need to filter these by merge date/commit SHA to match your tag range.
    # Also fetch commit messages for granular changes if needed.
    # Return a structured list of changes.
    pass  # Placeholder for actual implementation


def generate_notes_with_ai(changes_data, ai_api_key):
    client = OpenAI(api_key=ai_api_key)  # Or your chosen AI client

    # Construct a comprehensive prompt
    prompt = f"""
    You are an expert technical writer for a software project. Your task is to generate clear, concise, and user-friendly release notes based on the following list of changes (Pull Request titles, descriptions, and commit messages).

    Focus on describing *what* has changed from the user's perspective, *why* it's important, and *how* it benefits them. Categorize changes under "New Features", "Bug Fixes", "Improvements", and "Documentation/Other".

    Here are the changes:
    {changes_data}

    Please generate the release notes in Markdown format.
    """

    # Call the AI model
    response = client.chat.completions.create(
        model="gpt-4o",  # Or another suitable model
        messages=[
            {
                "role": "system",
                "content": "You are a helpful assistant for generating release notes.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.7,
        max_tokens=1000,
    )
    return response.choices[0].message.content


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate AI-powered release notes.")
    parser.add_argument("--current-tag", required=True, help="Current release tag.")
    parser.add_argument("--previous-tag", required=True, help="Previous release tag.")
    parser.add_argument(
        "--repo", required=True, help="GitHub repository name (owner/repo)."
    )
    args = parser.parse_args()

    github_token = os.environ.get("GITHUB_TOKEN")
    ai_api_key = os.environ.get("OPENAI_API_KEY")

    if not github_token or not ai_api_key:
        print(
            "Error: GITHUB_TOKEN and OPENAI_API_KEY environment variables must be set.",
            file=sys.stderr,
        )
        sys.exit(1)

    # 1. Fetch relevant PRs and commits
    changes = get_pull_requests_and_commits(
        args.repo, args.current_tag, args.previous_tag, github_token
    )

    # 2. Format changes for the AI (e.g., concatenate PR titles/descriptions and key commit messages)
    formatted_changes = "\n".join(
        [
            f"- PR #{pr['number']}: {pr['title']}\n  Description: {pr['body']}\n  Commits: {', '.join(pr['commit_messages'])}"
            for pr in changes
        ]
    )

    # 3. Generate notes with AI
    release_notes = generate_notes_with_ai(formatted_changes, ai_api_key)

    print(release_notes)
