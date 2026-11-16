"""核心检索与 Web 边界的离线回归测试。"""
import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.aggregator import Aggregator, IncrementalMerger, SourceResult
from app.cache import SEARCH_CACHE, TTLCache
from app.citations import CitationClient
from app.connectors.europepmc import EuropePmcConnector
from app.connectors.openaire import OpenAireConnector
from app.enrich import OaEnricher, apply_enrichment
from app.main import app
from app.merger import merge
from app.models import MergedRecord, PaperRecord
from app.normalize import normalize_doi


def test_doi_normalization_rejects_urls_and_accepts_valid_doi():
    assert normalize_doi(" HTTPS://DOI.ORG/10.1000/ABC ") == "10.1000/abc"
    assert normalize_doi("https://arxiv.org/abs/2401.00001") is None
    assert normalize_doi("not/a/doi") is None


def test_merge_preserves_year_distinction_and_input():
    first = PaperRecord(source="a", title="Annual report", year=2022)
    second = PaperRecord(source="b", title="Annual report", year=2024)
    assert len(merge([first, second])) == 2

    sparse = PaperRecord(source="a", title="A", abstract=None)
    detailed = PaperRecord(source="b", title="A", abstract="complete")
    assert merge([sparse, detailed])[0].record.abstract == "complete"
    assert detailed.abstract == "complete"
    assert detailed.extra == {}


def test_incremental_merge_indexes_incoming_key_after_fuzzy_merge():
    merger = IncrementalMerger()
    first = PaperRecord(source="a", title="A sufficiently long research title")
    second = PaperRecord(
        source="b", title="A sufficiently long research titles", doi="10.1000/example"
    )
    third = PaperRecord(
        source="c", title="Unrelated title", doi="10.1000/example", year=2024
    )
    merger.add_batch([first, second])
    assert merger._by_key["doi:10.1000/example"] == 0

    # 已有 DOI 的精确键可直接命中，无需再走标题模糊匹配。
    assert merger._find_match(third) == 0


def test_cache_key_is_unambiguous_and_copy_failure_does_not_raise():
    assert TTLCache.make_key("a|b", None) != TTLCache.make_key("a", "b")
    cache = TTLCache()

    class Uncopyable:
        def __deepcopy__(self, memo):
            raise RuntimeError("cannot copy")

    cache.set("bad", Uncopyable())
    assert cache.get("bad") is None


def test_enricher_validates_concurrency_and_applies_closed_and_landing():
    with pytest.raises(ValueError):
        OaEnricher(max_concurrency=0)
    merged = MergedRecord(PaperRecord(source="a", title="A", doi="10.1000/a"))
    apply_enrichment(
        [merged], {"10.1000/a": {"is_oa": False, "oa_status": "closed"}}
    )
    assert merged.record.oa_status == "closed"
    apply_enrichment(
        [merged],
        {"10.1000/a": {
            "is_oa": True, "oa_status": "gold", "landing_url": "https://example.org/a"
        }},
    )
    assert merged.record.fulltext_url == "https://example.org/a"


def test_shared_search_survives_waiter_cancellation():
    async def run():
        agg = Aggregator()
        first = asyncio.create_task(agg.search("shared"))
        second = asyncio.create_task(agg.search("shared"))
        await asyncio.sleep(0)
        first.cancel()
        result = await second
        assert result.results[0].record.title == "shared"
        assert not agg._inflight

    with patch.object(Aggregator, "_search_uncached", autospec=True) as mocked:
        async def controlled(self, cache_key, query, *args):
            await asyncio.sleep(0.02)
            return __import__("app.aggregator", fromlist=["SearchOutcome"]).SearchOutcome(
                query=query,
                results=[MergedRecord(PaperRecord(source="test", title=query))],
            )

        mocked.side_effect = controlled
        SEARCH_CACHE._d.clear()
        asyncio.run(run())


def test_empty_source_list_is_not_default_all_sources():
    with TestClient(app) as client:
        with patch("app.main.aggregator.search", new_callable=AsyncMock) as search:
            from app.aggregator import SearchOutcome
            search.return_value = SearchOutcome(query="q")
            response = client.get("/api/search", params={"q": "q", "sources": ""})
            assert response.status_code == 200
            assert search.await_args.kwargs["sources"] == []


