"""A01–A05 审查问题的离线回归；不访问上游 API。"""
import asyncio
from itertools import permutations
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.aggregator import Aggregator, IncrementalMerger, SourceResult
from app.cache import TTLCache
from app.enrich import OaEnricher
from app.merger import merge
from app.models import PaperRecord


@pytest.mark.parametrize("match_kind", ["doi", "fuzzy"])
def test_a01_ranking_preserves_incremental_indexes(match_kind):
    merger = IncrementalMerger()
    first = PaperRecord(
        source="a", title="Alpha research study", year=2020,
        doi="10.1000/a" if match_kind == "doi" else None, score=0.1,
    )
    second = PaperRecord(
        source="b", title="Beta unrelated work", year=2024,
        doi="10.1000/b", score=0.9,
    )
    merger.add_batch([first, second])
    original = list(merger.items)
    ranked = merger.ranked()
    assert ranked is not merger.items
    assert ranked == list(reversed(original))
    assert merger.items == original
    assert merger._by_year[2020] == {0}

    incoming = PaperRecord(
        source="c", title="Alpha research studys", year=2020,
        doi="10.1000/a" if match_kind == "doi" else None,
    )
    assert merger.add_batch([incoming]) == (0, 1)
    assert len(merger.items) == 2
    assert merger.items[0].sources == ["a", "c"]
    assert merger.items[1].sources == ["b"]
    assert [m.record.title for m in merger.ranked()] == [
        "Beta unrelated work", "Alpha research study"
    ]


@pytest.mark.parametrize("order", list(permutations(range(3))))
@pytest.mark.parametrize("path", ["batch", "incremental_batch", "incremental_separate"])
@pytest.mark.parametrize("same_doi", [False, True])
def test_a02_no_doi_record_cannot_bridge_conflicting_dois(order, path, same_doi):
    records = [
        PaperRecord(source="a", id="a1", title="Shared long research title"),
        PaperRecord(
            source="b", id="b1", title="Shared long research title", doi="10.1000/x"
        ),
        PaperRecord(
            source="c", id="c1", title="Shared long research title",
            doi="10.1000/x" if same_doi else "10.1000/y",
        ),
    ]
    ordered = [records[i] for i in order]
    if path == "batch":
        result = merge(ordered)
    else:
        merger = IncrementalMerger()
        batches = [ordered] if path == "incremental_batch" else [[r] for r in ordered]
        for batch in batches:
            merger.add_batch(batch)
            merger.ranked()
        result = merger.items

    assert len(result) == (1 if same_doi else 2)
    assert {m.record.doi for m in result} == (
        {"10.1000/x"} if same_doi else {"10.1000/x", "10.1000/y"}
    )
    assert {source for m in result for source in m.sources} == {"a", "b", "c"}
    assert {source: ident for m in result for source, ident in m.all_ids.items()} == {
        "a": "a1", "b": "b1", "c": "c1"
    }
    if not same_doi:
        assert all(not {"b", "c"}.issubset(m.sources) for m in result)


@pytest.fixture
def offline_aggregator(monkeypatch):
    """Replace only transport/registry; execute real selection, cache and merging."""
    import app.aggregator as module

    registry = {
        name: SimpleNamespace(name=name, label=name, enabled=True)
        for name in ("openalex", "europepmc", "openreview")
    }
    monkeypatch.setattr(module, "REGISTRY", registry)
    monkeypatch.setattr(module, "SEARCH_CACHE", TTLCache())
    monkeypatch.setattr(module, "_FANOUT_SEM", asyncio.Semaphore(4))
    monkeypatch.setattr(module, "get_shared_client", lambda: object())
    agg = Aggregator()

    async def run_one(client, cls, query, limit):
        return (
            SourceResult(name=cls.name, label=cls.label, ok=True, count=1),
            [PaperRecord(source=cls.name, title=query, doi="10.1000/result")],
        )

    agg._run_one = AsyncMock(side_effect=run_one)
    return agg


async def source_stats(agg, query, sources, path, enrich_oa=False):
    if path == "search":
        outcome = await agg.search(query, sources=sources)
        return [s.name for s in outcome.sources], outcome
    events = [
        e async for e in agg.search_stream(query, sources=sources, enrich_oa=enrich_oa)
    ]
    done = next(e["data"] for e in events if e["event"] == "done")
    return [s["name"] for s in done["sources"]], done


