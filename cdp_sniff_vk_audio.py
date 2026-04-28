#!/usr/bin/env python3
"""
Sniff VK audio-related network requests via Chrome DevTools Protocol.

Usage:
  ./venv/bin/python cdp_sniff_vk_audio.py --base http://127.0.0.1:9222 --seconds 60

Then play a track in the browser and watch printed URLs.
"""

import argparse
import json
import time

import requests
from websocket import create_connection, WebSocketTimeoutException


def pick_target(devtools_http_base: str) -> str:
    r = requests.get(f"{devtools_http_base}/json", timeout=5)
    r.raise_for_status()
    targets = r.json()
    # Prefer vk.com tab
    for t in targets:
        if t.get("type") == "page" and "vk.com" in (t.get("url") or "") and t.get("webSocketDebuggerUrl"):
            return t["webSocketDebuggerUrl"]
    for t in targets:
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
            return t["webSocketDebuggerUrl"]
    raise RuntimeError("No CDP page targets found at /json")


def cdp_send(ws, msg: dict):
    ws.send(json.dumps(msg))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:9222")
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument(
        "--match",
        default="m3u8,mp3,audio,al_audio.php,reload_audio,psv,vkuseraudio",
        help="comma-separated substrings to filter URLs",
    )
    args = ap.parse_args()

    ws_url = pick_target(args.base)
    ws = create_connection(ws_url, timeout=5)
    ws.settimeout(1.0)
    try:
        # Enable network events
        cdp_send(ws, {"id": 1, "method": "Network.enable", "params": {}})
        cdp_send(ws, {"id": 2, "method": "Page.enable", "params": {}})
        cdp_send(ws, {"id": 3, "method": "Runtime.enable", "params": {}})

        match = [s.strip().lower() for s in args.match.split(",") if s.strip()]
        end = time.time() + args.seconds
        seen = set()

        print(f"Listening {args.seconds}s on {ws_url}")
        print("Play a VK track now. Printing matching request URLs.\n")

        while time.time() < end:
            try:
                raw = ws.recv()
            except WebSocketTimeoutException:
                continue
            evt = json.loads(raw)
            method = evt.get("method")
            params = evt.get("params") or {}

            url = None
            if method == "Network.requestWillBeSent":
                req = params.get("request") or {}
                url = req.get("url")
            elif method == "Network.responseReceived":
                resp = params.get("response") or {}
                url = resp.get("url")

            if not url:
                continue

            u = url.lower()
            if match and not any(m in u for m in match):
                continue

            key = (method, url)
            if key in seen:
                continue
            seen.add(key)

            if method == "Network.requestWillBeSent":
                req = params.get("request") or {}
                print(f"[REQ] {req.get('method','GET')} {url}")
                # Show POST body for VK ajax endpoints
                if req.get("method") == "POST" and "vk.com/al_audio.php" in url:
                    post = req.get("postData")
                    if post:
                        # avoid huge spam
                        trimmed = post if len(post) < 1500 else post[:1500] + "...(truncated)"
                        print("[REQ]   postData:", trimmed)
            else:
                resp = params.get("response") or {}
                status = resp.get("status")
                mime = resp.get("mimeType")
                print(f"[RES] {status} {mime} {url}")

    finally:
        try:
            ws.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()

