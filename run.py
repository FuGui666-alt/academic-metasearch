"""启动学术元搜索服务。

默认生产模式：单进程、无热重载（uvicorn reloader 双进程会监听文件
变化，任何 .py 改动自动重启——内存缓存清空、进行中的 SSE 断开，
且历史上出现过 reloader 迟滞与孤儿进程占端口的问题）。

开发模式需显式开启（二选一）：
    python run.py --dev
    set METASEARCH_DEV=1
"""
import os
import sys

import uvicorn

if __name__ == "__main__":
    dev = "--dev" in sys.argv or os.environ.get("METASEARCH_DEV", "").strip() not in (
        "",
        "0",
    )
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=dev,
    )
