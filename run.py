#!/usr/bin/env python3
"""启动入口：python run.py [--port 8000] [--host 127.0.0.1]"""
import argparse
import socket

import uvicorn


def _lan_ip() -> str | None:
    """取本机在局域网里的 IP（手机要访问时就靠它）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def main():
    parser = argparse.ArgumentParser(description="看长做短 V5.1")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="开发模式自动重载")
    args = parser.parse_args()

    print(f"本机访问：http://127.0.0.1:{args.port}")
    if args.host == "0.0.0.0":
        ip = _lan_ip()
        if ip:
            print(f"手机访问：http://{ip}:{args.port}   （同一 Wi-Fi，浏览器里选「添加到主屏幕」）")
        print("提示：设了 ASTOCK_TOKEN 环境变量时，手机首次用 ?token=口令 打开即可")
    else:
        print("提示：手机要访问请用 --host 0.0.0.0（仅本机则不用）")

    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
