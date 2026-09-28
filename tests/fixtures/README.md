# Test fixtures

Real captures (recorded from live services, then scrubbed of personal paths):

- `stream/*.jsonl`: `claude -p --output-format stream-json --verbose` transcripts from Claude Code 2.1.283.
- `places/error_invalid_key.json`, `youtube/error_invalid_key.json`: responses to a fake key.

Hand-written from Google's documented error format (not recorded, because triggering them
needs a real key in a specific state):

- `places/error_quota.json`, `places/error_service_disabled.json`, `youtube/error_quota.json`.
