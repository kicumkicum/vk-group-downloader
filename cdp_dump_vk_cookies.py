#!/usr/bin/env python3
import argparse
import json
import sys
import time
from pathlib import Path

import requests
from websocket import create_connection


def cdp_call(ws, method, params=None, call_id=1, timeout=10.0):
    params = params or {}
    msg = {"id": call_id, "method": method, "params": params}
    ws.send(json.dumps(msg))
    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = ws.recv()
        data = json.loads(raw)
        if data.get("id") == call_id:
            if "error" in data:
                raise RuntimeError(data["error"])
            return data.get("result") or {}
    raise TimeoutError(f"Timeout waiting for {method}")


def pick_target(devtools_http_base: str) -> str:
    r = requests.get(f"{devtools_http_base}/json", timeout=5)
    r.raise_for_status()
    targets = r.json()
    # Prefer vk.com tab if exists, else first page target
    for t in targets:
        if t.get("type") == "page" and "vk.com" in (t.get("url") or "") and t.get("webSocketDebuggerUrl"):
            return t["webSocketDebuggerUrl"]
    for t in targets:
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
            return t["webSocketDebuggerUrl"]
    raise RuntimeError("No CDP page targets found at /json")


def dump_vk_cookies(devtools_http_base: str) -> list[dict]:
    ws_url = pick_target(devtools_http_base)
    ws = create_connection(ws_url, timeout=10)
    try:
        cdp_call(ws, "Network.enable", {}, call_id=1)
        res = cdp_call(ws, "Network.getAllCookies", {}, call_id=2)
        cookies = res.get("cookies") or []
        vk = []
        for c in cookies:
            domain = c.get("domain") or ""
            if "vk.com" not in domain:
                continue
            vk.append({"name": c.get("name"), "value": c.get("value")})
        return [c for c in vk if c.get("name") and c.get("value") is not None]
    finally:
        try:
            ws.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="Dump vk.com cookies via Chrome DevTools Protocol")
    ap.add_argument("--base", default="http://127.0.0.1:9222", help="DevTools HTTP base, e.g. http://127.0.0.1:9222")
    ap.add_argument("--out", default="cookies.json", help="Output cookies JSON path")
    args = ap.parse_args()

    cookies = dump_vk_cookies(args.base)
    if not cookies:
        print("No vk.com cookies found. Open vk.com tab and login, then retry.", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(cookies)} cookies to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

