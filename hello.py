import os
import requests
import argparse
import sys
from github import Auth, Github
from openai import OpenAI


def get_pull_requests_and_commits(repo, current_tag, previous_tag, github_token):
    """
    Fetches all commits and associated pull requests between two Git tags.

    Args:
        repo (str): The repository name, e.g., 'owner/repo'.
        current_tag (str): The current release tag (e.g., 'v1.2.0').
        previous_tag (str): The previous release tag (e.g., 'v1.1.0').
        github_token (str): A GitHub access token with 'repo' scope.

    Returns:
        dict: A dictionary of changes, where keys are PR numbers and values are
              dictionaries containing PR info and a list of commits.
    """

    api_url = f"https://api.github.com/repos/{repo}"
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }

    # 1. Compare the two tags to get all commits in the range.
    # The 'compare' endpoint is perfect for this.
    try:
        compare_url = f"{api_url}/compare/{previous_tag}...{current_tag}"
        print(f"Comparing tags: {compare_url}", file=sys.stderr)
        response = requests.get(compare_url, headers=headers, timeout=30)
        response.raise_for_status()
        compare_data = response.json()

    except requests.exceptions.HTTPError as err:
        print(f"HTTP Error: {err}", file=sys.stderr)
        print(
            "Could not compare tags. Please ensure tags exist and the token has 'repo' scope.",
            file=sys.stderr,
        )
        return {}

    except requests.exceptions.RequestException as err:
        print(f"An error occurred: {err}", file=sys.stderr)
        return {}

    commits = compare_data.get("commits", [])
    if not commits:
        print("No commits found between the specified tags.", file=sys.stderr)
        return {}

    print(
        f"Found {len(commits)} commits between {previous_tag} and {current_tag}.",
        file=sys.stderr,
    )

    # 2. For each commit, try to find its associated PR and gather data.
    changes = {}

    for commit in commits:
        commit_sha = commit["sha"]
        commit_message = commit["commit"]["message"]

        # A. Find the PR associated with this commit
        try:
            # The 'pulls' endpoint can be used to list PRs associated with a commit SHA.
            pulls_url = f"{api_url}/commits/{commit_sha}/pulls"
            pulls_response = requests.get(pulls_url, headers=headers, timeout=15)
            pulls_response.raise_for_status()
            pulls_data = pulls_response.json()

        except requests.exceptions.HTTPError as err:
            print(
                f"Warning: Could not fetch PRs for commit {commit_sha[:7]}. {err}",
                file=sys.stderr,
            )
            pulls_data = []

        pr_info = None
        if pulls_data:
            # Typically, a commit is associated with a single merged PR.
            # We'll take the first one found.
            pr = pulls_data[0]
            pr_info = {
                "number": pr["number"],
                "title": pr["title"],
                "body": pr["body"] if pr["body"] else "No description provided.",
                "url": pr["html_url"],
            }
            pr_key = pr["number"]

        else:
            # If no PR is found (e.g., a direct commit to main), use the commit itself.
            pr_info = {
                "number": "N/A",
                "title": f"Commit: {commit_message.splitlines()[0]}",
                "body": commit_message,
                "url": commit["html_url"],
            }
            pr_key = commit_sha

        # B. Group commits by PR
        if pr_key not in changes:
            changes[pr_key] = {"pr_info": pr_info, "commits": []}

        changes[pr_key]["commits"].append(
            {"sha": commit_sha, "message": commit_message}
        )

    return changes


def generate_notes_with_ai(changes_data, ai_api_key):
    """
    Generates release notes using an AI model.
    """
    client = OpenAI(api_key=ai_api_key)

    # Convert the structured changes into a string format for the AI prompt.
    formatted_changes = ""
    for pr_key, data in changes_data.items():
        pr_info = data["pr_info"]
        formatted_changes += f"--- PR #{pr_info['number']} ---\n"
        formatted_changes += f"Title: {pr_info['title']}\n"
        formatted_changes += f"Description: {pr_info['body']}\n"
        formatted_changes += "Associated Commits:\n"
        for commit in data["commits"]:
            formatted_changes += (
                f"- {commit['message'].splitlines()[0]} ({commit['sha'][:7]})\n"
            )
        formatted_changes += "\n"

    prompt = f"""
    You are an expert technical writer for a software project. Your task is to generate clear, concise, and user-friendly release notes based on the following list of changes (Pull Request descriptions and commit messages).

    Here are the changes:
    {formatted_changes}

    Generate the notes in Markdown format.
    """

    # Call the AI model
    response = client.chat.completions.create(
        model="gpt-4o",
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

    if not github_token:
        print("Error: GITHUB_TOKEN environment variable must be set.", file=sys.stderr)
        sys.exit(1)
    if not ai_api_key:
        print(
            "Error: OPENAI_API_KEY environment variable must be set.", file=sys.stderr
        )
        sys.exit(1)

    # 1. Fetch relevant PRs and commits
    changes = get_pull_requests_and_commits(
        args.repo, args.current_tag, args.previous_tag, github_token
    )

    if not changes:
        print("No changes to process. Exiting.", file=sys.stderr)
        sys.exit(0)

    # 2. Generate notes with AI
    print("Sending data to AI model for summarization...", file=sys.stderr)
    release_notes = generate_notes_with_ai(changes, ai_api_key)

    # 3. Print the final release notes to stdout for the GitHub Action to capture
    print(release_notes)
