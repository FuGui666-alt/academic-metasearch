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
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

API = "https://api.unpaywall.org/v2"


def _email() -> str:
    """联系邮箱：UNPAYWALL_EMAIL 保留为历史别名，缺省统一走
    METADATA_EMAIL（与 connectors 礼貌池同一来源，见 base.metadata_email）。"""
    from .connectors.base import metadata_email
    return (
        os.environ.get("UNPAYWALL_EMAIL", "").strip()
        or metadata_email()
    )


def _parse_oa_response(data: object) -> dict:
    """校验上游 JSON 的公共 OA 字段；坏响应由调用方按单 DOI 降级。"""
    if not isinstance(data, dict) or not isinstance(data.get("is_oa"), bool):
        raise TypeError("Unpaywall 响应缺少布尔 is_oa")
    best = data.get("best_oa_location")
    if best is None:
        best = {}
    if not isinstance(best, dict):
        raise TypeError("Unpaywall best_oa_location 必须为对象或 null")
    if data.get("oa_status") is not None and not isinstance(data["oa_status"], str):
        raise TypeError("Unpaywall oa_status 必须为字符串")
    for key in ("url_for_pdf", "url_for_landing_page", "url", "license", "version", "host_type"):
        if best.get(key) is not None and not isinstance(best[key], str):
            raise TypeError(f"Unpaywall {key} 必须为字符串")
    in_doaj = data.get("journal_is_in_doaj")
    if in_doaj is not None and not isinstance(in_doaj, bool):
        raise TypeError("Unpaywall journal_is_in_doaj 必须为布尔值")
    return {
        "is_oa": data["is_oa"],
        "oa_status": data.get("oa_status") or "unknown",
        "pdf_url": best.get("url_for_pdf"),
        "landing_url": best.get("url_for_landing_page") or best.get("url"),
        "license": best.get("license"),
        "version": best.get("version"),
        "host_type": best.get("host_type"),
        "in_doaj": bool(in_doaj),
    }


class OaEnricher:
    """对一批 PaperRecord 补充开放获取信息。"""

    def __init__(
        self,
        max_concurrency: int = 4,
        budget_seconds: float = 8.0,
        per_request_timeout: float = 6.0,
    ):
        if max_concurrency < 1:
            raise ValueError("max_concurrency 必须大于等于 1")
        if budget_seconds <= 0 or per_request_timeout <= 0:
            raise ValueError("超时预算必须大于 0")
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
                            logger.debug("Unpaywall %s: HTTP %d", doi, r.status_code)
                            return
                        results[doi] = _parse_oa_response(r.json())
                    except (httpx.HTTPError, TimeoutError, ValueError, TypeError) as e:
                        logger.debug("Unpaywall %s 失败: %s", doi, type(e).__name__)
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
                    logger.debug("Unpaywall lookup %s: HTTP %d", doi, r.status_code)
                    return None
                data = r.json()
                info = _parse_oa_response(data)
                # 保留 lookup 独有的元数据，不改变已有输出字段。
                info.update(title=data.get("title"), publisher=data.get("publisher"))
                return info
            except (httpx.HTTPError, ValueError, TypeError) as e:
                logger.debug("Unpaywall lookup %s 失败: %s", doi, type(e).__name__)
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
            # 有合法免费全文：标记 open，并在没有全文链接时补上。
            rec.oa_status = "open"
            if not rec.fulltext_url:
                rec.fulltext_url = info.get("pdf_url") or info.get("landing_url")
            if not rec.landing_url and info.get("landing_url"):
                rec.landing_url = info["landing_url"]
        elif info.get("oa_status") == "closed":
            rec.oa_status = "closed"
        count += 1

    return count