def test_stream_cached_result_contains_unknown_source():
    async def collect():
        agg = Aggregator()
        key = TTLCache.make_key("q", 20, "", False)
        SEARCH_CACHE.set(key, ([], [SourceResult("known", "Known", True)], 1))
        return [event async for event in agg.search_stream("q", sources=["missing"])]

    events = asyncio.run(collect())
    done = next(event for event in events if event["event"] == "done")
    assert done["data"]["sources"][0]["name"] == "missing"


def test_citation_invalid_identifier_makes_no_request():
    async def run():
        with patch("app.citations.get_shared_client") as get_client:
            assert await CitationClient().resolve_work_id("https://example.com/a/b") is None
            get_client.assert_not_called()

    asyncio.run(run())


class FakeResponse:
    def __init__(self, text):
        self.text = text

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        return None


class FakeClient:
    def __init__(self, text):
        self.text = text
        self.params = None

    async def get(self, url, params=None):
        self.params = params
        return FakeResponse(self.text)


def test_europepmc_string_n_is_not_open():
    payload = {"resultList": {"result": [{
        "title": "Closed", "isOpenAccess": "N", "inEPMC": "Y", "doi": "10.1000/a"
    }]}}
    connector = EuropePmcConnector(FakeClient(json.dumps(payload)))
    records = asyncio.run(connector.search("q"))
    assert records[0].oa_status == "unknown"
    assert records[0].fulltext_url is None


def test_openaire_namespaced_results_only_direct_children():
    xml = """<r xmlns='urn:test'><results><result><metadata><entity><result>
    <title>Inner only</title></result></entity></metadata></result></results></r>"""
    records = asyncio.run(OpenAireConnector(FakeClient(xml)).search("q"))
    assert len(records) == 1
    assert records[0].title == "Inner only"


def test_paper_record_runtime_validation():
    """异常上游字段类型被归一化，不影响合并/排序。"""
    r = PaperRecord(
        source="a", title="A", year="2024", citation_count=-5,
        score="not_a_number", authors="not_a_list",
    )
    assert r.year == 2024
    assert r.citation_count is None
    assert r.score == 0.0
    assert r.authors == []


def test_openalex_inverted_index_tolerates_bad_types():
    from app.connectors.openalex import _inv_to_text
    # 正常路径
    assert _inv_to_text({"hello": [0], "world": [1]}) == "hello world"
    # 异常类型不崩溃，返回 None
    assert _inv_to_text({"bad": "not_a_list", 123: [0]}) is None
    assert _inv_to_text({}) is None


def test_semantic_scholar_retry_after_header():
    """429 响应尊重 Retry-After，最多重试一次后抛 RateLimitedError。"""
    import httpx

    from app.connectors.semanticscholar import (
        RateLimitedError,
        SemanticScholarConnector,
    )

    call_count = 0

    class FakeS2Client:
        async def get(self, url, params=None, headers=None):
            nonlocal call_count
            call_count += 1
            resp = httpx.Response(
                429,
                headers={"Retry-After": "0"},
                request=httpx.Request("GET", url),
            )
            return resp

    async def run():
        conn = SemanticScholarConnector(FakeS2Client())
        try:
            await conn.search("q")
            assert False, "应该抛 RateLimitedError"
        except RateLimitedError:
            pass
        assert call_count == 2  # 原始请求 + 一次重试

    asyncio.run(run())


def test_openaire_duplicate_creators_deduplicated():
    xml = """<r><results><result>
    <title>T</title><creator>Alice</creator><creator>Alice</creator>
    </result></results></r>"""
    records = asyncio.run(OpenAireConnector(FakeClient(xml)).search("q"))
    assert records[0].authors == ["Alice"]


def test_zenodo_fulltext_is_not_landing_page():
    payload = {"hits": {"hits": [{
        "id": "1", "metadata": {"title": "T", "access_right": "open"},
        "links": {"html": "https://zenodo.org/records/1"},
        "files": [],
    }]}}
    connector_cls = __import__("app.connectors.zenodo", fromlist=["ZenodoConnector"]).ZenodoConnector
    records = asyncio.run(connector_cls(FakeClient(json.dumps(payload))).search("q"))
    assert records[0].fulltext_url is None
    assert records[0].landing_url == "https://zenodo.org/records/1"
