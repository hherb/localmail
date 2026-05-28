"""Admin-only service layer for localmail.

Every public function here checks the caller's admin status at the service
boundary so it remains safe to import directly from a future MCP-admin or
scripting layer (no HTTP middleware required).
"""
