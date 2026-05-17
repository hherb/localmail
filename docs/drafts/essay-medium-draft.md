# I Don't Trust My AI Agent With My Inbox. So I Built a Wall Between Them.

**Subtitle:** `localmail` is a read-only mirror of every email account I own — and it is the substrate my agents actually talk to.

---

Let me set the scene.

I am a physician who builds tools. One of those tools is [`hhagent`](https://github.com/hherb/hhagent), a personal AI agent that I want to do the obvious things: triage the morning inbox, dig out the invoice from a vendor I last spoke to in 2019, summarise a long clinical thread before I open it, find the paper a colleague mentioned three accounts ago. The models are finally good enough for this. The blocker, in 2026, is not capability.

The blocker is trust.

Because the moment you hand an LLM agent IMAP credentials, you have handed it the keys to:

- delete a thread you needed for a clinical case
- "helpfully" reply to a patient on your behalf
- flag your accountant's draft as read and quietly hide it
- read mail it has no business reading — a shared family account, the practice bookkeeping mailbox, a partner's account on the same server

OAuth scopes help. Provider-side label restrictions help. But once an agent process holds IMAP read+write, "scope" is the wrong abstraction. The right abstraction is much blunter: **never let the agent touch IMAP at all.**

That conviction is the entire reason `localmail` exists.

## What `localmail` is

`localmail` is a small Python daemon that mirrors one or more IMAP accounts — password auth or Gmail OAuth2, equally — into a local PostgreSQL database. Per account, it runs an `IDLE` loop on the inbox and a periodic poll on the other folders. New messages get parsed, deduped, stored. Attachments are written to disk in a content-addressable tree, keyed by SHA-256 of their bytes, so the same PDF received fifteen times across four accounts lives on disk exactly once.

The defining property is what `localmail` **never** does:

> `localmail` is read-only with respect to upstream. It does not delete, modify, send, flag, or move a single message on any IMAP server, ever.

There is no write path. Not "a write path that is currently disabled." Not "a write path that requires a flag." The IMAP client object exposes no verbs that mutate server state. If you wanted `localmail` to send mail tomorrow, you would have to write the code first.

That is the wall I wanted between my agent and my inboxes.

## The agent is downstream — and downstream is a different country

Once mail lives in Postgres, the threat model collapses. The agent — `hhagent`, or any other consumer — talks to a local API (an MCP server, in `hhagent`'s case). The API has its own surface area, its own permissions, its own filters. It can serve a thread. It cannot send one. It can search a folder. It cannot empty one. The IMAP credentials never leave the machine running the sync daemon, and the agent has no path to them.

This is not a security theatre detail. It is the difference between two architectures:

1. **Agent ↔ IMAP directly.** Every capability the protocol exposes is a capability the model can invoke. Refusal lives inside the model's head. One jailbroken prompt, one prompt-injected email body, one over-eager tool call, and the damage is real and upstream.
2. **Agent ↔ filtered API ↔ Postgres ← IMAP (read-only).** The model can be as confused as it likes. The wall is structural. There is no token, no scope, no clever phrasing that turns `SELECT … FROM messages` into `DELETE FROM imap`.

I do not want my agent's safety to depend on its judgement. I want it to depend on the fact that the dangerous verbs were never wired up.

## Per-agent filters, served at the door

A read-only substrate is not enough on its own, because not every agent should see every message. The bookkeeping account contains things my triage agent has no business reading. A shared family account has mail addressed to other people. A coaching agent looking at my correspondence should not be looking at patient consultations.

Because the agent does not query Postgres directly — it goes through the API — the API is where I enforce filters. An agent identity carries an allowlist of accounts, folders, and (eventually) label-based predicates. The MCP tools the agent sees are bound to that identity. If the agent asks for "all messages from last Tuesday," the underlying query is silently constrained to the slice that agent is allowed to see, and there is no way to ask "but really, all of them."

Two consequences fall out of this design:

- **Prompt injection becomes much less interesting.** The worst a malicious email body can do is convince the agent to ask for something it cannot have. The API simply will not serve it.
- **Multiple agents can share one archive without sharing each other's blast radius.** A triage agent, a literature agent, a billing agent, a shared family-calendar agent — same `localmail` install, different filters, no cross-talk.

This is what makes `localmail` a *necessary* dependency for `hhagent`, not a *convenient* one. The agent's value is bounded by the safety story underneath it. Without the wall, I would not have turned the agent on.

## The search problem nobody markets

The second reason `localmail` exists is more selfish.

I have email accounts going back to the late 1990s. Some are still live. Some belong to providers that no longer exist. Some live on servers I have been meaning to decommission for half a decade. The messages I actually need — the consent form from a 2014 study, the contract clause from a 2019 software vendor, the lecture notes a mentor mailed me at an address I no longer own — are scattered across that mess.

No provider's web client searches across all of it. None of them ever will. Gmail will not search your old Fastmail account. Fastmail will not search your dead university mailbox. And none of them will search the `.mbox` you exported from a server that has not booted since 2017.

`localmail` collapses that. Every account, every folder, every imported historical archive lands in the same Postgres schema, with a single hybrid search on top of it:

- **Lexical retrieval** via PostgreSQL's built-in `tsvector` and `ts_rank_cd`, weighted across subject / from / body. No third-party extension required.
- **Vector retrieval** via `pgvector` HNSW over chunk embeddings (default model: EmbeddingGemma-300M via fastembed, with an Apache-2.0 alternative one config line away).
- **RRF fusion** of the two arms, an optional rerank pass, and a small DSL on top — `invoice has:attachment after:2025-01-01 from:anna`.

The acceptance harness gates Phase 1 at recall@20 ≥ 80% and MRR@20 ≥ 0.5 across English, German, Spanish, and Japanese, on a synthetic multilingual corpus. It is not magic — it is a small set of well-understood components, glued together, evaluated honestly. But it works across every account I own, including the dead ones, and that is something I have wanted for fifteen years.

## A backup that is also a browsable archive

The third reason is the one that surprised me by becoming load-bearing.

Once you mirror IMAP into a content-addressable store, you have, almost by accident, the kind of backup most people stop thinking about until the morning their provider locks them out. Specifically:

- **Provenance is preserved.** A message in INBOX and three Gmail labels produces one row in `messages` and four rows in `message_labels`. The same Message-Id on a *different* account is a separate row — because, semantically, it is a separate copy with a different envelope. You can answer "did I receive this on my work address or my personal one?" years after the fact.
- **Attachments survive renames.** The PDF lives on disk under its SHA-256 hash, but the JSONB column on each message remembers the filename *that message* used. If you restore an attachment, it comes out with the name it had when it landed in your inbox — not the deduped hash, not some sanitised slug.
- **Shared accounts work.** A practice's bookkeeping mailbox, a household's logistics account, a research lab's submissions address — all of them sync into the same archive with full per-account provenance. Multiple humans can search the same shared mailbox through their respective agents, with their respective per-agent filters, without anyone losing the audit trail of where a message originally landed.
- **Dead servers stop being terrifying.** Once a server is mirrored, you can decommission it knowing the mail is local, searchable, and not going anywhere. The architecture explicitly anticipates ingesting "decades of email from servers that are no longer reachable" — historical imports are first-class, not an afterthought.

The archive is also browsable. A small Tauri-based desktop client (currently in early development) gives a Gmail-like three-pane view over the whole thing, with the cross-account search wired in. It is — deliberately, visibly — a read-only browser. It cannot send, cannot delete, cannot reply. It is honest about what it is.

## What I want you to take from this

If you are building agents in 2026, the temptation is to give them everything. Full IMAP. Full calendar write. Full Slack post. The models are good, the demos are impressive, and the failure modes feel hypothetical until they aren't.

I would gently suggest the opposite discipline:

- **Put a read-only mirror between the agent and the system of record.** Mail, calendars, code repositories, EHRs. The agent does not need the live system. It needs *answers from* the live system.
- **Enforce filters at the door, not in the prompt.** Allowlists in code beat refusals in the model.
- **Make safety structural, not aspirational.** "The agent is instructed not to delete" is not the same architecture as "the agent's tools have no delete verb."

`localmail` is my answer to that for email. It is open source, BSD-licensed, Python 3.12+, runs anywhere Postgres runs, and is happily mirroring six of my accounts as I write this.

- Repo: [github.com/hherb/localmail](https://github.com/hherb/localmail)
- Companion agent: [github.com/hherb/hhagent](https://github.com/hherb/hhagent)

It is not finished. The GUI is in early implementation. Attachment-content search (Phase 2) is in flight. The MCP surface for agents is the next thing I land. But the core property — the wall — has been there from commit one, and it is the reason I trust the rest of the stack enough to keep building on top of it.

I want my agent to read my email. I do not want my agent to *touch* my email. `localmail` is the difference.
