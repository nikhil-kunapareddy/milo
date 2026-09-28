# Milo

**Marketing research for restaurants and food businesses, in your terminal.** Tell Milo what
you sell and where, and it comes back with competitors, review themes, gaps, five campaign
ideas, a 7-day content calendar, and sources you can check.

> **Demo GIF coming soon.**
<!-- To add it: record a session (e.g. with vhs or asciinema + agg), save it as docs/demo.gif,
     and replace this placeholder with ![Milo demo](docs/demo.gif) -->

Milo uses your own [Claude Code](https://code.claude.com/docs/en/setup) login to do the
research. It never calls an LLM API itself and never asks for an LLM API key.

## Install

You need Python 3.11+ and Claude Code, installed and logged in:

```sh
curl -fsSL https://claude.ai/install.sh | bash   # skip if you already have Claude Code
claude auth login

pipx install git+https://github.com/<user>/milo
milo doctor
```

## Optional keys: `milo setup`

Milo works without any keys: it researches the web through Claude Code. Two optional keys add
structured local data:

| Key | Adds | Where to get it |
|---|---|---|
| Google Places | Competitors with ratings, review counts, prices, and review text | [Get an API key](https://developers.google.com/maps/documentation/places/web-service/get-api-key), then enable **Places API (New)** for its project |
| YouTube Data API | Local food videos with view counts | [Get started](https://developers.google.com/youtube/v3/getting-started), then enable **YouTube Data API v3** |

```sh
milo setup    # paste each key or press Enter to skip; each one is checked before it's saved
milo doctor   # backend and key status, with keys masked (AIza…x9Q)
```

Keys are stored in `~/.config/milo/config.toml`, readable only by you (`0600`). The
environment variables `MILO_PLACES_KEY` and `MILO_YOUTUBE_KEY` override the file.

## Usage

A real session, run without any optional keys:

```
$ milo
Milo · marketing research for food businesses
Backend: Claude Code ✓ · Google Places – (not configured) · YouTube – (not configured)

What do you want to market, and where?  (e.g. "ramen in Boston")
> I run a ramen place near Fenway, want more students

Who is this for?  [1] New opening  [2] Existing restaurant  [3] Creator  [4] Chain adding a location  (Enter to skip)
> 2
– Google Places skipped (no key)
– YouTube skipped (no key)
→ Researching local marketing and best practices…
✓ Searched 30 times and read 31 pages (7m 53s)
✓ Verified 32 of 32 citations

[brief: market snapshot, competitors, review themes, content benchmarks, gaps,
 5 campaign ideas, 7-day content calendar, numbered sources]

Ask a follow-up, or /report, /sources, /exit
> which competitor has the weakest social presence?
✓ Verified 12 of 12 links
[answer: Santouka Back Bay's Instagram looks abandoned (latest visible posts from 2018)…]

> how do I fix a python import error?
That's outside what Milo covers. Ask about marketing ramen in Fenway, Boston: competitors, offers, content, or customers.

> /report
Saved ./milo-ramen-fenway-boston-2026-09-28.md
```

With a Google Places key, the collection step adds lines like `✓ Found 20 competitors (Google
Places)` and the competitor table gets ratings, review counts, and prices.

Research is thorough by design: a brief takes about 5 to 10 minutes. Follow-ups continue the
same research session and can search the web again.

| Command | What it does |
|---|---|
| `milo` | Start a new session |
| `milo resume [session-id]` | Reopen a saved session (the most recent if no id) and keep asking follow-ups |
| `milo setup` | Add or change the optional keys |
| `milo doctor` | Check Claude Code and your keys |
| `milo --backend codex` | Pick a backend (Codex isn't supported yet) |
| `/report` | Write the Markdown report to the current folder (never overwrites: `-2`, `-3`, …) |
| `/sources` | List every verified source so far |
| `/exit` | Save the session and quit (Ctrl-D works too) |
| `/help` | List commands |

Ctrl-C during research stops it and keeps the session. Press Enter to try again, or run
`milo resume` later. Sessions live in `~/.milo/sessions/<id>/session.json`.

## How it works

```
CLI shell (Typer + Rich)       input, rendering, slash commands
Session controller             explicit state machine: which inputs each state accepts
 ├─ Intake parser              free text → what, where, audience
 ├─ Collectors                 Google Places, YouTube: concurrent, never raise
 ├─ Backend adapter            the only code that knows about Claude Code
 ├─ Citation guard             checks every URL against what the session saw
 ├─ Session store              one JSON file per session
 └─ Report renderer            Jinja template → Markdown, from the stored session
```

Three design rules hold everywhere:

1. **Deterministic code owns the flow; the model owns the reasoning.** Session states, input
   checks, and the report layout are Python, not prompts. Session states:
   `INTAKE → AUDIENCE → COLLECTING → RESEARCHING → FOLLOW_UP ⟲ → ENDED`.
2. **API keys never enter the model's context.** Collectors call Google in Python and hand
   over only results. Keys go in request headers, never URLs, and are stripped from the
   environment of the `claude` process.
3. **Every citation is verified.** A URL can appear in a brief, answer, or report only if it
   came back in this session: a web search result, a page that fetched successfully, or a
   collector result. Anything else is removed ("Verified 11 of 12 citations, 1 removed").
   Competitor ratings, review counts, and prices come only from Google Places data, so a
   made-up number can't slip in.

Claude Code runs headless (`claude -p`) with only the web search and web fetch tools, in
safe mode (your hooks, plugins, CLAUDE.md, and MCP servers stay out), from an empty folder
per session, and without `--dangerously-skip-permissions`.

## Data coverage and limitations

- Every report ends with a **Data coverage** line, for example: `Competitor ratings and
  reviews: skipped (no Google Places key) · Video benchmarks: skipped (no YouTube key) · Web
  research: Claude Code ✓ (39 verified sources)`.
- A missing key, an invalid key, an exceeded quota, or a network error never stops a session:
  Milo says which source was skipped and why, and tells the model not to invent that data.
- Google returns at most 5 reviews per place, so review themes are directional.
- Google Places: one Text Search request per session (up to 20 places, reviews included),
  billed by Google on the Enterprise + Atmosphere SKU. YouTube: 2 quota units per session.
- Milo doesn't scrape Instagram or TikTok (their terms prohibit it). The model may mention
  public social pages it found through web search.
- Research uses your Claude Code plan's usage; a thorough brief takes several minutes.
- Claude Code keeps its own transcript of each research session under `~/.claude/projects/`,
  as it does for any session. Milo's copy is `~/.milo/sessions/<id>/session.json`.

## Roadmap

- Codex backend (the interface and a stub exist: `milo --backend codex`)
- MCP server so the agent can run live collector lookups during follow-ups
- Instagram Graph API for accounts the owner connects
- More collectors (events, delivery platforms, local press feeds)

## Development

```sh
uv venv --python 3.11 && uv pip install -e ".[dev]"
.venv/bin/pytest      # no test calls a real API or the real `claude` CLI
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Fixtures in `tests/fixtures/` include real Claude Code stream-json transcripts; see
`tests/fixtures/README.md` for which are recorded and which are hand-written.
