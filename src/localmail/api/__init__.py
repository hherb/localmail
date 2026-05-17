"""Transport-free API library for the localmail GUI/MCP server.

Public service functions live in submodules:
- auth: login, logout, refresh, whoami
- accounts: list accounts and folders with capabilities
- messages: get message detail, full headers, raw RFC822
- attachments: stream blob bytes, extracted text
- search: hybrid search wrapping localmail.search.Searcher
- sanitize: bleach-based HTML sanitizer with cid: rewriting
- errors: typed exceptions
"""
