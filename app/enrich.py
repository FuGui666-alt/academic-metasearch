"""开放获取增强：用 Unpaywall 给每条结果标注「有没有合法免费全文」。

这不是一个 connector —— 它不发起检索，而是在检索完成后对已有结果做补充查询。
输入一批 DOI，输出每个 DOI 的 OA 状态、免费全文链接、许可证、版本。

设计要点：
  - Unpaywall 按 DOI 单条查询，批量时用信号量限流（默认 4 并发）。
  - 只对「有 DOI 且当前 oa_status 不是 open」的结果查，避免浪费请求。
  - 整批共享一个总超时预算（默认 8s），超时就放弃未完成的，已拿到的照常返回。
  - 任何失败都静默降级：标注 oa_status=unknown，不影响主检索。
  - 必须带真实邮箱；未配置时用 UA 里的占位邮箱。
"""
from __future__ import annotations

import asyncio
import os
import time

import httpx

API = "https://api.unpaywall.org/v2"


def _email() -> str:
    """联系邮箱：UNPAYWALL_EMAIL 保留为历史别名，缺省统一走
    METADATA_EMAIL（与 connectors 礼貌池同一来源，见 base.metadata_email）。"""
    from .connectors.base import metadata_email
    return (
        os.environ.get("UNPAYWALL_EMAIL", "").strip()
        or metadata_email()
    )


class OaEnricher:
    """对一批 PaperRecord 补充开放获取信息。"""

    def __init__(
        self,
        max_concurrency: int = 4,
        budget_seconds: float = 8.0,
        per_request_timeout: float = 6.0,
    ):
        self.max_concurrency = max_concurrency
        self.budget_seconds = budget_seconds
        self.per_request_timeout = per_request_timeout

    async def enrich(self, records: list) -> dict[str, dict]:
        """返回 {doi_clean: {is_oa, oa_status, pdf_url, landing_url, license, version}}。

        只查询「有 DOI 且非 open」的记录。
        """
        from .normalize import normalize_doi

        targets: dict[str, object] = {}
        for r in records:
            if r.oa_status == "open":
                continue
            d = normalize_doi(r.doi)
            if d and d not in targets:
                targets[d] = r

        if not targets:
            return {}

        sem = asyncio.Semaphore(self.max_concurrency)
        results: dict[str, dict] = {}
        deadline = time.monotonic() + self.budget_seconds

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.per_request_timeout, connect=4.0),
            follow_redirects=True,
        ) as client:

            async def one(doi: str):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                async with sem:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return
                    try:
                        r = await asyncio.wait_for(
                            client.get(
                                f"{API}/{doi}",
                                params={"email": _email()},
                            ),
                            timeout=min(self.per_request_timeout, remaining),
                        )
                        if r.status_code != 200:
                            return
                        data = r.json()
                        if data.get("is_oa") is None:
                            return
                        best = data.get("best_oa_location") or {}
                        results[doi] = {
                            "is_oa": bool(data.get("is_oa")),
                            "oa_status": data.get("oa_status") or "unknown",
                            "pdf_url": best.get("url_for_pdf"),
                            "landing_url": best.get("url_for_landing_page") or best.get("url"),
                            "license": best.get("license"),
                            "version": best.get("version"),
                            "host_type": best.get("host_type"),
                            "in_doaj": bool(data.get("journal_is_in_doaj")),
                        }
                    except Exception:
                        return

            await asyncio.gather(*[one(d) for d in targets])

        return results

    async def enrich_lookup(self, doi: str) -> dict | None:
        """查单个 DOI 的开放获取信息（详情页/调试用）。"""
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.per_request_timeout, connect=4.0),
            follow_redirects=True,
        ) as client:
            try:
                r = await client.get(f"{API}/{doi}", params={"email": _email()})
                if r.status_code != 200:
                    return None
                data = r.json()
                best = data.get("best_oa_location") or {}
                return {
                    "is_oa": bool(data.get("is_oa")),
                    "oa_status": data.get("oa_status") or "unknown",
                    "pdf_url": best.get("url_for_pdf"),
                    "landing_url": best.get("url_for_landing_page") or best.get("url"),
                    "license": best.get("license"),
                    "version": best.get("version"),
                    "host_type": best.get("host_type"),
                    "in_doaj": bool(data.get("journal_is_in_doaj")),
                    "title": data.get("title"),
                    "publisher": data.get("publisher"),
                }
            except Exception:
                return None


def apply_enrichment(merged_records: list, enrichment: dict[str, dict]) -> int:
    """把增强结果写回结果对象。返回成功标注的条数。"""
    from .normalize import normalize_doi

    count = 0
    for m in merged_records:
        rec = m.record
        d = normalize_doi(rec.doi)
        if not d or d not in enrichment:
            continue

        info = enrichment[d]
        rec.extra["oa"] = info

        if info["is_oa"]:
            # 有合法免费全文：标记 open，并在没有全文链接时补上
            if rec.oa_status != "open":
                rec.oa_status = "open"
            if not rec.fulltext_url and info.get("pdf_url"):
                rec.fulltext_url = info["pdf_url"]
        count += 1

    return count
