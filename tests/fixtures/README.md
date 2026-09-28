# Test fixtures

Real captures (recorded from live services, then scrubbed of personal paths):

- `stream/*.jsonl`: `claude -p --output-format stream-json --verbose` transcripts from Claude Code 2.1.283.
- `places/error_invalid_key.json`, `youtube/error_invalid_key.json`: responses to a fake key.

Hand-written from Google's documented error format (not recorded, because triggering them
needs a real key in a specific state):

- `places/error_quota.json`, `places/error_service_disabled.json`, `youtube/error_quota.json`.

Hand-written success responses, shaped like the documented API responses (names, sites, and
counts are made up; websites use `.example.com`):

- `places/text_search_ok.json` (reviews included, as Text Search returns them)
- `youtube/search_ok.json`, `youtube/videos_ok.json`