@pytest.mark.parametrize("writer", ["search", "stream"])
@pytest.mark.parametrize("reader", ["search", "stream"])
@pytest.mark.parametrize("status_kind", ["unknown", "skipped"])
def test_a03_cache_statistics_are_request_local(offline_aggregator, writer, reader, status_kind):
    agg = offline_aggregator
    query = "library" if status_kind == "unknown" else "\u56fe\u4e66\u9986"
    original_sources = ["openalex", "missing"] if status_kind == "unknown" else None
    excluded = ["missing"] if status_kind == "unknown" else ["europepmc", "openreview"]

    async def run():
        first_names, first = await source_stats(agg, query, original_sources, writer)
        assert first_names == excluded + ["openalex"]
        if writer == "search":
            first.sources[0].error = "caller mutation"
        for _ in range(2):
            names, response = await source_stats(agg, query, original_sources, reader)
            assert names == first_names
            if reader == "search":
                assert response.sources[0].error != "caller mutation"
            else:
                assert response["cached"] is True
                assert response["sources"][0]["error"] != "caller mutation"
        names, _ = await source_stats(agg, query, ["openalex"], reader)
        assert names == ["openalex"]
        assert agg._run_one.await_count == 1

    asyncio.run(run())


def test_a03_singleflight_does_not_share_unknown_source_status(offline_aggregator):
    agg = offline_aggregator

    async def run():
        first, second = await asyncio.gather(
            agg.search("shared", sources=["openalex", "missing"]),
            agg.search("shared", sources=["openalex"]),
        )
        assert [s.name for s in first.sources] == ["missing", "openalex"]
        assert [s.name for s in second.sources] == ["openalex"]
        assert agg._run_one.await_count == 1
        first.results[0].sources.append("mutation")
        assert second.results[0].sources == ["openalex"]
        assert not agg._inflight

    asyncio.run(run())


@pytest.mark.parametrize("status_kind", ["unknown", "skipped"])
def test_a03_enriched_stream_cache_has_only_queried_statistics(
    offline_aggregator, monkeypatch, status_kind
):
    monkeypatch.setattr("app.enrich.OaEnricher.enrich", AsyncMock(return_value={}))
    agg = offline_aggregator
    query = "library" if status_kind == "unknown" else "\u56fe\u4e66\u9986"
    sources = ["openalex", "missing"] if status_kind == "unknown" else None
    excluded = ["missing"] if status_kind == "unknown" else ["europepmc", "openreview"]

    async def run():
        names, _ = await source_stats(agg, query, sources, "stream", enrich_oa=True)
        assert names == excluded + ["openalex"]
        names, response = await source_stats(agg, query, sources, "stream", enrich_oa=True)
        assert names == excluded + ["openalex"]
        assert response["cached"] is True
        names, _ = await source_stats(agg, query, ["openalex"], "stream", enrich_oa=True)
        assert names == ["openalex"]
        assert agg._run_one.await_count == 1

    asyncio.run(run())


@pytest.mark.parametrize("path", ["search", "stream"])
@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        (None, ["openalex", "europepmc", "openreview"]),
        ([], []),
        (["openalex", "openalex"], ["openalex"]),
        (["openalex", "missing", "openalex", "missing"], ["missing", "openalex"]),
        (["missing"], ["missing"]),
    ],
)
def test_a04_source_selection_and_fanout(offline_aggregator, path, sources, expected):
    agg = offline_aggregator
    original = list(sources) if sources is not None else None

    async def run():
        names, _ = await source_stats(agg, "library", sources, path)
        assert names == expected
        queried = [call.args[1].name for call in agg._run_one.await_args_list]
        assert queried == [n for n in expected if n != "missing"]
        # 相同来源选择的缓存命中不能增加出站调用。
        names, _ = await source_stats(agg, "library", sources, path)
        assert names == expected
        assert agg._run_one.await_count == len(queried)
        assert sources == original

    asyncio.run(run())


@pytest.mark.parametrize("path", ["search", "stream"])
def test_a04_empty_mcp_source_string_does_not_fanout(offline_aggregator, path):
    from mcp_server import _clean_srcs

    async def run():
        for text in ("", " ", ", ,"):
            names, _ = await source_stats(offline_aggregator, "library", _clean_srcs(text), path)
            assert names == []
        offline_aggregator._run_one.assert_not_awaited()

    asyncio.run(run())


