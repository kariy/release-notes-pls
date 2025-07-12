import os
import argparse
import sys
from github import Auth, Github
import anthropic

def generate_prompt(formatted_changes):
	prompt = f"""
	You are an expert technical writer for a software project. Your task is to generate clear, concise, and user-friendly release notes based on the following list of changes (Pull Request descriptions and commit messages).

	Here are the changes: {formatted_changes}

	Generate the notes in this exact format in Markdown:

	```
	## <CHANGE CATEGORY>

	* <CHANGE_SUMMARY> (<PR_NUMBER>)
	```

	Valid change categories are: fixes, improvements

	If there are multiple PRs associated with a change, list the PR using the format `(<PR_NUMBER>)` side by side.
	"""

	return prompt

if __name__ == "__main__":
	parser = argparse.ArgumentParser(description="Generate AI-powered release notes.")
	parser.add_argument("--base", required=True, help="Base tag.")
	parser.add_argument("--head", required=True, help="Head tag.")
	parser.add_argument("repo", help="GitHub repository name (owner/repo).")
	args = parser.parse_args()

	repo = args.repo;
	base = args.base;
	head = args.head;

	github_token = os.environ.get("GITHUB_TOKEN")
	api_key = os.environ.get("API_KEY")

	if not github_token:
		print("Error: GITHUB_TOKEN environment variable must be set.", file=sys.stderr)
		sys.exit(1)

	if not api_key:
		print(
			"Error: OPENAI_API_KEY environment variable must be set.", file=sys.stderr
		)
		sys.exit(1)

	auth = Auth.Token(github_token)
	client = Github(auth=auth)

	repo = client.get_repo(repo)
	commits = repo.compare(base, head).complete().commits

	# (pr, commit)
	changes = {}

	for commit in commits:
		prs = commit.get_pulls()
		for pr in prs:
			id = pr.number
			changes[id] = { "pr": pr, "commits": [] }
			changes[id]["commits"].append(commit.commit)

	# Convert the structured changes into a string format for the AI prompt.
	formatted_changes = ""
	for change in changes.values():
		pr = change["pr"]
		commits = change["commits"]

		formatted_changes += f"\n--- PR #{pr.number} ---\n"
		formatted_changes += f"## Title\n{pr.title}\n"
		formatted_changes += f"## Description\n{pr.body}\n"
		formatted_changes += "## Commits:\n"

		for commit in commits:
			formatted_changes += (
				f"- {commit.message.splitlines()[0]}"
			)
			formatted_changes += "\n"

	prompt = generate_prompt(formatted_changes)

	client = anthropic.Anthropic(api_key=api_key)
	message = client.messages.create(
		model="claude-4-sonnet-20250514",
		max_tokens=1024,
		messages=[
			{"role": "user", "content": prompt}
		]
	)

	for text in message.content:
		print(text.text)
