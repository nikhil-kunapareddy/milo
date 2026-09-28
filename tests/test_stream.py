"""The stream parser, run over real `claude -p --output-format stream-json` transcripts."""

import json

from milo.backends.base import Error, Final, Progress, SessionStarted, ToolCall, ToolResult
from milo.backends.stream import StreamParser
from tests.conftest import FIXTURES


def parse(name: str) -> list:
    parser = StreamParser()
    events = []
    for line in (FIXTURES / "stream" / name).read_text().splitlines():
        events += parser.feed(line)
    return events + parser.finish(0, "")


def of(events: list, kind: type) -> list:
    return [e for e in events if isinstance(e, kind)]


def test_search_and_fetch_run():
    events = parse("research_search_fetch.jsonl")
    session = "57d42e17-39bc-44a7-bfc8-00be768c742d"

    assert events[0] == SessionStarted(session)
    calls = of(events, ToolCall)
    assert [(c.name, c.summary) for c in calls] == [
        ("WebSearch", "Searching the web: best ramen restaurants in Boston"),
        ("WebFetch", "Reading bostonmagazine.com"),
    ]
    assert [c.kind for c in calls] == ["search", "read"]
    search, fetch = of(events, ToolResult)
    assert (search.kind, fetch.kind) == ("search", "read")
    assert search.name == "WebSearch" and search.ok
    assert len(search.urls) == 9
    assert "https://www.bostonmagazine.com/restaurants/best-ramen-in-boston/" in search.urls
    assert fetch.name == "WebFetch" and fetch.ok
    assert fetch.urls == ["https://www.bostonmagazine.com/restaurants/best-ramen-in-boston/"]
    assert "Ebisuya Noodle House" in fetch.content

    [final] = of(events, Final)
    assert final.session_id == session
    assert final.text.startswith("Boston Magazine's guide lists 15")
    assert final.cost_usd and final.cost_usd > 0
    assert not of(events, Error)


def test_search_urls_come_from_structured_results_not_summary_prose():
    [search] = of(parse("search_only.jsonl"), ToolResult)
    # Every URL counted must be one of the search hits, never something in the prose summary.
    structured = json.loads(
        next(
            line
            for line in (FIXTURES / "stream" / "search_only.jsonl").read_text().splitlines()
            if '"tool_use_result"' in line
        )
    )["tool_use_result"]
    hits = {item["url"] for block in structured["results"] if isinstance(block, dict)
            for item in block["content"]}  # fmt: skip
    assert search.urls and set(search.urls) == hits


def test_final_text_also_streams_as_progress():
    events = parse("search_only.jsonl")
    progress = of(events, Progress)
    [final] = of(events, Final)
    assert progress[-1].text == final.text.strip()


def test_failed_fetch_is_not_an_observed_url():
    events = parse("fetch_404.jsonl")
    [fetch] = of(events, ToolResult)
    assert fetch.name == "WebFetch"
    assert not fetch.ok
    assert fetch.urls == []
    assert "404" in fetch.content


def test_resume_keeps_the_session_id():
    events = parse("resume_followup.jsonl")
    [final] = of(events, Final)
    assert of(events, SessionStarted)[0].session_id == final.session_id
    assert "bostonmagazine.com" in final.text


def test_structured_output_is_captured_and_its_tool_hidden():
    events = parse("intake_structured_output.jsonl")
    [final] = of(events, Final)
    assert final.structured == {"what": "ramen", "where": "Fenway", "on_topic": True}
    assert not of(events, ToolCall)  # StructuredOutput is plumbing, not research
    assert not of(events, ToolResult)


def test_not_logged_in_is_an_error_despite_success_subtype():
    events = parse("not_logged_in.jsonl")
    [error] = of(events, Error)
    assert error.kind == "not_logged_in"
    assert "claude auth login" in error.message
    assert not of(events, Final)


def test_max_turns():
    events = parse("max_turns.jsonl")
    [error] = of(events, Error)
    assert error.kind == "max_turns"
    assert error.session_id == "4ff32be7-d702-424b-a78c-e7bf045d0639"


def test_process_dying_without_a_result_is_a_crash():
    parser = StreamParser()
    first = (FIXTURES / "stream" / "search_only.jsonl").read_text().splitlines()[0]
    parser.feed(first)
    [error] = parser.finish(1, "some warning\nFatal: out of memory\n")
    assert error.kind == "crashed"
    assert "Fatal: out of memory" in error.message
    assert error.session_id == parser.session_id


def test_junk_lines_are_ignored():
    parser = StreamParser()
    assert parser.feed("") == []
    assert parser.feed("not json") == []
    assert parser.feed('["a list"]') == []
    assert parser.feed('{"type": "rate_limit_event"}') == []
    assert parser.feed('{"type": "system", "subtype": "hook_started"}') == []


def test_api_error_carries_the_cli_message():
    parser = StreamParser()
    result = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "terminal_reason": "api_error",
        "result": "API Error: 529 Overloaded",
    }
    [error] = parser.feed(json.dumps(result))
    assert error.kind == "api_error"
    assert "529 Overloaded" in error.message


def test_search_links_fallback_when_structured_data_is_missing():
    parser = StreamParser()
    call = {"type": "tool_use", "id": "t1", "name": "WebSearch", "input": {"query": "q"}}
    parser.feed(json.dumps({"type": "assistant", "message": {"content": [call]}}))
    text = (
        'Web search results for query: "q"\n\nLinks: [{"title": "A", "url": "https://a.example/"}]'
    )
    block = {"type": "tool_result", "tool_use_id": "t1", "content": text}
    [result] = parser.feed(json.dumps({"type": "user", "message": {"content": [block]}}))
    assert result.urls == ["https://a.example/"]
