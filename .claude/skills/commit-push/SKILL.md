---
name: commit-push
description: Run tests, stage changes, write conventional commit, push to current branch
---
1. Run the project's test suite; abort if failures.
2. Run `git status` and `git diff` to summarize changes.
3. Stage relevant files and commit with a conventional message (feat:/fix:/docs:/refactor:).
4. Push to the current branch's upstream.
