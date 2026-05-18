---
name: handoff
description: instructions what to do when ending a sessions
disable-model-invocation: true
allowed-tools: Bash(git *) Bash(cargo test *) Bash(uv run pytest *)
---
Before we end, write a NEXT_SESSION.md containing: 
(1) what we shipped this session with commit SHAs, 
(2) what's next with concrete acceptance criteria, 
(3) any open decisions or risks, 
(4) the exact commands needed to resume (cd, branch, test command). 
Save it to docs/handoffs/ with today's date.
