"""知网（CNKI）跳转辅助 —— 只生成外链，不做任何抓取。

知网没有公开检索 API，且其订阅内容受版权保护。本项目不接入、不爬取，
只提供「带着 DOI / 标题去知网/学校入口继续检索」的链接，由用户在自己的
浏览器里（校园网内）完成正式检索与下载。
"""
from __future__ import annotations

from urllib.parse import quote

# 知网通用检索页（公开可达的搜索入口，非接口）
CNKI_SEARCH = "https://kns.cnki.net/kns8s/search?crossids=YSTT4HG0%2CLSTPFY1C%2CJUP3MUPD%2CMPMFIG1A%2CWQ0UVIAA%2CBLZOG7CK%2CEMRPGLPA%2CPWFIRAGL%2CNLBO1Z6R%2CNN3FJMUV&korder=SU&kw="

# 某高校图书馆（校园网内可凭订阅权限访问）
SCHOOL_LIBRARY = "https://lib.example.edu.cn/"


def cnki_search_url(query: str) -> str:
    """生成知网检索链接（用户自己点开去搜，不是本软件去搜）。"""
    return CNKI_SEARCH + quote(query.strip())


def cnki_title_url(title: str) -> str:
    """按标题在知网检索（题名检索）。"""
    base = CNKI_SEARCH.replace("korder=SU", "korder=TI")
    return base + quote(title.strip())


def school_library_url() -> str:
    return SCHOOL_LIBRARY


def build_links(title: str, doi: str | None = None) -> dict[str, str]:
    """给一条结果生成「去别处继续找」的链接集合。"""
    links = {
        "cnki_title": cnki_title_url(title) if title else "",
        "school": school_library_url(),
    }
    if doi:
        d = doi.replace("https://doi.org/", "").replace("http://doi.org/", "").strip()
        if d:
            links["doi"] = f"https://doi.org/{d}"
    return {k: v for k, v in links.items() if v}