INVALID_OA_RESPONSES = [
    [], None, "not an object", {},
    {"is_oa": None}, {"is_oa": "false"}, {"is_oa": 1},
    {"is_oa": True, "best_oa_location": [{}]},
    {"is_oa": True, "best_oa_location": []},
    {"is_oa": True, "best_oa_location": "bad"},
    {"is_oa": True, "oa_status": []},
    {"is_oa": True, "best_oa_location": {"url_for_pdf": []}},
    {"is_oa": True, "best_oa_location": {"url_for_landing_page": {}}},
    {"is_oa": True, "best_oa_location": {"license": []}},
    {"is_oa": True, "best_oa_location": {"version": 1}},
    {"is_oa": True, "best_oa_location": {"host_type": []}},
    {"is_oa": True, "journal_is_in_doaj": "false"},
]
VALID_OA = {
    "is_oa": True, "oa_status": "gold",
    "title": "OA paper", "publisher": "Example publisher", "journal_is_in_doaj": True,
    "best_oa_location": {
        "url_for_pdf": "https://example.org/paper.pdf",
        "url_for_landing_page": "https://example.org/paper",
        "license": "cc-by", "version": "publishedVersion",
    },
}


@pytest.fixture
def oa_transport(monkeypatch):
    """Use real httpx parsing over an in-memory transport, never the network."""
    clients = []
    responses = {}
    client_class = httpx.AsyncClient

    def handler(request):
        item = responses.get(request.url.path.rsplit("/", 1)[-1], VALID_OA)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, httpx.Response):
            return item
        if item is None:
            return httpx.Response(200, content=b"null", headers={"Content-Type": "application/json"})
        return httpx.Response(200, json=item)

    class MockClient(client_class):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, transport=httpx.MockTransport(handler), **kwargs)
            clients.append(self)

    monkeypatch.setattr("app.enrich.httpx.AsyncClient", MockClient)
    return responses, clients


@pytest.mark.parametrize("payload", INVALID_OA_RESPONSES)
def test_a05_bad_oa_response_isolated_in_batch_and_lookup(oa_transport, payload):
    responses, clients = oa_transport
    responses["bad"] = payload

    async def run():
        enricher = OaEnricher()
        records = [
            PaperRecord(source="test", title="Bad", doi="10.1000/bad"),
            PaperRecord(source="test", title="Good", doi="10.1000/good"),
        ]
        result = await enricher.enrich(records)
        assert set(result) == {"10.1000/good"}
        assert result["10.1000/good"]["pdf_url"] == "https://example.org/paper.pdf"
        assert result["10.1000/good"]["license"] == "cc-by"
        assert await enricher.enrich_lookup("10.1000/bad") is None
        lookup = await enricher.enrich_lookup("10.1000/good")
        assert {key: lookup[key] for key in result["10.1000/good"]} == result["10.1000/good"]
        assert lookup["title"] == "OA paper"
        assert lookup["publisher"] == "Example publisher"
        assert lookup["in_doaj"] is True
        assert all(r.oa_status == "unknown" for r in records)
        assert all(client.is_closed for client in clients)

    asyncio.run(run())


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503), httpx.Response(200, text="{broken json"),
        httpx.ReadTimeout("timeout"), httpx.ConnectError("connection failed"),
        httpx.Response(200, json={"is_oa": True, "best_oa_location": [1]}),
    ],
)
@pytest.mark.parametrize("entry", ["http", "mcp", "stream"])
def test_a05_optional_oa_failure_preserves_search_results(
    offline_aggregator, oa_transport, monkeypatch, response, entry
):
    responses, clients = oa_transport
    responses["result"] = response
    agg = offline_aggregator

    if entry == "http":
        from app.main import app

        monkeypatch.setattr("app.main.aggregator", agg)
        result = TestClient(app).get(
            "/api/search", params={"q": "library", "sources": "openalex", "oa": "true"}
        )
        assert result.status_code == 200
        data = result.json()
    elif entry == "mcp":
        import mcp_server

        monkeypatch.setattr(mcp_server, "Aggregator", lambda **kwargs: agg)
        data = asyncio.run(mcp_server.metasearch("library", sources="openalex", oa=True))
    else:
        async def run():
            return [e async for e in agg.search_stream("library", sources=["openalex"])]

        events = asyncio.run(run())
        assert [e["event"] for e in events] == ["source", "partial", "done", "oa"]
        assert events[-1]["data"]["annotated"] == 0
        data = next(e["data"] for e in events if e["event"] == "done")
    assert data["total"] == 1
    assert data["results"][0]["title"] == "library"
    assert data["results"][0]["oa_status"] == "unknown"
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("payload", [
    {"is_oa": False, "oa_status": "closed", "best_oa_location": None},
    {"is_oa": True},
    {"is_oa": True, "best_oa_location": {"url": "https://example.org/paper"}},
])
def test_a05_valid_oa_response_shapes_remain_supported(oa_transport, payload):
    responses, _ = oa_transport
    responses["good"] = payload

    async def run():
        enricher = OaEnricher()
        record = PaperRecord(source="test", title="Good", doi="10.1000/good")
        batch = await enricher.enrich([record])
        lookup = await enricher.enrich_lookup(record.doi)
        assert {key: lookup[key] for key in batch[record.doi]} == batch[record.doi]
        assert lookup["is_oa"] is payload["is_oa"]

    asyncio.run(run())


