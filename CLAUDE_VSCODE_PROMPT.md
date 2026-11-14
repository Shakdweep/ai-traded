Paste everything below the line into Claude in VS Code, with the unzipped `ai-traded` folder open.

---

This folder is "AI Traded", a Python (FastAPI + SQLite) and vanilla-JS web app for analysing Indian stocks (NSE/BSE) with paper trading only. Please publish it to my GitHub as a new PUBLIC repository so I can share the link with a friend for testing.

Steps:

1. Check the GitHub CLI is installed and logged in (`gh auth status`). If it isn't logged in, stop and tell me to run `gh auth login` myself.
2. Git needs an author identity for this repo only (not global). Use my GitHub username from `gh api user --jq .login`, and for the email use my GitHub no-reply address: `<id>+<login>@users.noreply.github.com`, where id comes from `gh api user --jq .id`. Set them with `git config user.name` / `git config user.email` inside this folder. Don't use my personal email.
3. Before committing, confirm that `.gitignore` excludes `.venv/`, `data/`, `.env` and `.claude/`. Scan the files for anything that looks like a secret (API keys, tokens, passwords) and show me anything you find before going on.
4. Run `git init -b main`, commit everything with the message "AI Traded: Indian-market analysis and paper-trading dashboard".
5. Create the repo and push: `gh repo create ai-traded --public --source . --push --description "Personal AI trading intelligence for NSE/BSE: scanner, explained ratings, trade plans, backtests and paper trading (analysis only, not investment advice)"`. If the name `ai-traded` is taken on my account, ask me for another name.
6. Give me the repository link, and a short message I can send my friend: what the app is, that it's for testing only (paper trading, not investment advice), and the 3-step "For testers" instructions from README.md (install Python 3.10+, Download ZIP, run `run.bat`).

Optional, only if I say yes: run the unit tests first with
`python -m venv .venv`, `.venv/Scripts/python -m pip install -r requirements.txt`, then
`.venv/Scripts/python -m unittest discover -s tests -t .` (23 tests should pass).
