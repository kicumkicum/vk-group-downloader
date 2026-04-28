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
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs

import requests
from websocket import create_connection, WebSocketTimeoutException


def pick_target(devtools_http_base: str, prefer_substring: str = "vk.com") -> str:
    r = requests.get(f"{devtools_http_base}/json", timeout=5)
    r.raise_for_status()
    targets = r.json()
    # Prefer a specific tab (vk.com by default)
    for t in targets:
        if (
            t.get("type") == "page"
            and prefer_substring
            and prefer_substring in (t.get("url") or "")
            and t.get("webSocketDebuggerUrl")
        ):
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
    ap.add_argument(
        "--vkvideo-tokens",
        action="store_true",
        help="Extract vkvideo catalog.getSection section_id + access_token from CDP network requests.",
    )
    ap.add_argument(
        "--out",
        default="vkvideo_tokens.json",
        help="Output path for --vkvideo-tokens (default: vkvideo_tokens.json).",
    )
    args = ap.parse_args()

    prefer = "vkvideo.ru" if args.vkvideo_tokens else "vk.com"
    ws_url = pick_target(args.base, prefer_substring=prefer)
    ws = create_connection(ws_url, timeout=5)
    ws.settimeout(1.0)
    try:
        # Enable network events
        cdp_send(ws, {"id": 1, "method": "Network.enable", "params": {}})
        cdp_send(ws, {"id": 2, "method": "Page.enable", "params": {}})
        cdp_send(ws, {"id": 3, "method": "Runtime.enable", "params": {}})

        if args.vkvideo_tokens:
            match = ["api.vkvideo.ru/method/catalog.getsection"]
        else:
            match = [s.strip().lower() for s in args.match.split(",") if s.strip()]
        end = time.time() + args.seconds
        seen = set()

        print(f"Listening {args.seconds}s on {ws_url}")
        if args.vkvideo_tokens:
            print("Open vkvideo.ru, go to the Clips section, scroll a bit. Waiting for catalog.getSection...\n")
        else:
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
                post = req.get("postData") or ""
                if args.vkvideo_tokens and req.get("method") == "POST" and "api.vkvideo.ru/method/catalog.getSection".lower() in u:
                    q = parse_qs(post, keep_blank_values=True)
                    section_id = (q.get("section_id") or [""])[0].strip()
                    access_token = (q.get("access_token") or [""])[0].strip()
                    if section_id and access_token:
                        out = {
                            "section_id": section_id,
                            "access_token": access_token,
                            "source": "cdp",
                            "captured_at": datetime.utcnow().isoformat() + "Z",
                            "request_url": url,
                        }
                        p = Path(args.out)
                        p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
                        print("[OK] Saved tokens to", str(p))
                        return 0
                # Show POST body for VK ajax endpoints
                if (not args.vkvideo_tokens) and req.get("method") == "POST" and "vk.com/al_audio.php" in url:
                    if post:
                        trimmed = post if len(post) < 1500 else post[:1500] + "...(truncated)"
                        print("[REQ]   postData:", trimmed)
            else:
                resp = params.get("response") or {}
                status = resp.get("status")
                mime = resp.get("mimeType")
                print(f"[RES] {status} {mime} {url}")

        if args.vkvideo_tokens:
            print("[ERR] Did not see catalog.getSection POST within the time window.")
            print("Tip: open `https://vkvideo.ru/` in the same browser, login, then open Clips and scroll.")
            return 2
    finally:
        try:
            ws.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())

