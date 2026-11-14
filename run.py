"""启动学术元搜索服务。

脚本模式（源码运行）：
    python run.py            生产模式：单进程、无热重载
    python run.py --dev      开发模式：uvicorn reloader 双进程热重载
                             （任何 .py 改动自动重启，缓存与 SSE 会断）
    set METASEARCH_DEV=1     与 --dev 等价

便携模式（PyInstaller 打包的 exe，sys.frozen=True）：
    双击 学术元搜索.exe → 启动服务并自动打开浏览器。
"""
import os
import sys
import threading
import webbrowser

import uvicorn

if __name__ == "__main__":
    frozen = getattr(sys, "frozen", False)
    dev = (not frozen) and (
        "--dev" in sys.argv
        or os.environ.get("METASEARCH_DEV", "").strip() not in ("", "0")
    )
    if frozen:
        # 便携版：直接引入 app 对象（避免依赖冻结环境里的字符串导入），
        # 启动后自动打开浏览器
        from app.main import app

        threading.Timer(
            2.0, lambda: webbrowser.open("http://127.0.0.1:8000")
        ).start()
        uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
    else:
        uvicorn.run(
            "app.main:app",
            host="127.0.0.1",
            port=8000,
            reload=dev,
        )