def test_a05_cancellation_is_not_swallowed(oa_transport):
    responses, clients = oa_transport
    responses["cancel"] = asyncio.CancelledError()

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await OaEnricher().enrich([
                PaperRecord(source="test", title="Cancelled", doi="10.1000/cancel")
            ])
        with pytest.raises(asyncio.CancelledError):
            await OaEnricher().enrich_lookup("10.1000/cancel")
        assert all(client.is_closed for client in clients)

    asyncio.run(run())


def test_a01_real_stream_preserves_doi_after_ranked_partial(offline_aggregator):
    agg = offline_aggregator

    async def run():
        released = asyncio.Event()

        async def run_one(client, cls, query, limit):
            if cls.name == "openalex":
                records = [
                    PaperRecord("a", "Alpha study", doi="10.1000/a", score=0.1),
                    PaperRecord("b", "Beta study", doi="10.1000/b", score=0.9),
                ]
            else:
                await released.wait()
                records = [PaperRecord("c", "Alpha study", doi="10.1000/a")]
            return SourceResult(cls.name, cls.label, True, count=len(records)), records

        agg._run_one.side_effect = run_one
        events = []
        async for event in agg.search_stream(
            "ranked-stream", sources=["openalex", "europepmc"], enrich_oa=False
        ):
            events.append(event)
            if event["event"] == "partial":
                released.set()
        partial = next(e["data"] for e in events if e["event"] == "partial")
        assert [r["doi"] for r in partial["results"]] == ["10.1000/b", "10.1000/a"]
        done = next(e["data"] for e in events if e["event"] == "done")
        assert done["total"] == 2
        assert {r["doi"]: set(r["sources"]) for r in done["results"]} == {
            "10.1000/a": {"a", "c"}, "10.1000/b": {"b"}
        }

    asyncio.run(run())


@pytest.mark.parametrize("entry", ["http", "mcp"])
@pytest.mark.parametrize("sources", ["", "openalex,openalex"])
def test_a04_public_entry_obeys_source_selection(offline_aggregator, monkeypatch, entry, sources):
    agg = offline_aggregator
    if entry == "http":
        from app.main import app

        monkeypatch.setattr("app.main.aggregator", agg)
        response = TestClient(app).get("/api/search", params={"q": "library", "sources": sources})
        assert response.status_code == 200
        data = response.json()
    else:
        import mcp_server

        monkeypatch.setattr(mcp_server, "Aggregator", lambda **kwargs: agg)
        data = asyncio.run(mcp_server.metasearch("library", sources=sources))
    expected = 0 if sources == "" else 1
    assert data["total"] == expected
    assert len(data["sources"]) == expected
    assert agg._run_one.await_count == expected


@pytest.mark.parametrize("entry", ["http", "mcp", "stream"])
def test_a05_valid_oa_still_enriches_public_results(offline_aggregator, oa_transport, monkeypatch, entry):
    agg = offline_aggregator
    if entry == "http":
        from app.main import app

        monkeypatch.setattr("app.main.aggregator", agg)
        response = TestClient(app).get(
            "/api/search", params={"q": "library", "sources": "openalex", "oa": "true"}
        )
        assert response.status_code == 200
        data = response.json()
    elif entry == "mcp":
        import mcp_server

        monkeypatch.setattr(mcp_server, "Aggregator", lambda **kwargs: agg)
        data = asyncio.run(mcp_server.metasearch("library", sources="openalex", oa=True))
    else:
        async def run():
            return [e async for e in agg.search_stream("library", sources=["openalex"])]

        events = asyncio.run(run())
        data = events[-1]["data"]
        assert events[-1]["event"] == "oa"
        assert data["annotated"] == 1
    record = data["results"][0]
    assert record["oa_status"] == "open"
    assert record["fulltext_url"] == "https://example.org/paper.pdf"
    assert record["landing_url"] == "https://example.org/paper"
