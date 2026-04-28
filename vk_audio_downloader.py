#!/usr/bin/env python3
"""Скрипт для скачивания аудио по плейлистам/альбомам из VK групп/пабликов.

Авторизация: только через cookies (CDP/браузер), без логина/пароля.
"""

import os
import sys
import json
import time
import re
import hashlib
import urllib.parse
import html as _html
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import subprocess
import shutil
from typing import Optional, List, Tuple, Dict, Any
import threading
import concurrent.futures as _futures

try:
    import requests
except ImportError:
    print("Установите requests: pip install requests")
    sys.exit(1)

# Попытка импорта vk_api (используем только декодер url при необходимости)
try:
    import vk_api

    VK_API_AVAILABLE = True
except ImportError:
    VK_API_AVAILABLE = False
    print("[INFO] vk_api не установлена. Используйте токен или cookies.")
    print("Установите: pip install vk_api")

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

try:
    import browser_cookie3
except ImportError:
    browser_cookie3 = None

try:
    import requests as _requests_for_cdp
    from websocket import create_connection as _cdp_ws_create
except Exception:
    _requests_for_cdp = None
    _cdp_ws_create = None

try:
    from vk_api.audio_url_decoder import decode_audio_url
except Exception:
    decode_audio_url = None


def print_progress(current, total, prefix="Скачивание"):
    """Отображение прогресса"""
    if total > 0:
        percent = (current / total) * 100
        bar_len = 40
        filled = int(bar_len * current / total)
        bar = "#" * filled + "-" * (bar_len - filled)
        print(
            f"\r{prefix}: [{bar}] {current}/{total} ({percent:.1f}%)",
            end="",
            flush=True,
        )
    else:
        print(f"\r{prefix}: {current}", end="", flush=True)


class VKAudioDownloader:
    def __init__(self, group_id, cookies=None, access_token=None, user_agent=None):
        self.group_id = str(group_id).strip()
        self.cookies = cookies or {}
        self.access_token = access_token
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent
                or "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Referer": "https://vk.com/",
                "Origin": "https://vk.com",
            }
        )

        # Установка cookies
        # Поддерживаем 2 формата:
        # - dict name->value (legacy)
        # - {"_simple":{...}, "_items":[{name,value,domain,path,...}, ...]} (domain-aware)
        cookie_items = None
        if isinstance(self.cookies, dict) and "_items" in self.cookies and isinstance(self.cookies.get("_items"), list):
            cookie_items = self.cookies.get("_items")
            simple = self.cookies.get("_simple") if isinstance(self.cookies.get("_simple"), dict) else {}
            self.cookies = simple  # keep legacy dict behavior for other code

        # Some "служебные" cookies нужны, иначе аудио-страницы могут ломаться
        if isinstance(self.cookies, dict):
            self.cookies.setdefault("remixaudio_show_alert_today", "0")
            self.cookies.setdefault("remixmdevice", "1920/1080/2/!!-!!!!")

        if cookie_items:
            for c in cookie_items:
                name = c.get("name")
                val = c.get("value")
                if not name or val is None:
                    continue
                domain = c.get("domain") or ".vk.com"
                path = c.get("path") or "/"
                try:
                    self.session.cookies.set(str(name), str(val), domain=domain, path=path)
                except Exception:
                    self.session.cookies.set(str(name), str(val), domain=".vk.com")
        else:
            for name, value in (self.cookies or {}).items():
                self.session.cookies.set(name, value, domain=".vk.com")

        # Заголовки, которые используют моб. эндпоинты m.vk.com/audio
        self.m_headers = {
            "User-Agent": self.session.headers.get("User-Agent"),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://m.vk.com/",
            "Origin": "https://m.vk.com",
        }

    @staticmethod
    def _normalize_owner_id(owner_id):
        """
        Приводит owner_id к числу.
        Для групп в VK owner_id отрицательный (например, -179835916).
        """
        s = str(owner_id).strip()
        if not s:
            raise ValueError("owner_id пустой")
        if s.startswith("club"):
            s = s[4:]
        if s.startswith("public"):
            s = s[6:]
        if s.startswith("id"):
            s = s[2:]
        s = s.replace(" ", "")
        m = re.match(r"^-?\d+", s)
        if not m:
            raise ValueError(f"Не удалось распознать owner_id: {owner_id!r}")
        return int(m.group(0))

    def group_owner_id(self):
        try:
            gid = self._normalize_owner_id(self.group_id)
            return gid if gid < 0 else -gid
        except ValueError:
            # Prefer official resolver when token is present
            if self.access_token:
                resolved = self._api_request(
                    "utils.resolveScreenName", {"screen_name": self.group_id}
                )
                if "error" in resolved:
                    raise ValueError(
                        f"Не удалось резолвить '{self.group_id}': {resolved['error'].get('error_msg', '')}"
                    )
                resp = resolved.get("response") or {}
                obj_type = resp.get("type")
                obj_id = resp.get("object_id")
                if not obj_id or obj_type not in {"group", "page"}:
                    raise ValueError(
                        f"'{self.group_id}' не похож на группу/паблик (type={obj_type}, object_id={obj_id})"
                    )
                return -int(obj_id)

            # Без токена резолвим screen name через HTML vk.com
            html = self._get_page_html(f"https://vk.com/{self.group_id}")
            if not html:
                raise ValueError(f"Не удалось открыть страницу https://vk.com/{self.group_id}")

            patterns = [
                r'"group_id":\s*(\d+)',
                r'"public_id":\s*(\d+)',
                r'data-community-id="(\d+)"',
                r'"entity_id":\s*(\d+)',
            ]
            for p in patterns:
                m = re.search(p, html)
                if m:
                    return -int(m.group(1))

            raise ValueError(f"Не удалось распознать ID группы по screen name: {self.group_id!r}")

    def _api_request(self, method, params=None, http_method: str = "GET", api_version: Optional[str] = None):
        """Вызов VK API (GET/POST)"""
        params = params or {}
        # Параметры совместимости для некоторых методов VK
        params.setdefault("https", 1)
        params.setdefault("lang", "ru")
        params.setdefault("extended", 1)
        params["v"] = api_version or "5.131"
        if self.access_token:
            params["access_token"] = self.access_token

        url = f"https://api.vk.com/method/{method}"

        try:
            if http_method.upper() == "POST":
                response = self.session.post(url, data=params, timeout=15)
            else:
                response = self.session.get(url, params=params, timeout=15)
            data = response.json()
            if "error" in data:
                safe_params = dict(params)
                if "access_token" in safe_params:
                    tok = str(safe_params["access_token"])
                    safe_params["access_token"] = tok[:8] + "...(redacted)"
                print(f"[LOG] VK API error in {method}")
                print(f"[LOG]   http_method={http_method.upper()} v={params.get('v')}")
                print(f"[LOG]   params={safe_params}")
                print(f"[LOG]   error={data.get('error')}")
            return data
        except Exception as e:
            print(f"Ошибка API запроса: {e}")
            return {"error": {"error_msg": str(e)}}

    # NOTE: VK audio API methods intentionally not used.

    def _get_page_html(self, url):
        """Получение HTML страницы"""
        try:
            response = self.session.get(url, timeout=10)
            return response.text
        except Exception as e:
            print(f"Ошибка получения HTML: {e}")
            return ""

    def _extract_audio_data(self, html):
        """Извлечение данных об аудио из HTML страницы"""
        # Формат: <div class="audio" data-audio="['artist', 'title', 'url', 'duration', '...']">
        audios = []

        # Паттерн для find
        pattern = r'data-audio=["\']([^"\']+)["\']'
        matches = re.findall(pattern, html)

        for match in matches:
            try:
                # JSON строка в виде массива
                data = json.loads(match.replace("'", '"'))
                if isinstance(data, list) and len(data) >= 4:
                    audios.append(
                        {
                            "id": data[0],
                            "owner_id": data[1],
                            "hash": data[2],
                            "duration": data[3],
                            "url": data[4] if len(data) > 4 else "",
                            "artist": data[5] if len(data) > 5 else "",
                            "title": data[6] if len(data) > 6 else "",
                            "access_key": data[7] if len(data) > 7 else "",
                        }
                    )
            except:
                pass

        return audios

    def get_audio_by_page(self):
        """Получение аудио через парсинг страницы (без API)"""
        url = f"https://vk.com/audios{self.group_id}"

        html = self._get_page_html(url)
        if not html:
            return []

        return self._extract_audio_data(html)

    def get_audio_api(self):
        """Получение аудио через VK API"""
        audios = []

        # Метод: audio.get
        params = {
            "owner_id": self.group_owner_id(),
            "count": 100,
        }

        data = self._api_request("audio.get", params)

        if "error" in data:
            print(f"Ошибка API: {data['error'].get('error_msg', '')}")
            return []

        if "response" in data and "items" in data["response"]:
            items = data["response"]["items"]
            for item in items:
                audios.append(
                    {
                        "id": item.get("id"),
                        "owner_id": item.get("owner_id"),
                        "title": item.get("title"),
                        "artist": item.get("artist"),
                        "url": item.get("url"),
                        "duration": item.get("duration"),
                        "access_key": item.get("access_key", ""),
                    }
                )

        return audios

    def list_playlists(self, owner_id=None, count=100):
        """Список плейлистов. Сначала пробуем m.vk.com, потом fallback на vk.com HTML."""
        if BeautifulSoup is None:
            print("[LOG] Установите beautifulsoup4: pip install beautifulsoup4")
            return []

        if owner_id is None:
            owner_id = self.group_owner_id()
        else:
            owner_id = self._normalize_owner_id(owner_id)

        # Прогреем cookies
        self.session.get("https://m.vk.com/", timeout=10)

        playlists = []
        offset = 0
        while True:
            r = self.session.get(
                f"https://m.vk.com/audio?act=audio_playlists{owner_id}",
                params={"offset": offset},
                timeout=15,
            )
            html = r.text or ""
            if not html:
                break

            soup = BeautifulSoup(html, "html.parser")
            items = soup.find_all("div", {"class": "audioPlaylistsPage__item"})
            if not items:
                break

            for album in items:
                link_el = album.select_one(".audioPlaylistsPage__itemLink")
                if not link_el or not link_el.get("href"):
                    continue
                link = link_el["href"]
                m = re.search(r"act=audio_playlist(-?\d+)_(\d+)", link)
                if not m:
                    continue
                owner = int(m.group(1))
                pid = int(m.group(2))
                h = re.search(r"access_hash=([0-9a-zA-Z_]+)", link)
                access_hash = h.group(1) if h else None
                title_el = album.select_one(".audioPlaylistsPage__title")
                title = title_el.text.strip() if title_el else f"playlist_{pid}"
                playlists.append(
                    {
                        "id": pid,
                        "owner_id": owner,
                        "title": title,
                        "access_hash": access_hash,
                    }
                )

            if len(items) < 100:
                break
            offset += 100
            time.sleep(0.3)

        if playlists:
            return playlists

        # Fallback: парсим vk.com/audios{owner}?section=playlists
        print("[LOG] Fallback: парсинг плейлистов с vk.com (web).")
        url = f"https://vk.com/audios{owner_id}?section=playlists"
        html = self._get_page_html(url)
        if not html:
            return []

        # Достаём ссылки на audio_playlist... (часто встречаются в HTML)
        found = {}
        for m in re.finditer(r"audio_playlist(-?\d+)_(\d+)(?:[^\"]*access_hash=([0-9a-zA-Z_]+))?", html):
            o = int(m.group(1))
            pid = int(m.group(2))
            ah = m.group(3) if m.group(3) else None
            found[(o, pid)] = ah

        playlists = []
        for (o, pid), ah in found.items():
            playlists.append({"id": pid, "owner_id": o, "title": f"playlist_{pid}", "access_hash": ah})

        return playlists

    def iter_playlist_tracks(self, playlist_id, owner_id=None, batch=200):
        """Постранично выдаёт треки из плейлиста через m.vk.com (требует cookies)."""
        if BeautifulSoup is None:
            print("[LOG] Установите beautifulsoup4: pip install beautifulsoup4")
            return

        if owner_id is None:
            owner_id = self.group_owner_id()
        else:
            owner_id = self._normalize_owner_id(owner_id)

        # Достанем user_id из cookies, чтобы при необходимости декодировать ссылки
        user_id = 0
        for key in ("remixuid", "remixuserid", "remixuser"):
            if key in self.cookies:
                try:
                    user_id = int(re.match(r"\d+", str(self.cookies[key])).group(0))
                except Exception:
                    pass

        # Прогреем cookies
        self.session.get("https://m.vk.com/", timeout=10)

        offset = 0
        access_hash = None
        if isinstance(playlist_id, dict):
            access_hash = playlist_id.get("access_hash")
            playlist_id = playlist_id.get("id")

        while True:
            resp = self.session.post(
                "https://m.vk.com/audio",
                data={
                    "act": "load_section",
                    "owner_id": owner_id,
                    "playlist_id": int(playlist_id),
                    "offset": offset,
                    "type": "playlist",
                    "access_hash": access_hash,
                    "is_loading_all": 1,
                },
                headers=self.m_headers,
                timeout=20,
            ).json()

            data0 = (resp.get("data") or [{}])[0] or {}
            audio_list = data0.get("list") or []
            if not audio_list:
                break

            ids = []
            for track in audio_list:
                try:
                    audio_hashes = track[13].split("/")
                    # full_id: owner_id, audio_id, actionHash, urlHash
                    full_id = (str(track[1]), str(track[0]), audio_hashes[2], audio_hashes[5])
                    if all(full_id):
                        ids.append(full_id)
                except Exception:
                    continue

            if not ids:
                break

            # reload_audio группами по 10
            for i in range(0, len(ids), 10):
                ids_group = ids[i : i + 10]
                result = self.session.post(
                    "https://m.vk.com/audio",
                    data={
                        "act": "reload_audio",
                        "ids": ",".join(["_".join(x) for x in ids_group]),
                    },
                    headers=self.m_headers,
                    timeout=20,
                ).json()

                block = (result.get("data") or [None])[0] or []
                for a in block:
                    try:
                        artist = BeautifulSoup(a[4], "html.parser").text
                        title = BeautifulSoup((a[3] or "").strip(), "html.parser").text
                        duration = a[5]
                        link = a[2]
                        if "audio_api_unavailable" in link and decode_audio_url and user_id:
                            link = decode_audio_url(link, user_id)
                        # Не подменяем m3u8 на mp3: это часто приводит к заглушке.
                        yield {
                            "id": a[0],
                            "owner_id": a[1],
                            # хэши нужны, чтобы получить реальный url через al_audio.php reload_audios
                            "action_hash": (a[13].split("/")[2] if isinstance(a[13], str) and "/" in a[13] else ""),
                            "url_hash": (a[13].split("/")[5] if isinstance(a[13], str) and "/" in a[13] else ""),
                            "hashes_raw": (a[13] if isinstance(a[13], str) else ""),
                            "url": link,
                            "artist": artist,
                            "title": title,
                            "duration": duration,
                        }
                    except Exception:
                        continue

            has_more = bool(data0.get("hasMore"))
            if not has_more:
                break
            offset += 2000
            time.sleep(0.4)

    def get_all_audio(self):
        """Получение всех доступных аудио"""
        print("[LOG] Пробуем получить через API...")
        audios = self.get_audio_api()

        if audios:
            print(f"[LOG] Получено через API: {len(audios)} треков")
            return audios

        print("[LOG] API не сработал, пробуем парсинг страницы...")
        audios = self.get_audio_by_page()
        print(f"[LOG] Получено через парсинг: {len(audios)} треков")

        return audios

    def get_playlist_url(self, playlist_id):
        """Получение URL для прослушивания плейлиста"""
        # Метод: audio.getPlaylist
        params = {
            "playlist_id": playlist_id,
            "owner_id": self.group_owner_id(),
            "v": "5.131",
        }
        if self.access_token:
            params["access_token"] = self.access_token

        url = "https://api.vk.com/method/audio.getPlaylist"

        try:
            response = self.session.get(url, params=params, timeout=10)
            return response.json()
        except Exception as e:
            print(f"Ошибка: {e}")
            return {"error": {"error_msg": str(e)}}


def extract_audio_from_vk_html(html):
    """Извлечение аудио данных из HTML страницы VK"""
    audios = []

    # Паттерн для данных аудио
    # data-audio="['id', 'owner_id', 'hash', 'duration', 'url', 'artist', 'title', 'access_key']"
    pattern = r'data-audio=["\']([^"\']+)["\']'
    matches = re.findall(pattern, html)

    for match in matches:
        try:
            # ПАРСИМ JSON - формат странный, используем eval с осторожностью
            # Или просто заменяем одинарные кавычки на двойные и парсим
            data_str = match.replace("\\", "\\\\").replace("'", '"')
            data = json.loads(data_str)

            if isinstance(data, list) and len(data) >= 7:
                audios.append(
                    {
                        "id": str(data[0]) if data[0] else "",
                        "owner_id": str(data[1]) if data[1] else "",
                        "hash": data[2] if data[2] else "",
                        "duration": int(data[3]) if data[3] else 0,
                        "url": data[4] if len(data) > 4 else "",
                        "artist": data[5] if len(data) > 5 else "",
                        "title": data[6] if len(data) > 6 else "",
                        "access_key": data[7] if len(data) > 7 else "",
                    }
                )
        except json.JSONDecodeError as e:
            print(f"Ошибка парсинга аудио данных: {e}")
            continue

    return audios


def get_cookies_from_browser(browser_type="chrome"):
    """
    Попытка получить cookies из браузера
    Поддерживаемые: Firefox, Chrome, Chromium, Edge, Vivaldi
    """
    from pathlib import Path

    cookies = {}
    profile_path = None

    # Firefox
    if browser_type == "firefox":
        profiles_dir = Path.home() / ".mozilla" / "firefox"
        if profiles_dir.exists():
            # Ищем profiles.ini
            ini_path = profiles_dir / "profiles.ini"
            if ini_path.exists():
                with open(ini_path) as f:
                    content = f.read()
                    if "Path=" in content:
                        # Находим активный профиль
                        import configparser

                        config = configparser.ConfigParser()
                        config.read(ini_path)
                        for section in config.sections():
                            if "Path=" in str(config[section]):
                                profile_path = profiles_dir / config[section].get(
                                    "Path", ""
                                )
                                break

            if profile_path:
                cookie_db = profile_path / "cookies.sqlite"
                if cookie_db.exists():
                    try:
                        import sqlite3

                        conn = sqlite3.connect(cookie_db)
                        cursor = conn.cursor()
                        cursor.execute("""
                            SELECT name, value, host FROM moz_cookies 
                            WHERE host LIKE '%vk.com%' AND expiry > strftime('%s', 'now')
                        """)
                        for name, value, host in cursor.fetchall():
                            cookies[name] = value
                        conn.close()
                        print(f"Cookie из Firefox: {len(cookies)} записей")
                    except Exception as e:
                        print(f"Ошибка чтения Firefox cookies: {e}")

    # Chrome/Chromium/Edge/Vivaldi
    elif browser_type in ["chrome", "chromium", "edge", "vivaldi"]:
        chrome_dirs = []
        if browser_type == "chrome":
            chrome_dirs = [Path.home() / ".config" / "google-chrome"]
        elif browser_type == "chromium":
            chrome_dirs = [Path.home() / ".config" / "chromium"]
        elif browser_type == "vivaldi":
            # Vivaldi использует тот же профиль что и Chrome
            chrome_dirs = [
                Path.home() / ".config" / "vivaldi",
                Path.home() / ".config" / "google-chrome",
            ]
        else:
            chrome_dirs = [
                Path.home() / ".config" / "microsoft-edge",
                Path.home() / ".config" / "chr",
            ]

        for chrome_dir in chrome_dirs:
            if chrome_dir.exists():
                # Vivaldi использует Default, как и Chrome
                cookie_db = chrome_dir / "Default" / "Cookies"
                if cookie_db.exists():
                    try:
                        import sqlite3

                        conn = sqlite3.connect(cookie_db)
                        cursor = conn.cursor()

                        # Получаем список колонок для определения версии
                        cursor.execute("PRAGMA table_info(cookies)")
                        columns = [col[1] for col in cursor.fetchall()]

                        if "host_key" in columns:
                            # Новая структура (Chrome 5+, Vivaldi)
                            cursor.execute("""
                                SELECT name, value, host_key FROM cookies 
                                WHERE host_key LIKE '%vk.com%' AND expires_utc > (strftime('%s', 'now') * 1000000)
                            """)
                            for name, value, host_key in cursor.fetchall():
                                cookies[name] = value
                        else:
                            # Старая структура
                            cursor.execute("""
                                SELECT name, value, host FROM cookies 
                                WHERE host LIKE '%vk.com%' AND expires_utc > (strftime('%s', 'now') * 1000000)
                            """)
                            for name, value, host in cursor.fetchall():
                                cookies[name] = value

                        conn.close()
                        browser_name = (
                            "Vivaldi"
                            if browser_type == "vivaldi"
                            else chrome_dir.name.replace("-", " ").title()
                        )
                        print(f"Cookie из {browser_name}: {len(cookies)} записей")
                        break
                    except Exception as e:
                        print(f"Ошибка чтения cookies: {e}")

    return cookies


def get_cookies_from_file(filepath):
    """Получение cookies из JSON файла"""
    # New format: list of cookie objects with domains (preferred)
    # Legacy format: dict name->value or list of {name,value}
    cookies: Dict[str, str] = {}
    cookie_items: List[Dict[str, Any]] = []
    try:
        with open(filepath, "r") as f:
            data = json.load(f)
            if isinstance(data, list):
                for item in data:
                    if "name" in item and "value" in item:
                        cookie_items.append(dict(item))
                        # Keep a simple dict for checks and vk.com requests.
                        # If duplicates exist across domains, vk.com cookie wins.
                        name = item.get("name")
                        val = item.get("value")
                        dom = (item.get("domain") or "").lstrip(".")
                        if name and val is not None:
                            if not dom or "vk.com" in dom:
                                cookies[str(name)] = str(val)
                            elif str(name) not in cookies:
                                cookies[str(name)] = str(val)
            elif isinstance(data, dict):
                cookies = {str(k): str(v) for k, v in data.items()}
                cookie_items = [{"name": k, "value": v, "domain": ".vk.com", "path": "/"} for k, v in cookies.items()]
        print(f"Загружено {len(cookie_items) or len(cookies)} cookie-записей из файла (уникальных имён: {len(cookies)})")
    except Exception as e:
        print(f"Ошибка чтения cookies файла: {e}")
    return {"_simple": cookies, "_items": cookie_items}


def save_cookies_to_file(cookies, filepath):
    """Сохранение cookies в JSON файл"""
    try:
        with open(filepath, "w") as f:
            if isinstance(cookies, dict) and "_items" in cookies:
                json.dump(cookies["_items"], f, ensure_ascii=False, indent=2)
            elif isinstance(cookies, dict):
                json.dump([{"name": k, "value": v} for k, v in cookies.items()], f, ensure_ascii=False, indent=2)
            else:
                json.dump(cookies, f, ensure_ascii=False, indent=2)
        print(f"Cookies сохранены в {filepath}")
    except Exception as e:
        print(f"Ошибка сохранения cookies: {e}")


def download_audio(audio, output_dir, session):
    """Скачивание одного аудио файла"""
    try:
        url = audio.get("url", "")
        if not url:
            return None, "Нет URL"

        debug = os.environ.get("VK_DEBUG", "").strip() not in {"", "0", "false", "False"}
        if debug:
            print(f"[LOG] download_audio url={url}")

        filename = build_track_filename(audio)
        filepath = ensure_unique_path(output_dir / filename)

        # Проверка существования
        if filepath.exists():
            return filepath, "Уже существует"

        # Если это HLS (m3u8) — качаем через ffmpeg и конвертим в mp3
        if ".m3u8" in url:
            import subprocess
            from urllib.parse import urljoin
            import re as _re
            import time as _time
            try:
                from Cryptodome.Cipher import AES
            except Exception:  # pragma: no cover
                from Crypto.Cipher import AES  # type: ignore

            tmp_path = filepath.with_suffix(".part.mp3")
            ua = session.headers.get("User-Agent", "")
            # Для некоторых CDN VK нужен referer/user-agent, иначе key/ts может отдавать 403
            headers = ""
            if ua:
                headers += f"User-Agent: {ua}\r\n"
            headers += "Referer: https://vk.com/\r\nOrigin: https://vk.com\r\n"

            # Логи + сохранение m3u8 рядом (для отладки)
            expected_duration = None
            ffmpeg_input = url
            m3u8_text = None
            if debug:
                try:
                    m3u8_text = session.get(url, timeout=20).text
                    m3u8_path = filepath.with_suffix(".m3u8")
                    m3u8_path.write_text(m3u8_text, encoding="utf-8", errors="ignore")
                    expected_duration = sum(
                        float(m.group(1))
                        for m in _re.finditer(r"#EXTINF:([0-9.]+)", m3u8_text)
                    )
                    # Rewrite to absolute URLs to make ffmpeg more reliable
                    abs_lines = []
                    for ln in m3u8_text.splitlines():
                        s = ln.strip()
                        if s.startswith("#EXT-X-KEY:") and 'URI="' in s:
                            s2 = _re.sub(r'URI="([^"]+)"', lambda mm: f'URI="{urljoin(url, mm.group(1))}"', s)
                            abs_lines.append(s2)
                            continue
                        if s and not s.startswith("#"):
                            abs_lines.append(urljoin(url, s))
                        else:
                            abs_lines.append(ln.rstrip("\n"))
                    abs_m3u8_path = filepath.with_suffix(".abs.m3u8")
                    abs_m3u8_path.write_text("\n".join(abs_lines) + "\n", encoding="utf-8", errors="ignore")
                    ffmpeg_input = str(abs_m3u8_path)
                    lines = [ln.strip() for ln in m3u8_text.splitlines() if ln.strip()]
                    print(f"[LOG] m3u8 saved: {m3u8_path}")
                    print("[LOG] m3u8 head:")
                    for ln in lines[:20]:
                        print("[LOG]  ", ln)
                    key_line = next((ln for ln in lines if ln.startswith("#EXT-X-KEY:")), None)
                    if key_line:
                        print("[LOG] m3u8 key:", key_line)
                    segs = [ln for ln in lines if not ln.startswith("#")]
                    print(f"[LOG] m3u8 segments: {len(segs)}")
                    for s in segs[:8]:
                        print("[LOG]  seg:", urljoin(url, s))
                except Exception as e:
                    print("[LOG] m3u8 debug fetch failed:", e)
            else:
                try:
                    m3u8_text = session.get(url, timeout=20).text
                    expected_duration = sum(
                        float(m.group(1))
                        for m in _re.finditer(r"#EXTINF:([0-9.]+)", m3u8_text)
                    )
                except Exception:
                    m3u8_text = None

            last_err = ""
            # Some ffmpeg builds don't support -reconnect* options.
            ffmpeg_help = ""
            try:
                ffmpeg_help = subprocess.run(
                    ["ffmpeg", "-hide_banner", "-h"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=3,
                ).stdout.decode(errors="ignore")
            except Exception:
                ffmpeg_help = ""
            supports_reconnect = "reconnect" in ffmpeg_help
            supports_headers = " -headers " in ffmpeg_help or "\n  -headers" in ffmpeg_help or "headers" in ffmpeg_help

            # If ffmpeg can't pass headers, download HLS ourselves and transcode locally.
            if not supports_headers:
                if debug:
                    print("[LOG] ffmpeg без -headers. Скачиваю HLS через requests и собираю локальный .ts")

                if not m3u8_text:
                    return None, "failed to fetch m3u8"

                ts_tmp = filepath.with_suffix(".part.ts")
                key_cache: Dict[str, bytes] = {}

                def fetch_key(key_url: str) -> bytes:
                    if key_url in key_cache:
                        return key_cache[key_url]
                    kb = session.get(key_url, timeout=30).content
                    key_cache[key_url] = kb
                    return kb

                media_seq = 0
                m = _re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", m3u8_text)
                if m:
                    media_seq = int(m.group(1))

                cur_method = "NONE"
                cur_key_url = None

                seg_urls: List[str] = []
                seg_encrypt: List[Tuple[str, Optional[str]]] = []

                lines_all = [ln.strip() for ln in m3u8_text.splitlines() if ln.strip()]
                for ln in lines_all:
                    if ln.startswith("#EXT-X-KEY:"):
                        if "METHOD=NONE" in ln:
                            cur_method = "NONE"
                            cur_key_url = None
                        else:
                            cur_method = "AES-128"
                            km = _re.search(r'URI="([^"]+)"', ln)
                            cur_key_url = urljoin(url, km.group(1)) if km else None
                        continue
                    if ln.startswith("#"):
                        continue
                    seg_urls.append(urljoin(url, ln))
                    seg_encrypt.append((cur_method, cur_key_url))

                if debug:
                    print(f"[LOG] HLS segments to fetch: {len(seg_urls)}")

                with open(ts_tmp, "wb") as out_ts:
                    for i, (seg_url, (meth, key_url)) in enumerate(zip(seg_urls, seg_encrypt)):
                        seg = session.get(seg_url, timeout=60).content
                        if meth == "AES-128" and key_url:
                            key = fetch_key(key_url)
                            # IV defaults to media sequence number (RFC8216)
                            iv_int = media_seq + i
                            iv = iv_int.to_bytes(16, "big")
                            cipher = AES.new(key[:16], AES.MODE_CBC, iv=iv)
                            seg = cipher.decrypt(seg)
                        out_ts.write(seg)

                # Now transcode local ts -> mp3
                proc = subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-loglevel",
                        ("warning" if debug else "error"),
                        "-i",
                        str(ts_tmp),
                        "-vn",
                        "-c:a",
                        "libmp3lame",
                        "-q:a",
                        "2",
                        str(tmp_path),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                last_err = proc.stderr.decode(errors="ignore").strip()
                if proc.returncode != 0:
                    if debug and last_err:
                        tail = "\n".join(last_err.splitlines()[-20:])
                        print("[LOG] ffmpeg stderr tail:\n" + tail)
                    return None, f"ffmpeg failed: {last_err[:400]}"
                tmp_path.replace(filepath)
                try:
                    ts_tmp.unlink(missing_ok=True)
                except Exception:
                    pass

            else:
                for attempt in range(1, 4):
                    cmd = [
                        "ffmpeg",
                        "-y",
                        "-loglevel",
                        ("warning" if debug else "error"),
                        "-protocol_whitelist",
                        "file,https,tcp,tls,crypto",
                        "-rw_timeout",
                        "60000000",
                        "-headers",
                        headers,
                        "-i",
                        ffmpeg_input,
                        "-vn",
                        "-c:a",
                        "libmp3lame",
                        "-q:a",
                        "2",
                        str(tmp_path),
                    ]
                    if supports_reconnect:
                        cmd[cmd.index("-protocol_whitelist") + 2 : cmd.index("-rw_timeout")] = [
                            "-reconnect",
                            "1",
                            "-reconnect_streamed",
                            "1",
                            "-reconnect_delay_max",
                            "10",
                        ]
                    if debug:
                        print(f"[LOG] ffmpeg attempt {attempt}/3 input={ffmpeg_input}")
                    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    last_err = proc.stderr.decode(errors="ignore").strip()
                    if proc.returncode != 0:
                        if debug and last_err:
                            tail = "\n".join(last_err.splitlines()[-20:])
                            print("[LOG] ffmpeg stderr tail:\n" + tail)
                        _time.sleep(1.0)
                        continue
                    tmp_path.replace(filepath)
                    break
                else:
                    return None, f"ffmpeg failed: {last_err[:400]}"

            # Контроль длительности: иногда ffmpeg "успешно" отдаёт обрезанный файл
            if debug and expected_duration:
                try:
                    p = subprocess.run(
                        [
                            "ffprobe",
                            "-v",
                            "error",
                            "-show_entries",
                            "format=duration",
                            "-of",
                            "default=nokey=1:noprint_wrappers=1",
                            str(filepath),
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=10,
                    )
                    got = float(p.stdout.decode().strip() or "0")
                    if got and got + 10 < expected_duration:
                        if last_err:
                            tail = "\n".join(last_err.splitlines()[-20:])
                            print("[LOG] ffmpeg stderr tail:\n" + tail)
                        return (
                            None,
                            f"ffmpeg produced truncated audio: got {got:.1f}s expected {expected_duration:.1f}s",
                        )
                except Exception:
                    pass

            # Cleanup sidecar m3u8 files (keep them only in debug)
            if not debug:
                try:
                    filepath.with_suffix(".m3u8").unlink(missing_ok=True)
                    filepath.with_suffix(".abs.m3u8").unlink(missing_ok=True)
                except Exception:
                    pass
        else:
            # Обычное скачивание
            file_response = session.get(url, timeout=30, stream=True)
            file_response.raise_for_status()

            with open(filepath, "wb") as f:
                for chunk in file_response.iter_content(chunk_size=8192):
                    f.write(chunk)

        return filepath, None

    except Exception as e:
        return None, str(e)


def _extract_first_http(obj):
    if isinstance(obj, str):
        return obj if obj.startswith("http") else None
    if isinstance(obj, list):
        for x in obj:
            v = _extract_first_http(x)
            if v:
                return v
    if isinstance(obj, dict):
        for x in obj.values():
            v = _extract_first_http(x)
            if v:
                return v
    return None


def _extract_audio_urls_from_payload(payload):
    """
    Пытаемся вытащить url из ответа al_audio.php?act=reload_audios.
    Обычно payload содержит список аудио, где url лежит в [0][2] или в dict.url.
    """
    urls = []
    if isinstance(payload, list):
        # Частый вариант: payload[1][0] = list of audios (each audio is list)
        # Но структура плавает — пройдём рекурсивно по спискам, где есть http.
        first = _extract_first_http(payload)
        if first:
            urls.append(first)
        # Ищем списки вида [.., .., "http..."]
        for el in payload:
            if isinstance(el, list) and len(el) > 2 and isinstance(el[2], str) and el[2].startswith("http"):
                urls.append(el[2])
            if isinstance(el, list):
                urls.extend(_extract_audio_urls_from_payload(el))
            elif isinstance(el, dict):
                urls.extend(_extract_audio_urls_from_payload(list(el.values())))
    elif isinstance(payload, dict):
        u = payload.get("url")
        if isinstance(u, str) and u.startswith("http"):
            urls.append(u)
        for v in payload.values():
            urls.extend(_extract_audio_urls_from_payload(v))
    return urls


def _pick_best_audio_url(urls: List[str]) -> Optional[str]:
    if not urls:
        return None
    # Убираем мусор/заглушки
    clean = []
    for u in urls:
        if not isinstance(u, str) or not u.startswith("http"):
            continue
        if "audio_api_unavailable" in u or "vk.com/mp3/" in u:
            continue
        clean.append(u)
    if not clean:
        return None

    # Приоритет: HLS m3u8 с vkuseraudio CDN
    for u in clean:
        if "vkuseraudio.net" in u and ".m3u8" in u:
            return u
    for u in clean:
        if ".m3u8" in u:
            return u
    # Потом прямые mp3 ссылки
    for u in clean:
        if u.endswith(".mp3") or ".mp3?" in u:
            return u
    return clean[0]


def resolve_audio_url_web(session: requests.Session, audio: dict, group_owner_id: Optional[int] = None) -> Optional[str]:
    """
    Если VK отдаёт заглушку/непрямую ссылку, пробуем получить реальный URL через al_audio.php act=reload_audio.
    Требуются рабочие cookies (сессия vk.com).
    """
    try:
        debug = os.environ.get("VK_DEBUG", "").strip() not in {"", "0", "false", "False"}

        # 0) Пробуем декодировать audio_api_unavailable (если есть декодер)
        u0 = audio.get("url") or ""
        if isinstance(u0, str) and "audio_api_unavailable" in u0 and decode_audio_url:
            # user_id попробуем взять из сессии/страницы
            user_id = getattr(session, "_vk_user_id", 0) or 0
            if not user_id:
                try:
                    html = session.get("https://vk.com/feed", timeout=15).text
                    m = re.search(r'"id"\s*:\s*(\d+)', html)
                    if m:
                        user_id = int(m.group(1))
                        setattr(session, "_vk_user_id", user_id)
                except Exception:
                    user_id = 0
            if user_id:
                try:
                    decoded = decode_audio_url(u0, user_id)
                    if debug:
                        print(f"[LOG] decode_audio_url user_id={user_id} -> {decoded}")
                    if decoded and decoded.startswith("http") and "audio_api_unavailable" not in decoded:
                        return decoded
                except Exception as e:
                    if debug:
                        print("[LOG] decode_audio_url failed:", e)

        oid = audio.get("owner_id")
        aid = audio.get("id")
        if oid is None or aid is None:
            return None
        action_hash = audio.get("action_hash") or audio.get("actionHash")
        url_hash = audio.get("url_hash") or audio.get("urlHash")
        hashes_raw = audio.get("hashes_raw") or ""
        # Для reload_audios обычно нужен полный id с хэшами: owner_id_audio_id_actionHash_urlHash
        if action_hash and url_hash:
            ids = f"{oid}_{aid}_{action_hash}_{url_hash}"
        else:
            ids = f"{oid}_{aid}"
        # Для reload_audio достаточно owner_id_id в залогиненной сессии (как в браузерных сниппетах)
        r = session.post(
            "https://vk.com/al_audio.php",
            data={"act": "reload_audios", "al": 1, "ids": ids},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://vk.com/",
                "Origin": "https://vk.com",
            },
            timeout=20,
        )
        txt = (r.text or "").replace("<!--", "")
        data = json.loads(txt)
        payload = data.get("payload")
        url = None
        urls = _extract_audio_urls_from_payload(payload)
        url = _pick_best_audio_url(urls)
        if not url:
            url = _extract_first_http(payload)
        if debug:
            sample = urls[:5]
            print(
                f"[LOG] reload_audios ids={ids} status={r.status_code} urls_found={len(urls)} sample={sample} resolved_url={url}"
            )
        if url and "audio_api_unavailable" not in url and "vk.com/audio" not in url and "vk.com/mp3/" not in url:
            return url

        # Fallback: emulate browser sequence queue_params -> start_playback
        # Need hash used by al_audio.php, it is usually embedded in hashes_raw.
        candidates = []
        if isinstance(hashes_raw, str) and hashes_raw:
            for part in hashes_raw.split("/"):
                if re.fullmatch(r"[0-9a-f]{16,40}", part):
                    candidates.append(part)
        # Prefer 20-hex hash (seen in CDP: 7b6b... length 20)
        candidates.sort(key=lambda s: (0 if len(s) == 20 else 1, len(s)))
        play_hash = candidates[0] if candidates else None
        if debug:
            print(f"[LOG] start_playback candidates={candidates[:5]} chosen={play_hash}")
        if not play_hash:
            return None

        # queue_params
        q = session.post(
            "https://vk.com/al_audio.php",
            data={
                "act": "queue_params",
                "al": 1,
                "audio_id": aid,
                "owner_id": oid,
                "hash": play_hash,
            },
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://vk.com/",
                "Origin": "https://vk.com",
            },
            timeout=20,
        )
        # start_playback
        uuid = hashlib.md5(f"{time.time()}_{oid}_{aid}".encode()).hexdigest()
        s = session.post(
            "https://vk.com/al_audio.php",
            data={
                "act": "start_playback",
                "al": 1,
                "audio_id": aid,
                "owner_id": oid,
                "hash": play_hash,
                "uuid": uuid,
            },
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://vk.com/",
                "Origin": "https://vk.com",
            },
            timeout=20,
        )
        if debug:
            print(f"[LOG] queue_params status={q.status_code} start_playback status={s.status_code}")
        try:
            raw2 = (s.text or "").replace("<!--", "")
            payload2 = json.loads(raw2).get("payload")
            urls2 = _extract_audio_urls_from_payload(payload2)
            best2 = _pick_best_audio_url(urls2)
            if debug:
                print(f"[LOG] start_playback urls_found={len(urls2)} best={best2}")
            if best2:
                return best2
        except Exception:
            if debug:
                print("[LOG] start_playback parse failed, head=", (s.text or "")[:300])
            return None
    except Exception:
        return None
    return None


def sanitize_filename(name, max_len=200):
    name = (name or "").strip()
    if not name:
        return "unknown"
    for c in r'\/:*?"<>|':
        name = name.replace(c, "_")
    name = re.sub(r"\s+", " ", name).strip()
    if len(name) > max_len:
        name = name[:max_len].rstrip()
    return name


def build_track_filename(audio):
    artist = sanitize_filename((audio.get("artist") or "Unknown Artist")[:100])
    title = sanitize_filename((audio.get("title") or "Unknown Title")[:100])
    return f"{artist} - {title}.mp3"


def ensure_unique_path(path: Path):
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for i in range(2, 10000):
        candidate = path.with_name(f"{stem} ({i}){suffix}")
        if not candidate.exists():
            return candidate
    return path


def token_seems_valid(downloader: VKAudioDownloader):
    data = downloader._api_request("users.get", {})
    if "error" in data:
        msg = (data.get("error") or {}).get("error_msg", "")
        return False, msg
    return True, ""


def read_pasted_json(prompt: str) -> str:
    print(prompt)
    print("Вставь JSON целиком и заверши строкой: END")
    lines = []
    while True:
        try:
            line = sys.stdin.readline()
        except KeyboardInterrupt:
            return ""
        if not line:
            break
        if line.strip() == "END":
            break
        lines.append(line)
    return "".join(lines).strip()


def load_cookies_from_browser_auto() -> dict:
    """
    Пытаемся забрать cookies vk.com из браузера (Vivaldi/Chrome/Chromium).
    Это единственный способ получить HttpOnly cookies без ручной копипасты.
    """
    if browser_cookie3 is None:
        return {}

    jar = None
    errors = []
    source = None
    for name, fn in [
        ("vivaldi", getattr(browser_cookie3, "vivaldi", None)),
        ("chrome", getattr(browser_cookie3, "chrome", None)),
        ("chromium", getattr(browser_cookie3, "chromium", None)),
    ]:
        if not fn:
            continue
        try:
            jar = fn(domain_name="vk.com")
            if jar:
                source = name
                break
        except Exception as e:
            errors.append(f"{name}: {e}")

    cookies = {}
    if jar:
        for c in jar:
            # берём только vk.com и поддомены
            if "vk.com" not in (c.domain or ""):
                continue
            cookies[c.name] = c.value
        # служебное поле, чтобы в логах было понятно откуда взяли
        cookies["_source"] = source or "unknown"
    else:
        if errors:
            print("[LOG] Авто-извлечение cookies упало:", " | ".join(errors))
    return cookies


def load_cookies_via_cdp(devtools_http_base: str = "http://127.0.0.1:9222") -> dict:
    """
    Достаём cookies из работающего браузера через Chrome DevTools Protocol.
    Требует запуска браузера с --remote-debugging-port=9222.
    """
    if _requests_for_cdp is None or _cdp_ws_create is None:
        return {}

    def cdp_call(ws, method, params=None, call_id=1, timeout=8.0):
        params = params or {}
        ws.send(json.dumps({"id": call_id, "method": method, "params": params}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = json.loads(ws.recv())
            if data.get("id") == call_id:
                if "error" in data:
                    raise RuntimeError(data["error"])
                return data.get("result") or {}
        raise TimeoutError(method)

    r = _requests_for_cdp.get(f"{devtools_http_base}/json", timeout=3)
    r.raise_for_status()
    targets = r.json()
    ws_url = None
    for t in targets:
        if t.get("type") == "page" and "vk.com" in (t.get("url") or "") and t.get("webSocketDebuggerUrl"):
            ws_url = t["webSocketDebuggerUrl"]
            break
    if not ws_url:
        for t in targets:
            if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                ws_url = t["webSocketDebuggerUrl"]
                break
    if not ws_url:
        return {}

    ws = _cdp_ws_create(ws_url, timeout=10)
    try:
        cdp_call(ws, "Network.enable", {}, call_id=1)
        res = cdp_call(ws, "Network.getAllCookies", {}, call_id=2)
        cookie_items: List[Dict[str, Any]] = []
        cookies_simple: Dict[str, str] = {}
        for c in (res.get("cookies") or []):
            domain = c.get("domain") or ""
            if ("vk.com" not in domain) and ("vkvideo.ru" not in domain):
                continue
            name = c.get("name")
            val = c.get("value")
            if name and val is not None:
                cookie_items.append(
                    {
                        "name": name,
                        "value": val,
                        "domain": c.get("domain"),
                        "path": c.get("path") or "/",
                        "secure": bool(c.get("secure")),
                        "httpOnly": bool(c.get("httpOnly")),
                        "expires": c.get("expires"),
                    }
                )
                dom = (domain or "").lstrip(".")
                if not dom or "vk.com" in dom:
                    cookies_simple[str(name)] = str(val)
                elif str(name) not in cookies_simple:
                    cookies_simple[str(name)] = str(val)
        if cookie_items:
            cookies_simple["_source"] = "cdp"
        return {"_simple": cookies_simple, "_items": cookie_items}
    finally:
        try:
            ws.close()
        except Exception:
            pass


def load_or_create_cookies_json(cookies_path: str) -> dict:
    path = Path(cookies_path)
    if path.exists():
        cookies = get_cookies_from_file(str(path))
        return cookies

    # 1) Авто-попытка вытащить cookies из браузера (через файлы)
    cookies = load_cookies_from_browser_auto()
    if cookies:
        src = cookies.pop("_source", "unknown")
        names = sorted(cookies.keys())
        print(f"[LOG] Автоматически получил cookies из браузера ({src}): {len(cookies)}")
        print("[LOG] Cookie names:", ", ".join(names))
        # Важно: НЕ перетирать cookies.json неполными cookies без session (remixsid*)
        has_session = any(k.startswith("remixsid") for k in cookies.keys())
        if has_session:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "w") as f:
                    json.dump(
                        [{"name": k, "value": v} for k, v in cookies.items()],
                        f,
                        ensure_ascii=False,
                        indent=2,
                    )
                print("[LOG] Сохранил cookies в", path)
            except Exception as e:
                print("[LOG] Не удалось сохранить cookies.json:", e)
            return cookies
        print("[LOG] Эти cookies без remixsid* -> не сохраняю и пробую CDP.")

    # 2) Авто-попытка через CDP (если включён remote debugging)
    try:
        cookies = load_cookies_via_cdp()
    except Exception as e:
        cookies = {}
        print("[LOG] CDP cookies не удалось получить:", e)
    if cookies:
        cookies_simple = cookies.get("_simple") if isinstance(cookies, dict) else None
        cookies_items = cookies.get("_items") if isinstance(cookies, dict) else None
        src = ""
        if isinstance(cookies_simple, dict):
            src = cookies_simple.pop("_source", "cdp")
        print(f"[LOG] Автоматически получил cookies через CDP ({src or 'cdp'}): {len(cookies_items or [])}")
        if isinstance(cookies_items, list):
            print("[LOG] Cookie names:", ", ".join(sorted({c.get('name') for c in cookies_items if c.get('name')})))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as f:
                json.dump(
                    (cookies_items or []),
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            print("[LOG] Сохранил cookies в", path)
        except Exception as e:
            print("[LOG] Не удалось сохранить cookies.json:", e)
        return cookies

    print("[LOG] Файл cookies не найден:", path)
    print()
    print("Авто-извлечение cookies не сработало.")
    print("Чаще всего причина: браузер хранит cookies шифрованными и системе неоткуда взять ключ.")
    print()
    print("Вариант без DevTools: запусти браузер с CDP и дай скрипту забрать cookies.")
    print("  1) Закрой Vivaldi полностью")
    print("  2) Запусти: vivaldi --remote-debugging-port=9222")
    print("  3) Открой vk.com, залогинься")
    print("  4) Запусти скрипт снова")
    print()
    print("Fallback: вставка cookies JSON (если у тебя есть откуда взять).")
    print()
    pasted = read_pasted_json("Вставь сюда этот JSON:")
    if not pasted:
        return {}
    try:
        data = json.loads(pasted)
    except Exception as e:
        print("[LOG] Не похоже на JSON:", e)
        return {}

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print("[LOG] Сохранил cookies в", path)
    except Exception as e:
        print("[LOG] Не удалось сохранить cookies.json:", e)
        return {}

    return get_cookies_from_file(str(path))


def cookies_seem_valid(session: requests.Session) -> Tuple[bool, str]:
    """
    Быстрая проверка: если vk.com/m.vk.com кидает на логин или отдаёт форму логина — cookies протухли.
    """
    try:
        r = session.get("https://m.vk.com/", timeout=15, allow_redirects=True)
    except Exception as e:
        return False, f"ошибка сети: {e}"
    t = (r.text or "").lower()
    if "login" in r.url or "act=login" in r.url:
        return False, "редирект на логин (cookies протухли)"
    if "name=\"email\"" in t and "name=\"pass\"" in t:
        return False, "похоже на страницу логина (cookies протухли)"
    return True, ""


def cookies_look_complete(cookies: dict) -> Tuple[bool, str]:
    """
    Частая проблема: копируют только 2-3 cookies (remixstlid/remixlgck),
    но для залогиненной сессии обычно нужен session cookie (часто remixsid*).
    """
    if not cookies:
        return False, "cookies пустые"
    # If passed composite structure, validate vk.com simple dict
    if isinstance(cookies, dict) and "_simple" in cookies and isinstance(cookies.get("_simple"), dict):
        cookies = cookies["_simple"]  # type: ignore[assignment]
    # VK меняет имя session cookie (remixsid, remixsid6, ...)
    has_session = any(k.startswith("remixsid") for k in cookies.keys())
    if not has_session:
        return False, "не вижу session cookie remixsid* (значит сессия не извлеклась, авторизации нет)"
    return True, ""


def check_audio_playlists_access(downloader: VKAudioDownloader) -> Tuple[bool, str]:
    """
    Проверяем доступ именно к аудио-страницам, а не к главной m.vk.com.
    """
    try:
        owner_id = downloader.group_owner_id()
    except Exception as e:
        return False, f"не удалось определить owner_id: {e}"

    try:
        r = downloader.session.get(
            f"https://m.vk.com/audio?act=audio_playlists{owner_id}",
            params={"offset": 0},
            timeout=20,
            allow_redirects=True,
            headers={"Referer": "https://m.vk.com/"},
        )
    except Exception as e:
        return False, f"ошибка сети: {e}"

    t = (r.text or "").lower()
    print("[LOG] audio_check:", "status=", r.status_code, "url=", r.url)
    if "login" in r.url or "act=login" in r.url:
        return False, "редирект на логин на audio-странице (cookies протухли)"
    if "name=\"email\"" in t and "name=\"pass\"" in t:
        return False, "страница логина на audio-странице (cookies протухли)"
    if "access denied" in t or "доступ запрещ" in t:
        return False, "доступ к audio закрыт (страница access denied). Это не 'протухшие cookies', это ограничение доступа."
    if "err" in t and "blocked" in t:
        return False, "похоже на блокировку/ограничение на стороне VK"
    # Если страница открылась — считаем cookies валидными для audio
    return True, ""

def main():
    import argparse

    parser = argparse.ArgumentParser(
        prog="vk_audio_downloader.py",
        description="Download VK group audio by playlists/albums.",
    )
    parser.add_argument(
        "--what",
        default="audio",
        help="What to download: audio, photos, videos, clips, all (comma-separated). Default: audio",
    )
    parser.add_argument(
        "--group",
        default=None,
        help="Group screen name or id (e.g. settlersspb or -179835916).",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output directory (default: ./out/<group>/audio).",
    )
    parser.add_argument(
        "--cookies-path",
        default="cookies.json",
        help="Path to cookies.json (default: cookies.json).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logs (same as VK_DEBUG=1).",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=4,
        help="Parallel downloads for audio (default: 4).",
    )
    parser.add_argument(
        "--vkvideo-access-token",
        default=None,
        help="Optional vkvideo.ru access_token for api.vkvideo.ru (used to get clip titles). Can also be set via VKVIDEO_ACCESS_TOKEN env.",
    )
    parser.add_argument(
        "--vkvideo-section-id",
        default=None,
        help="Optional section_id for catalog.getSection (used to get clip titles). Can also be set via VKVIDEO_SECTION_ID env.",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Only list items (videos/clips/playlists) and exit without downloading.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Non-interactive mode: download all listed items without prompting.",
    )
    parser.add_argument(
        "--clip-desc",
        action="store_true",
        help="Fetch clip descriptions for selected items via yt-dlp metadata (no download).",
    )
    parser.add_argument(
        "--ytdlp-bin",
        default=None,
        help="Path to yt-dlp executable (recommended on Python 3.8). Can also be set via YTDLP_BIN env.",
    )
    args, _unknown = parser.parse_known_args()

    if args.debug:
        os.environ["VK_DEBUG"] = "1"

    print("=" * 60)
    print("VK Audio Downloader")
    print("=" * 60)
    print()

    what_raw = (args.what or "audio").strip().lower()
    what_set = set()
    for part in what_raw.split(","):
        p = part.strip()
        if not p:
            continue
        if p == "all":
            what_set.update({"audio", "photos", "videos", "clips"})
        else:
            what_set.add(p)
    unknown = sorted([w for w in what_set if w not in {"audio", "photos", "videos", "clips"}])
    if unknown:
        print("[LOG] Unknown --what values:", ", ".join(unknown))
        return

    cookies_path = args.cookies_path
    cookies = {}
    cookie_items: List[Dict[str, Any]] = []
    print("Авторизация: cookies (CDP/браузер), без логина/пароля.")
    cookies_data = load_or_create_cookies_json(cookies_path)
    if not cookies_data:
        print("[LOG] Cookies не получены. Завершение.")
        return

    if isinstance(cookies_data, dict) and "_simple" in cookies_data and "_items" in cookies_data:
        cookies = cookies_data.get("_simple") or {}
        cookie_items = cookies_data.get("_items") or []
    elif isinstance(cookies_data, dict):
        cookies = cookies_data
        cookie_items = [{"name": k, "value": v, "domain": ".vk.com", "path": "/"} for k, v in cookies.items() if not str(k).startswith("_")]
    else:
        cookies = {}
        cookie_items = []

    ok, reason = cookies_look_complete(cookies_data if isinstance(cookies_data, dict) else {})
    if not ok:
        print("[LOG] НЕ АВТОРИЗОВАН:", reason)
        print("[LOG] Cookie names:", ", ".join(sorted({c.get("name") for c in cookie_items if c.get("name")})))
        return

    # Сначала проверяем что cookies живые, без запроса группы
    print()
    print("[LOG] Проверка cookies...")
    probe = VKAudioDownloader("-1", cookies=cookies_data, access_token=None)
    ok, reason = cookies_seem_valid(probe.session)
    if not ok:
        print("[LOG] Cookies недействительны:", reason)
        return

    # Group ID из URL: https://vk.com/audios-179835916?section=playlists
    try:
        GROUP_ID = args.group or input("ID группы (например settlersspb или -179835916): ").strip()
    except KeyboardInterrupt:
        print("\n[LOG] Отмена.")
        return
    if not GROUP_ID:
        GROUP_ID = "-179835916"

    # Инициализация загрузчика (cookies + группа)
    print()
    print("[LOG] Инициализация загрузчика...")
    downloader = VKAudioDownloader(GROUP_ID, cookies=cookies_data, access_token=None)

    print("[LOG] Проверка доступа к группе...")
    try:
        _ = downloader.group_owner_id()
    except Exception as e:
        print(f"[LOG] Ошибка: {e}")
        return

    # Base output dir
    gid_num = abs(downloader.group_owner_id())
    group_slug = sanitize_filename(GROUP_ID) if GROUP_ID else f"group_{gid_num}"
    if group_slug.lstrip("-").isdigit():
        group_slug = f"group_{gid_num}"
    base_out = Path(args.out) if args.out else (Path.cwd() / "out" / group_slug)
    base_out.mkdir(parents=True, exist_ok=True)

    def _vkvideo_handles() -> List[str]:
        """
        Build possible vkvideo.ru /@handle candidates from CLI group id.
        """
        s = (GROUP_ID or "").strip()
        handles: List[str] = []
        if not s:
            return handles
        if s.startswith("@"):
            handles.append(s[1:])
            return handles
        # numeric / club123 / public123
        if re.match(r"^-?\d+$", s) or s.lower().startswith("club") or s.lower().startswith("public"):
            gid = abs(int(downloader.group_owner_id()))
            handles.append(f"club{gid}")
            handles.append(f"public{gid}")
            handles.append(str(gid))
            return handles
        # screen name
        handles.append(s)
        return handles

    def _is_generic_vk_title(t: str) -> bool:
        tl = (t or "").strip().lower()
        if not tl:
            return True
        bad = {
            "video embed",
            "vk видео — смотреть онлайн бесплатно",
            "vk video",
        }
        return tl in bad

    def fetch_vkvideo_title_map(wanted_keys: List[str]) -> Dict[str, str]:
        """
        Try to map video keys like '-179_456' -> human title using vkvideo.ru catalog HTML.
        """
        debug = os.environ.get("VK_DEBUG", "").strip() not in {"", "0", "false", "False"}
        wanted = set(wanted_keys)
        out: Dict[str, str] = {}

        for h in _vkvideo_handles():
            page_url = f"https://vkvideo.ru/@{h}/all"
            try:
                r = downloader.session.get(
                    page_url,
                    timeout=25,
                    headers={
                        "Referer": "https://vkvideo.ru/",
                        "Origin": "https://vkvideo.ru",
                    },
                )
                html = r.text or ""
            except Exception as e:
                if debug:
                    print("[LOG] vkvideo fetch failed:", page_url, e)
                continue

            if debug:
                print("[LOG] vkvideo:", page_url, "HTTP", getattr(r, "status_code", "?"), "len", len(html))
                try:
                    dbg = base_out / "_debug_titles" / f"vkvideo__{sanitize_filename(h, max_len=80)}__all.html"
                    dbg.parent.mkdir(parents=True, exist_ok=True)
                    dbg.write_text(html, encoding="utf-8", errors="ignore")
                    print("[LOG] debug saved:", dbg)
                except Exception:
                    pass

            # Fast path: find anchors to videos and nearby aria-label/title
            for key in list(wanted):
                if key in out and not _is_generic_vk_title(out.get(key, "")):
                    continue
                # href may be /video-... or https://vkvideo.ru/video-...
                m = re.search(
                    rf'(?is)href="[^"]*video{re.escape(key)}[^"]*"[^>]*?(?:aria-label|title)="([^"]+)"',
                    html,
                )
                if not m:
                    m = re.search(
                        rf'(?is)(?:aria-label|title)="([^"]+)"[^>]*href="[^"]*video{re.escape(key)}[^"]*"',
                        html,
                    )
                if m:
                    title = _html.unescape(m.group(1)).strip()
                    if title and not _is_generic_vk_title(title):
                        out[key] = title

            # JSON-ish fragments sometimes include md_title near video id
            if wanted - set(out.keys()):
                for key in list(wanted):
                    if key in out and not _is_generic_vk_title(out.get(key, "")):
                        continue
                    m = re.search(rf'video{re.escape(key)}[\s\S]{{0,800}}?"md_title"\s*:\s*"([^"]+)"', html)
                    if m:
                        title = _html.unescape(m.group(1)).strip()
                        if title and not _is_generic_vk_title(title):
                            out[key] = title

            # If we found something meaningful, stop trying other handle guesses
            if out:
                break

        return out

    def cookies_to_netscape(cookie_path: Path):
        lines = ["# Netscape HTTP Cookie File\n"]
        # Prefer domain-aware cookie list
        if cookie_items:
            for c in cookie_items:
                name = c.get("name")
                value = c.get("value")
                if not name or value is None:
                    continue
                domain = c.get("domain") or ".vk.com"
                dom = str(domain).lstrip(".")
                path = c.get("path") or "/"
                secure = "TRUE" if c.get("secure") else "FALSE"
                expires = "0"
                lines.append(f".{dom}\tTRUE\t{path}\t{secure}\t{expires}\t{name}\t{value}\n")
        else:
            for name, value in (cookies or {}).items():
                if not name or name.startswith("_"):
                    continue
                lines.append(f".vk.com\tTRUE\t/\tTRUE\t0\t{name}\t{value}\n")
        cookie_path.write_text("".join(lines), encoding="utf-8")

    def run_ytdlp(url: str, out_dir: Path):
        out_dir.mkdir(parents=True, exist_ok=True)
        cookies_txt = base_out / "cookies.txt"
        cookies_to_netscape(cookies_txt)
        # Prefer yt-dlp executable; python module requires Python >= 3.10 in recent versions.
        exe = args.ytdlp_bin or os.environ.get("YTDLP_BIN") or shutil.which("yt-dlp")
        if exe and not os.access(exe, os.X_OK):
            exe = None
        ua = downloader.session.headers.get(
            "User-Agent",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )

        def build_cmd(use_headers: bool) -> List[str]:
            common = [
                "--cookies",
                str(cookies_txt),
                "--user-agent",
                ua,
                "-P",
                str(out_dir),
                "-o",
                "%(title).200s [%(id)s].%(ext)s",
            ]
            if use_headers:
                common += [
                    "--add-header",
                    "Referer:https://vk.com/",
                    "--add-header",
                    "Origin:https://vk.com",
                    "--add-header",
                    "Accept-Language:ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                ]
            if exe:
                return [exe] + common + [url]
            # Don't try importing yt_dlp on Python < 3.10 (it may not be installed or supported).
            if sys.version_info < (3, 10):
                return []
            return [sys.executable, "-m", "yt_dlp"] + common + [url]

        # Log yt-dlp version for debugging (old versions often fall back to generic extractor)
        try:
            if exe:
                ver_cmd = [exe, "--version"]
                ver = subprocess.run(ver_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True).stdout.strip()
                if ver:
                    print("[LOG] yt-dlp version:", ver)
            else:
                if sys.version_info < (3, 10):
                    print("[LOG] yt-dlp не найден/не исполняемый. На Python 3.8 нужен yt-dlp как бинарник.")
                    print("[LOG] Укажи путь через --ytdlp-bin или env YTDLP_BIN (пример: /usr/local/bin/yt-dlp).")
                    return
        except Exception:
            if not exe and sys.version_info < (3, 10):
                print("[LOG] yt-dlp не найден/не исполняемый. На Python 3.8 нужен yt-dlp как бинарник.")
                print("[LOG] Укажи путь через --ytdlp-bin или env YTDLP_BIN.")
                return

        print("[LOG] yt-dlp:", url)
        # First try with extra headers (helps avoid vk.com/badbrowser.php redirects)
        cmd = build_cmd(use_headers=True)
        if not cmd:
            return
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out = (p.stdout or "").strip()
        if out:
            # keep it compact in normal mode
            print(out)
        if p.returncode != 0:
            lower = out.lower()
            if "badbrowser.php" in lower or "unsupported url" in lower or "falling back on generic" in lower:
                print("[LOG] yt-dlp не смог обработать URL (badbrowser/generic). Пробую ещё раз без доп. заголовков...")
                cmd2 = build_cmd(use_headers=False)
                p2 = subprocess.run(cmd2, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                out2 = (p2.stdout or "").strip()
                if out2:
                    print(out2)
                if p2.returncode != 0:
                    print("[LOG] yt-dlp failed with code", p2.returncode)
                    return
            else:
                print("[LOG] yt-dlp failed with code", p.returncode)
            return

    def _fetch_text(url: str) -> str:
        try:
            r = downloader.session.get(url, timeout=20)
            return r.text or ""
        except Exception:
            return ""

    def _post_al(path: str, data: Dict[str, Any], referer: str) -> Tuple[int, str]:
        """
        VK internal AJAX endpoints (al_*.php) often return payload-like text.
        We don't try to fully parse it; we extract ids via regex.
        """
        try:
            headers = {
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "*/*",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": "https://vk.com",
                "Referer": referer,
            }
            r = downloader.session.post(
                f"https://vk.com/{path}",
                data=data,
                headers=headers,
                timeout=25,
            )
            return int(getattr(r, "status_code", 0) or 0), (r.text or "")
        except Exception as e:
            return 0, ""

    def _vkvideo_web_token(app_id: str = "52461373") -> str:
        """
        Obtain vkvideo web access_token via cookies:
        POST https://vkvideo.ru/al_video.php?act=web_token (version=1&app_id=...)
        """
        debug = os.environ.get("VK_DEBUG", "").strip() not in {"", "0", "false", "False"}
        try:
            r = downloader.session.post(
                "https://vkvideo.ru/al_video.php?act=web_token",
                data={"version": "1", "app_id": str(app_id)},
                headers={
                    "Accept": "*/*",
                    "Origin": "https://vkvideo.ru",
                    "Referer": "https://vkvideo.ru/",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=20,
            )
            text = r.text or ""
            if debug:
                print("[LOG] vkvideo web_token HTTP", getattr(r, "status_code", "?"), "len", len(text))
                try:
                    dbg = base_out / "_debug_titles" / "vkvideo_web_token__raw.txt"
                    dbg.parent.mkdir(parents=True, exist_ok=True)
                    dbg.write_text(text, encoding="utf-8", errors="ignore")
                except Exception:
                    pass
        except Exception as e:
            if debug:
                print("[LOG] vkvideo web_token failed:", e)
            return ""

        # Try json first
        try:
            j = r.json()
            if isinstance(j, dict):
                tok = (j.get("access_token") or j.get("token") or j.get("response", {}).get("access_token") or "").strip()
                if tok.startswith("vk1."):
                    return tok
        except Exception:
            pass

        # Fallback: regex in text
        m = re.search(r'"access_token"\s*:\s*"([^"]+)"', text)
        if m:
            tok = m.group(1).strip()
            if tok.startswith("vk1."):
                return tok
        m = re.search(r"\bvk1\.[A-Za-z0-9._-]+\b", text)
        if m:
            return m.group(0)
        return ""

    def _vkvideo_find_section_id(access_token: str, want_title: str = "Клипы") -> str:
        """
        Try to discover section_id via api.vkvideo.ru catalog.getVideoShowcase.
        vkvideo.ru itself calls it with url=https://vkvideo.ru/sidebar&need_blocks=1&access_token=...
        """
        debug = os.environ.get("VK_DEBUG", "").strip() not in {"", "0", "false", "False"}
        url = "https://api.vkvideo.ru/method/catalog.getVideoShowcase"
        params = {"v": "5.275", "client_id": "52461373"}
        headers = {
            "Accept": "*/*",
            "Origin": "https://vkvideo.ru",
            "Referer": "https://vkvideo.ru/",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            r = downloader.session.post(
                url,
                params=params,
                data={"url": "https://vkvideo.ru/sidebar", "need_blocks": "1", "access_token": access_token},
                headers=headers,
                timeout=25,
            )
            text = r.text or ""
        except Exception as e:
            if debug:
                print("[LOG] vkvideo showcase failed:", e)
            return ""

        if debug:
            print("[LOG] vkvideo showcase HTTP", getattr(r, "status_code", "?"), "len", len(text))
            try:
                dbg = base_out / "_debug_titles" / "vkvideo_api__catalog.getVideoShowcase.json"
                dbg.parent.mkdir(parents=True, exist_ok=True)
                dbg.write_text(text, encoding="utf-8", errors="ignore")
            except Exception:
                pass

        try:
            data = r.json()
        except Exception:
            return ""

        # normalize: {"response": {...}} or top-level
        root = data.get("response") if isinstance(data, dict) and isinstance(data.get("response"), dict) else data

        def iter_dicts(x):
            if isinstance(x, dict):
                yield x
                for v in x.values():
                    for y in iter_dicts(v):
                        yield y
            elif isinstance(x, list):
                for it in x:
                    for y in iter_dicts(it):
                        yield y

        want = (want_title or "").strip().lower()
        # Heuristic: find dict with id + title/breadcrumbs label "Клипы"
        for d in iter_dicts(root):
            if not isinstance(d, dict):
                continue
            sid = d.get("id")
            if not (isinstance(sid, str) and sid):
                continue
            title = (d.get("title") or "").strip().lower()
            if title and want and title == want:
                return sid
            crumbs = d.get("breadcrumbs")
            if isinstance(crumbs, list) and crumbs:
                for c in crumbs:
                    if isinstance(c, dict) and (c.get("label") or "").strip().lower() == want:
                        return sid

        # Fallback: first dict that looks like a section with title containing want
        for d in iter_dicts(root):
            sid = d.get("id")
            if not (isinstance(sid, str) and sid):
                continue
            title = (d.get("title") or "").strip().lower()
            if want and title and want in title:
                return sid
        return ""

    def _vkvideo_get_showcase(access_token: str, url_value: str) -> Dict[str, Any]:
        debug = os.environ.get("VK_DEBUG", "").strip() not in {"", "0", "false", "False"}
        url = "https://api.vkvideo.ru/method/catalog.getVideoShowcase"
        params = {"v": "5.275", "client_id": "52461373"}
        headers = {
            "Accept": "*/*",
            "Origin": "https://vkvideo.ru",
            "Referer": "https://vkvideo.ru/",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            r = downloader.session.post(
                url,
                params=params,
                data={"url": url_value, "need_blocks": "1", "access_token": access_token},
                headers=headers,
                timeout=25,
            )
            text = r.text or ""
        except Exception as e:
            if debug:
                print("[LOG] vkvideo showcase failed:", e)
            return {}
        if debug:
            try:
                dbg = base_out / "_debug_titles" / f"vkvideo_api__showcase__{sanitize_filename(url_value, max_len=80)}.json"
                dbg.parent.mkdir(parents=True, exist_ok=True)
                dbg.write_text(text, encoding="utf-8", errors="ignore")
            except Exception:
                pass
        try:
            return r.json() if isinstance(r.json(), dict) else {}
        except Exception:
            return {}

    def _vkvideo_pick_section_id_from_showcase(showcase: Dict[str, Any], owner_id: int, want_titles: List[str]) -> str:
        """
        Heuristic: walk showcase JSON and find a section_id/id that belongs to this owner_id
        and matches one of the wanted titles (case-insensitive). Returns best candidate or "".
        """
        if not isinstance(showcase, dict):
            return ""
        root = showcase.get("response") if isinstance(showcase.get("response"), dict) else showcase
        if not isinstance(root, dict):
            return ""

        want = {w.strip().lower() for w in (want_titles or []) if w and w.strip()}

        def iter_dicts(x):
            if isinstance(x, dict):
                yield x
                for v in x.values():
                    for y in iter_dicts(v):
                        yield y
            elif isinstance(x, list):
                for it in x:
                    for y in iter_dicts(it):
                        yield y

        candidates: List[Tuple[int, str]] = []  # (score, section_id)
        for d in iter_dicts(root):
            if not isinstance(d, dict):
                continue
            # must be related to owner_id
            layout = d.get("layout")
            oid = None
            if isinstance(layout, dict):
                oid = layout.get("owner_id")
            if oid != owner_id:
                continue
            # find any plausible section id in this dict
            sid = ""
            for k in ("section_id", "id"):
                v = d.get(k)
                if isinstance(v, str) and v and v.startswith("PU"):
                    sid = v
                    break
            if not sid:
                continue
            # score by title match if present
            score = 1
            t = (d.get("title") or "").strip().lower()
            if t and t in want:
                score += 10
            crumbs = d.get("breadcrumbs")
            if isinstance(crumbs, list):
                for c in crumbs:
                    if isinstance(c, dict) and (c.get("label") or "").strip().lower() in want:
                        score += 10
                        break
            candidates.append((score, sid))

        if not candidates:
            return ""
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    def _vkvideo_find_section_id_for_url(access_token: str, url_value: str, want_title: str) -> str:
        """
        Same as _vkvideo_find_section_id, but allows custom url=... (e.g. https://vkvideo.ru/@handle/all).
        """
        debug = os.environ.get("VK_DEBUG", "").strip() not in {"", "0", "false", "False"}
        url = "https://api.vkvideo.ru/method/catalog.getVideoShowcase"
        params = {"v": "5.275", "client_id": "52461373"}
        headers = {
            "Accept": "*/*",
            "Origin": "https://vkvideo.ru",
            "Referer": "https://vkvideo.ru/",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            r = downloader.session.post(
                url,
                params=params,
                data={"url": url_value, "need_blocks": "1", "access_token": access_token},
                headers=headers,
                timeout=25,
            )
            text = r.text or ""
        except Exception as e:
            if debug:
                print("[LOG] vkvideo showcase failed:", e)
            return ""

        if debug:
            print("[LOG] vkvideo showcase:", url_value, "HTTP", getattr(r, "status_code", "?"), "len", len(text))
            try:
                dbg = base_out / "_debug_titles" / f"vkvideo_api__showcase__{sanitize_filename(url_value, max_len=80)}.json"
                dbg.parent.mkdir(parents=True, exist_ok=True)
                dbg.write_text(text, encoding="utf-8", errors="ignore")
            except Exception:
                pass

        try:
            data = r.json()
        except Exception:
            return ""

        root = data.get("response") if isinstance(data, dict) and isinstance(data.get("response"), dict) else data

        def iter_dicts(x):
            if isinstance(x, dict):
                yield x
                for v in x.values():
                    for y in iter_dicts(v):
                        yield y
            elif isinstance(x, list):
                for it in x:
                    for y in iter_dicts(it):
                        yield y

        want = (want_title or "").strip().lower()
        for d in iter_dicts(root):
            if not isinstance(d, dict):
                continue
            sid = d.get("id")
            if not (isinstance(sid, str) and sid):
                continue
            title = (d.get("title") or "").strip().lower()
            if title and want and title == want:
                return sid
            crumbs = d.get("breadcrumbs")
            if isinstance(crumbs, list) and crumbs:
                for c in crumbs:
                    if isinstance(c, dict) and (c.get("label") or "").strip().lower() == want:
                        return sid
        for d in iter_dicts(root):
            sid = d.get("id")
            if not (isinstance(sid, str) and sid):
                continue
            title = (d.get("title") or "").strip().lower()
            if want and title and want in title:
                return sid
        return ""

    def _fetch_vkvideo_clips_catalog(
        owner_id: int, section_id: Optional[str], access_token: Optional[str]
    ) -> Tuple[List[str], Dict[str, str], Dict[str, str]]:
        """
        Best source of clip titles: api.vkvideo.ru catalog.getSection (cookie-based session + optional token).
        Returns (ids_in_order, title_map, desc_map) where ids are keys like "-oid_id".
        """
        debug = os.environ.get("VK_DEBUG", "").strip() not in {"", "0", "false", "False"}
        url = "https://api.vkvideo.ru/method/catalog.getSection"
        params = {
            "v": "5.275",
            "client_id": "52461373",
        }
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://vkvideo.ru",
            "Referer": "https://vkvideo.ru/",
        }
        try:
            if section_id and access_token:
                # This matches what vkvideo.ru XHR does: POST form-data with section_id + access_token.
                r = downloader.session.post(
                    url,
                    params=params,
                    data={"section_id": section_id, "access_token": access_token},
                    headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
                    timeout=25,
                )
            else:
                # Fallback: sometimes returns a useful section without explicit token.
                r = downloader.session.get(url, params=params, headers=headers, timeout=25)
            text = r.text or ""
        except Exception as e:
            if debug:
                print("[LOG] vkvideo api failed:", e)
            return [], {}, {}

        if debug:
            print("[LOG] vkvideo api:", url, "HTTP", getattr(r, "status_code", "?"), "len", len(text))
            try:
                dbg = base_out / "_debug_titles" / "vkvideo_api__catalog.getSection.json"
                dbg.parent.mkdir(parents=True, exist_ok=True)
                dbg.write_text(text, encoding="utf-8", errors="ignore")
                print("[LOG] debug saved:", dbg)
            except Exception:
                pass

        try:
            data = r.json()
        except Exception:
            return [], {}, {}

        # Response may be {"section": {...}} or {"response": {"section": {...}}}
        section = None
        if isinstance(data, dict):
            section = data.get("section")
            if section is None and isinstance(data.get("response"), dict):
                section = data["response"].get("section")
        if not isinstance(section, dict):
            return [], {}, {}

        ids_in_order: List[str] = []
        title_map: Dict[str, str] = {}
        desc_map: Dict[str, str] = {}

        # Prefer explicit order from videos_ids if present.
        vids = section.get("videos_ids")
        if isinstance(vids, list):
            for x in vids:
                if isinstance(x, str) and re.match(r"^-?\d+_\d+$", x.strip()):
                    ids_in_order.append(x.strip())

        videos = section.get("videos")
        if isinstance(videos, list):
            for v in videos:
                if not isinstance(v, dict):
                    continue
                oid = v.get("owner_id")
                vid = v.get("id")
                if not (isinstance(oid, int) and isinstance(vid, int)):
                    continue
                if oid != owner_id:
                    continue
                key = f"{oid}_{vid}"
                t = (v.get("title") or "").strip()
                if t and not _is_generic_vk_title(t):
                    title_map[key] = t
                dsc = (v.get("description") or "").strip()
                if dsc:
                    desc_map[key] = dsc
                if key not in ids_in_order:
                    ids_in_order.append(key)

        # If api returned items for a different owner (e.g. not logged / wrong context), ignore.
        # Keep only ids matching our owner_id.
        ids_in_order = [k for k in ids_in_order if k.startswith(f"{owner_id}_")]
        if not ids_in_order:
            return [], {}, {}
        return ids_in_order, title_map, desc_map

    def _fetch_vkvideo_titles_for_owner(owner_id: int, want_title: str) -> Dict[str, str]:
        """
        Fetch titles for a given owner_id and section ("Видео"/"Клипы") via vkvideo API.
        Returns key "-oid_id" -> title for items returned by that section.
        """
        debug = os.environ.get("VK_DEBUG", "").strip() not in {"", "0", "false", "False"}
        access_token = args.vkvideo_access_token or os.environ.get("VKVIDEO_ACCESS_TOKEN")
        if not access_token:
            access_token = _vkvideo_web_token("52461373")
            if debug and access_token:
                print("[LOG] vkvideo: получил web_token по cookies (titles).")
        if not access_token:
            return {}

        # Primary: sidebar showcase (stable) + owner_id filtering.
        showcase = _vkvideo_get_showcase(access_token, "https://vkvideo.ru/sidebar")
        section_id = _vkvideo_pick_section_id_from_showcase(
            showcase,
            owner_id=owner_id,
            want_titles=[want_title, "Видеозаписи", "Все видео", "Видео", "Клипы", "Clips", "Videos"],
        )
        if not section_id:
            # Secondary: try handle page (sometimes 104 Not found; keep as fallback)
            for h in _vkvideo_handles():
                showcase_url = f"https://vkvideo.ru/@{h}/all"
                section_id = _vkvideo_find_section_id_for_url(access_token, showcase_url, want_title=want_title)
                if section_id:
                    break
        if not section_id:
            return {}
            # Fetch section
            try:
                r = downloader.session.post(
                    "https://api.vkvideo.ru/method/catalog.getSection",
                    params={"v": "5.275", "client_id": "52461373"},
                    data={"section_id": section_id, "access_token": access_token},
                    headers={
                        "Accept": "application/json, text/plain, */*",
                        "Origin": "https://vkvideo.ru",
                        "Referer": "https://vkvideo.ru/",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    timeout=25,
                )
                text = r.text or ""
                if debug:
                    try:
                        dbg = base_out / "_debug_titles" / f"vkvideo_api__catalog.getSection__{sanitize_filename(want_title, max_len=30)}.json"
                        dbg.parent.mkdir(parents=True, exist_ok=True)
                        dbg.write_text(text, encoding="utf-8", errors="ignore")
                    except Exception:
                        pass
                data = r.json()
            except Exception:
                return {}
            section = None
            if isinstance(data, dict):
                section = data.get("section")
                if section is None and isinstance(data.get("response"), dict):
                    section = data["response"].get("section")
            if not isinstance(section, dict):
                return {}
            videos = section.get("videos")
            if not isinstance(videos, list):
                return {}
            out: Dict[str, str] = {}
            for v in videos:
                if not isinstance(v, dict):
                    continue
                oid = v.get("owner_id")
                vid = v.get("id")
                if not (isinstance(oid, int) and isinstance(vid, int)):
                    continue
                if oid != owner_id:
                    continue
                title = (v.get("title") or "").strip()
                if title and not _is_generic_vk_title(title):
                    out[f"{oid}_{vid}"] = title
            return out

    def _iter_unique_ids(pattern: str, text: str) -> List[str]:
        # Returns strings like "-123_456"
        seen = set()
        out = []
        for m in re.finditer(pattern, text):
            oid = m.group(1)
            vid = m.group(2)
            key = f"{oid}_{vid}"
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
        return out

    def _extract_titles_from_payload(kind: str, payload: str) -> Dict[str, str]:
        """
        Best-effort title extraction from VK al_*.php payloads.
        Returns mapping key ("-oid_id") -> title.
        """
        if not payload:
            return {}
        out: Dict[str, str] = {}

        # 1) JSON payload path: many endpoints return {"payload":[...]} with lists like
        #    [-oid, id, preview_url, "Title", ...]
        try:
            if payload.lstrip().startswith("{") and len(payload) < 20_000_000:
                j = json.loads(payload)

                def walk(x):
                    if isinstance(x, list):
                        # Candidate tuple: [owner_id, id, <str>, <title str>, ...]
                        if len(x) >= 4 and isinstance(x[0], int) and isinstance(x[1], int) and isinstance(x[3], str):
                            oid = x[0]
                            vid = x[1]
                            title = _html.unescape(x[3]).strip()
                            if title:
                                tl = title.lower()
                                if tl not in {"video embed", "vk видео — смотреть онлайн бесплатно", "vk video"}:
                                    out[f"{oid}_{vid}"] = title
                        for it in x:
                            walk(it)
                    elif isinstance(x, dict):
                        for v in x.values():
                            walk(v)

                walk(j)
        except Exception:
            pass

        # Common attributes in rendered snippets
        patterns = [
            # ... video-123_456 ... aria-label="Some title"
            rf'{kind}(-?\d+)_(\d+)[^<>\n]{{0,400}}?aria-label="([^"]+)"',
            rf'{kind}(-?\d+)_(\d+)[^<>\n]{{0,400}}?title="([^"]+)"',
            # data-title="Some title"
            rf'{kind}(-?\d+)_(\d+)[^<>\n]{{0,400}}?data-title="([^"]+)"',
            # JS-ish: "title":"Some title"
            rf'{kind}(-?\d+)_(\d+)[^<>\n]{{0,400}}?"title"\s*:\s*"([^"]+)"',
        ]
        for pat in patterns:
            for m in re.finditer(pat, payload, re.I):
                key = f"{m.group(1)}_{m.group(2)}"
                title = _html.unescape(m.group(3)).strip()
                if not title:
                    continue
                # Avoid generic wrappers
                tl = title.lower()
                if tl in {"video embed", "vk видео — смотреть онлайн бесплатно", "vk video"}:
                    continue
                if key not in out:
                    out[key] = title
        return out

    def _extract_vk_og_title(html: str) -> str:
        # Prefer og:title; fallback to <title>.
        if not html:
            return ""
        m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
        if not m:
            m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']', html, re.I)
        if m:
            return _html.unescape(m.group(1)).strip()
        m = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
        if m:
            t = re.sub(r"\s+", " ", m.group(1))
            return _html.unescape(t).strip()
        return ""

    def _extract_vk_og_description(html: str) -> str:
        if not html:
            return ""
        m = re.search(
            r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
            html,
            re.I,
        )
        if not m:
            m = re.search(
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:description["\']',
                html,
                re.I,
            )
        if m:
            return _html.unescape(m.group(1)).strip()
        return ""

    def _debug_save_html(name: str, html_text: str):
        debug = os.environ.get("VK_DEBUG", "").strip() not in {"", "0", "false", "False"}
        if not debug or not html_text:
            return
        try:
            dbg_dir = base_out / "_debug_titles"
            dbg_dir.mkdir(parents=True, exist_ok=True)
            p = dbg_dir / f"{sanitize_filename(name, max_len=120)}.html"
            p.write_text(html_text, encoding="utf-8", errors="ignore")
            print("[LOG] debug saved:", p)
        except Exception as e:
            print("[LOG] debug save failed:", e)

    def _debug_title_candidates(label: str, html_text: str):
        debug = os.environ.get("VK_DEBUG", "").strip() not in {"", "0", "false", "False"}
        if not debug or not html_text:
            return
        og = _extract_vk_og_title(html_text)
        md = ""
        mv = ""
        # VK player params sometimes have md_title / mvData.title
        m = re.search(r'"md_title"\s*:\s*"([^"]+)"', html_text)
        if m:
            md = _html.unescape(m.group(1))
        m = re.search(r'"mvData"\s*:\s*\{[^}]*"title"\s*:\s*"([^"]+)"', html_text)
        if m:
            mv = _html.unescape(m.group(1))
        if og or md or mv:
            print(f"[LOG] title candidates {label}: og={og!r} md_title={md!r} mvData.title={mv!r}")
        else:
            # print a short hint where og:title might be absent
            head = re.sub(r"\s+", " ", html_text[:300])
            print(f"[LOG] title candidates {label}: none. head={head!r}")

    def _get_vk_media_title(kind: str, key: str) -> str:
        # kind: "video" or "clip", key: "-oid_id"
        try:
            # Prefer embed page for videos/clips: it tends to contain real title without vkvideo wrappers
            if "_" in key:
                oid_s, id_s = key.split("_", 1)
                if oid_s.lstrip("-").isdigit() and id_s.isdigit():
                    embed_url = f"https://vk.com/video_ext.php?oid={oid_s}&id={id_s}&hd=2"
                    embed = _fetch_text(embed_url)
                    _debug_save_html(f"{kind}{key}__embed", embed)
                    _debug_title_candidates(f"{kind}{key} embed", embed)
                    t = _extract_vk_og_title(embed)
                    if t and "vk видео" not in t.lower() and "vk video" not in t.lower():
                        return t

            page_url = f"https://vk.com/{kind}{key}"
            page = _fetch_text(page_url)
            _debug_save_html(f"{kind}{key}__page", page)
            _debug_title_candidates(f"{kind}{key} page", page)
            title = _extract_vk_og_title(page)
            if title:
                return title
            return ""
        except Exception:
            return ""

    def ytdlp_metadata(url: str) -> Dict[str, Any]:
        """
        Get metadata without downloading (yt-dlp --dump-single-json --skip-download).
        """
        cookies_txt = base_out / "cookies.txt"
        cookies_to_netscape(cookies_txt)
        exe = args.ytdlp_bin or os.environ.get("YTDLP_BIN") or shutil.which("yt-dlp")
        if exe and not os.access(exe, os.X_OK):
            exe = None
        ua = downloader.session.headers.get(
            "User-Agent",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )
        cmd: List[str]
        if exe:
            cmd = [
                exe,
                "--cookies",
                str(cookies_txt),
                "--user-agent",
                ua,
                "--add-header",
                "Referer:https://vk.com/",
                "--add-header",
                "Origin:https://vk.com",
                "--skip-download",
                "--dump-single-json",
                url,
            ]
        else:
            if sys.version_info < (3, 10):
                return {}
            cmd = [
                sys.executable,
                "-m",
                "yt_dlp",
                "--cookies",
                str(cookies_txt),
                "--user-agent",
                ua,
                "--add-header",
                "Referer:https://vk.com/",
                "--add-header",
                "Origin:https://vk.com",
                "--skip-download",
                "--dump-single-json",
                url,
            ]
        try:
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if p.returncode != 0:
                return {}
            j = json.loads(p.stdout or "{}")
            return j if isinstance(j, dict) else {}
        except Exception:
            return {}

    def _fmt_title_for_list(title: str, max_len: int = 90) -> str:
        t = (title or "").strip()
        if not t:
            return ""
        t = re.sub(r"\s+", " ", t)
        return (t[: max_len - 1] + "…") if len(t) > max_len else t

    def _select_indices(total: int) -> List[int]:
        """
        Returns 0-based indices selected by user.
        Supports: all/y, n/no/нет, 1,2,5-10
        """
        print()
        print("Что скачать?")
        print("- all / y  : всё")
        print("- n        : ничего (выход)")
        print("- 1,2,5-10 : номера")
        sel = (input(">> ").strip() or "all").lower()
        if sel in {"n", "no", "нет"}:
            return []
        if sel in {"all", "y", "yes", "да"}:
            return list(range(total))
        indices: List[int] = []
        try:
            for part in (sel or "").split(","):
                part = part.strip()
                if not part:
                    continue
                if "-" in part:
                    a, b = map(int, part.split("-", 1))
                    for x in range(a, b + 1):
                        if 1 <= x <= total:
                            indices.append(x - 1)
                else:
                    x = int(part)
                    if 1 <= x <= total:
                        indices.append(x - 1)
        except Exception:
            return []
        # unique, keep order
        seen = set()
        out: List[int] = []
        for i in indices:
            if i in seen:
                continue
            seen.add(i)
            out.append(i)
        return out

    def download_group_videos_web(owner_id: int):
        # Listing pages are unstable for yt-dlp; scrape IDs and download direct video URLs.
        url = f"https://vk.com/videos{owner_id}"
        html = _fetch_text(url)
        ids = _iter_unique_ids(r"video(-?\d+)_(\d+)", html)
        titles: Dict[str, str] = {}
        if not ids:
            # Fallback: use internal AJAX endpoint used by the site itself.
            # This tends to work even when HTML is mostly scripts.
            print("[LOG] HTML videos пустой/без id. Пробую al_video.php (silent load)...")
            seen = set()
            offset = 0
            # 'section' can be 'all' for group/channel videos
            while True:
                st, payload = _post_al(
                    "al_video.php",
                    {
                        "act": "load_videos_silent",
                        "oid": str(owner_id),
                        "offset": str(offset),
                        "section": "all",
                        "al": "1",
                        "need_albums": "0",
                        "rowlen": "3",
                        "snippet_video": "0",
                    },
                    referer=url,
                )
                debug = os.environ.get("VK_DEBUG", "").strip() not in {"", "0", "false", "False"}
                if debug and payload and offset == 0:
                    try:
                        dbg = base_out / "_debug_titles" / "al_video__load_videos_silent__offset0.txt"
                        dbg.parent.mkdir(parents=True, exist_ok=True)
                        dbg.write_text(payload, encoding="utf-8", errors="ignore")
                        print("[LOG] debug saved:", dbg)
                    except Exception:
                        pass
                if st and st >= 400:
                    if debug:
                        print(f"[LOG] al_video.php HTTP {st} (offset={offset}) head:", payload[:200].replace("\n", " "))
                titles.update(_extract_titles_from_payload("video", payload))
                batch = _iter_unique_ids(r"video(-?\d+)_(\d+)", payload)
                new = [x for x in batch if x not in seen]
                for x in new:
                    seen.add(x)
                if not new:
                    break
                ids.extend(new)
                offset += len(new)
                if offset > 10000:
                    break

        if not ids:
            print("[LOG] Не удалось найти video-oid_id (ни HTML, ни al_video.php).")
            return
        out_dir = base_out / "videos"
        print(f"[LOG] Найдено видео: {len(ids)}")
        print("[LOG] Получаю названия...")
        vk_api_map = _fetch_vkvideo_titles_for_owner(owner_id, want_title="Видео")
        if vk_api_map:
            merged = 0
            for k, t in vk_api_map.items():
                if not t:
                    continue
                cur = titles.get(k, "")
                if (not cur) or _is_generic_vk_title(cur):
                    titles[k] = t
                    merged += 1
            print(f"[LOG] vkvideo API titles: {merged}/{len(ids)}")
        else:
            # Legacy HTML heuristic as last resort
            vk_map = fetch_vkvideo_title_map(ids)
            if vk_map:
                merged = 0
                for k, t in vk_map.items():
                    if not t:
                        continue
                    cur = titles.get(k, "")
                    if (not cur) or _is_generic_vk_title(cur):
                        titles[k] = t
                        merged += 1
                print(f"[LOG] vkvideo.ru titles: {merged}/{len(ids)}")
        items: List[Dict[str, str]] = []
        for k in ids:
            title = titles.get(k) or _get_vk_media_title("video", k)
            items.append({"key": k, "title": title})
        for i, it in enumerate(items, 1):
            t = _fmt_title_for_list(it.get("title", ""))
            if t:
                print(f"{i:3d}. {t} [video{it['key']}]")
            else:
                print(f"{i:3d}. video{it['key']}")
        if args.list_only:
            return
        chosen: Optional[List[int]] = None
        if sys.stdin.isatty():
            chosen = _select_indices(len(items))
            if not chosen:
                print("[LOG] Отмена.")
                return
        else:
            if not args.yes:
                print("[LOG] stdin не TTY. Чтобы скачать всё без вопросов, добавь --yes. Сейчас только показал список.")
                return
            chosen = list(range(len(items)))
        for idx in chosen:
            k = items[idx]["key"]
            run_ytdlp(f"https://vk.com/video{k}", out_dir)

    def download_group_clips_web(owner_id: int):
        # /clips{owner} is usually not matched by yt-dlp; scrape clip IDs and download direct clip URLs.
        vkvideo_access_token = args.vkvideo_access_token or os.environ.get("VKVIDEO_ACCESS_TOKEN")
        vkvideo_section_id = args.vkvideo_section_id or os.environ.get("VKVIDEO_SECTION_ID")
        if not vkvideo_access_token:
            vkvideo_access_token = _vkvideo_web_token("52461373")
            if vkvideo_access_token:
                print("[LOG] vkvideo: получил web_token по cookies.")
            else:
                print("[LOG] vkvideo: не удалось получить web_token по cookies.")
        if vkvideo_access_token and not vkvideo_section_id:
            # First try dedicated clips showcase URL; it often contains owner-specific sections.
            showcase = _vkvideo_get_showcase(vkvideo_access_token, "https://vkvideo.ru/clips")
            vkvideo_section_id = _vkvideo_pick_section_id_from_showcase(
                showcase,
                owner_id=owner_id,
                want_titles=["Клипы", "Clips"],
            )
            # Try handle-specific urls (some accounts get 104 on /all).
            if not vkvideo_section_id:
                for h in _vkvideo_handles():
                    for u in (f"https://vkvideo.ru/@{h}/clips", f"https://vkvideo.ru/@{h}"):
                        showcase_h = _vkvideo_get_showcase(vkvideo_access_token, u)
                        vkvideo_section_id = _vkvideo_pick_section_id_from_showcase(
                            showcase_h,
                            owner_id=owner_id,
                            want_titles=["Клипы", "Clips"],
                        )
                        if vkvideo_section_id:
                            break
                    if vkvideo_section_id:
                        break
            if not vkvideo_section_id:
                vkvideo_section_id = _vkvideo_find_section_id(vkvideo_access_token, want_title="Клипы")
            if vkvideo_section_id:
                print("[LOG] vkvideo: нашёл section_id для 'Клипы'.")
            else:
                print("[LOG] vkvideo: не удалось найти section_id (showcase).")

        api_ids, api_titles, api_desc = _fetch_vkvideo_clips_catalog(owner_id, vkvideo_section_id, vkvideo_access_token)
        titles: Dict[str, str] = {}
        descs: Dict[str, str] = {}
        ids: List[str] = []
        if api_ids:
            ids = list(api_ids)
            titles.update(api_titles)
            descs.update(api_desc)
            print(f"[LOG] vkvideo API: клипов {len(api_ids)}, titles {len(api_titles)}, desc {len(api_desc)}")

        url = f"https://vk.com/clips{owner_id}"
        html = _fetch_text(url)
        more = _iter_unique_ids(r"clip(-?\d+)_(\d+)", html)
        if more:
            # merge, keep order
            seen = set(ids)
            for k in more:
                if k not in seen:
                    ids.append(k)
                    seen.add(k)
        if not ids:
            # some pages reference clip as clip-oid_id
            ids = _iter_unique_ids(r"clip-(-?\d+)_(\d+)", html)
        if not ids:
            print("[LOG] HTML clips пустой/без id. Пробую al_clips.php (silent load)...")
            seen = set()
            offset = 0
            while True:
                st, payload = _post_al(
                    "al_clips.php",
                    {
                        "act": "load_clips_silent",
                        "oid": str(owner_id),
                        "offset": str(offset),
                        "al": "1",
                    },
                    referer=url,
                )
                if st and st >= 400:
                    debug = os.environ.get("VK_DEBUG", "").strip() not in {"", "0", "false", "False"}
                    if debug:
                        print(f"[LOG] al_clips.php HTTP {st} (offset={offset}) head:", payload[:200].replace("\n", " "))
                titles.update(_extract_titles_from_payload("clip", payload))
                batch = _iter_unique_ids(r"clip(-?\d+)_(\d+)", payload)
                if not batch:
                    batch = _iter_unique_ids(r"clip-(-?\d+)_(\d+)", payload)
                new = [x for x in batch if x not in seen]
                for x in new:
                    seen.add(x)
                if not new:
                    break
                ids.extend(new)
                offset += len(new)
                if offset > 10000:
                    break

        if not ids:
            # Fallback: sometimes clips are returned via al_video.php with section=clips
            print("[LOG] al_clips.php не дал id. Пробую al_video.php section=clips...")
            seen = set()
            offset = 0
            while True:
                st, payload = _post_al(
                    "al_video.php",
                    {
                        "act": "load_videos_silent",
                        "oid": str(owner_id),
                        "offset": str(offset),
                        "section": "clips",
                        "al": "1",
                    },
                    referer=f"https://vk.com/videos{owner_id}?section=clips",
                )
                debug = os.environ.get("VK_DEBUG", "").strip() not in {"", "0", "false", "False"}
                if debug and payload and offset == 0:
                    try:
                        dbg = base_out / "_debug_titles" / "al_video__load_videos_silent__section_clips__offset0.txt"
                        dbg.parent.mkdir(parents=True, exist_ok=True)
                        dbg.write_text(payload, encoding="utf-8", errors="ignore")
                        print("[LOG] debug saved:", dbg)
                    except Exception:
                        pass
                if st and st >= 400:
                    if debug:
                        print(f"[LOG] al_video.php(clips) HTTP {st} head:", payload[:200].replace("\n", " "))
                titles.update(_extract_titles_from_payload("clip", payload))
                batch = _iter_unique_ids(r"clip(-?\d+)_(\d+)", payload)
                new = [x for x in batch if x not in seen]
                for x in new:
                    seen.add(x)
                if not new:
                    break
                ids.extend(new)
                offset += len(new)
                if offset > 10000:
                    break

        if not ids:
            debug = os.environ.get("VK_DEBUG", "").strip() not in {"", "0", "false", "False"}
            if debug:
                st, payload = _post_al(
                    "al_clips.php",
                    {"act": "load_clips_silent", "oid": str(owner_id), "offset": "0", "al": "1"},
                    referer=url,
                )
                print("[LOG] al_clips.php debug HTTP", st, "len", len(payload), "head:", payload[:300].replace("\n", " "))
            print("[LOG] Не удалось найти clip-oid_id (ни HTML, ни AJAX).")
            return
        out_dir = base_out / "clips"
        print(f"[LOG] Найдено клипов: {len(ids)}")
        print("[LOG] Получаю названия...")
        items: List[Dict[str, str]] = []
        for k in ids:
            title = titles.get(k) or _get_vk_media_title("clip", k)
            desc = descs.get(k, "")
            items.append({"key": k, "title": title, "desc": desc})
        for i, it in enumerate(items, 1):
            t = _fmt_title_for_list(it.get("title", ""))
            d = _fmt_title_for_list(it.get("desc", ""), max_len=80)
            if t:
                if d:
                    print(f"{i:3d}. {t} — {d} [clip{it['key']}]")
                else:
                    print(f"{i:3d}. {t} [clip{it['key']}]")
            else:
                print(f"{i:3d}. clip{it['key']}")
        if args.list_only:
            return
        chosen: Optional[List[int]] = None
        if sys.stdin.isatty():
            chosen = _select_indices(len(items))
            if not chosen:
                print("[LOG] Отмена.")
                return
        else:
            if not args.yes:
                print("[LOG] stdin не TTY. Чтобы скачать всё без вопросов, добавь --yes. Сейчас только показал список.")
                return
            chosen = list(range(len(items)))

        if args.clip_desc:
            for idx in chosen:
                if items[idx].get("desc"):
                    continue
                k = items[idx]["key"]
                meta = ytdlp_metadata(f"https://vk.com/clip{k}")
                d = (meta.get("description") or "").strip()
                d = re.sub(r"\s+", " ", d)
                if d:
                    items[idx]["desc"] = d
                    # show what we found
                    print(f"[LOG] clip desc: clip{k} -> {_fmt_title_for_list(d, max_len=120)}")
        for idx in chosen:
            k = items[idx]["key"]
            run_ytdlp(f"https://vk.com/clip{k}", out_dir)

    # videos/clips/photos are handled by yt-dlp for now
    if "videos" in what_set:
        download_group_videos_web(downloader.group_owner_id())
    if "clips" in what_set:
        download_group_clips_web(downloader.group_owner_id())
    if "photos" in what_set:
        run_ytdlp(f"https://vk.com/albums{downloader.group_owner_id()}", base_out / "photos")

    if "audio" not in what_set:
        return

    audio_ok, audio_reason = check_audio_playlists_access(downloader)
    if not audio_ok:
        print("[LOG] Нет доступа к audio по cookies:", audio_reason)
        print("[LOG] Перехожу в режим плейлистов и пробую web-fallback (vk.com).")
        mode = "1"
    else:
        print()
        print("Что скачивать?")
        print("1. Плейлисты/альбомы группы (рекомендуется)")
        print("2. Все треки группы (как раньше)")
        mode = (input(">> ").strip() or "1").strip()

    playlist_jobs = []  # list of (playlist_title, playlist_id)
    audios = []

    if mode == "1":
        print("[LOG] Получение списка плейлистов...")
        playlists = downloader.list_playlists()
        if not playlists:
            if not audio_ok:
                print("[LOG] Плейлисты не найдены даже через web-fallback.")
                print("[LOG] Значит VK не отдаёт плейлисты этому аккаунту/сессии или верстка изменилась.")
            else:
                print("[LOG] Плейлисты не найдены.")
                print("[LOG] Если в браузере плейлисты видны — VK поменял верстку, нужно обновить парсер.")
            return
        else:
            print(f"[LOG] Найдено плейлистов: {len(playlists)}")
            for i, pl in enumerate(playlists, 1):
                title = pl.get("title") or "Без названия"
                cnt = pl.get("count") or pl.get("audio_count") or "?"
                pid = pl.get("id")
                print(f"{i:2d}. {title} (tracks: {cnt}) [id={pid}]")
            print()
            print("Что скачать?")
            print("- all / y  : все плейлисты")
            print("- n        : ничего (выход)")
            print("- 1,2,5-10 : номера плейлистов")
            sel = (input(">> ").strip() or "all").lower()

            if sel in {"n", "no", "нет"}:
                print("[LOG] Отмена.")
                return

            if sel in {"all", "y", "yes", "да"}:
                for pl in playlists:
                    if pl.get("id") is not None:
                        playlist_jobs.append(pl)
            else:
                indices = []
                try:
                    for part in (sel or "").split(","):
                        part = part.strip()
                        if not part:
                            continue
                        if "-" in part:
                            a, b = map(int, part.split("-", 1))
                            indices.extend(list(range(a, b + 1)))
                        else:
                            indices.append(int(part))
                except Exception:
                    indices = []

                if not indices:
                    print("[LOG] Неверный ввод. Ничего не скачиваю.")
                    return
                else:
                    for idx in indices:
                        if 1 <= idx <= len(playlists):
                            pl = playlists[idx - 1]
                            if pl.get("id") is not None:
                                playlist_jobs.append(pl)

            if not playlist_jobs:
                print("[LOG] Плейлисты не выбраны. Завершаю.")
                return

    if mode == "2":
        print()
        print("[LOG] Получение всех треков группы...")
        audios = downloader.get_all_audio()
        if not audios:
            print("[LOG] Аудио не найдено!")
            print("[ПОДСКАЗКА] Обычно нужен токен с правом `audio`.")
            return
        print(f"[LOG] Всего получено: {len(audios)} треков")

    # Audio output dir
    output_dir = base_out / "audio"
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nСохранение в: {output_dir}")
    print()

    # Шаг 5: Скачивание
    downloaded = 0
    errors = 0
    skipped = 0
    lock = threading.Lock()

    jobs = int(args.jobs or 4)
    if jobs < 1:
        jobs = 1

    # requests.Session is not thread-safe; create one per worker thread.
    _thread_local = threading.local()

    def _get_worker_session() -> requests.Session:
        s = getattr(_thread_local, "session", None)
        if s is not None:
            return s
        s = requests.Session()
        s.headers.update(dict(downloader.session.headers))
        for c in downloader.session.cookies:
            try:
                s.cookies.set(c.name, c.value, domain=c.domain, path=c.path)
            except Exception:
                s.cookies.set(c.name, c.value, domain=".vk.com")
        _thread_local.session = s
        return s

    def download_one(audio, target_dir, idx, total):
        nonlocal downloaded, errors, skipped
        session = _get_worker_session()
        # Если url похож на заглушку — попробуем резолвить через web
        # Если url похож на заглушку — попробуем резолвить через web
        u = (audio.get("url") or "")
        if u and ("audio_api_unavailable" in u or "vk.com/audio" in u):
            resolved = resolve_audio_url_web(session, audio)
            if resolved:
                audio["url"] = resolved
        filepath, error = download_audio(audio, target_dir, session)

        status = "ok"
        if error:
            if "Уже существует" in error:
                status = "skipped"
            else:
                status = "error"
        with lock:
            if status == "ok":
                downloaded += 1
            elif status == "skipped":
                skipped += 1
            else:
                errors += 1
        return status, filepath, error, idx, total, audio

    def _print_result(res):
        status, filepath, error, idx, total, audio = res
        print_progress(idx, total, f"[{idx}/{total}]")
        if status == "skipped":
            print(f"\x1b[2K  [SKIP] {audio.get('artist')} - {audio.get('title')}: {error}")
            return
        if status == "error":
            print(f"\x1b[2K  [ОШИБКА] {audio.get('artist')} - {audio.get('title')}: {error}")
            return
        try:
            size_kb = filepath.stat().st_size / 1024 if filepath else 0
            name = filepath.name if filepath else build_track_filename(audio)
            print(f"\x1b[2K  [OK] {name} ({size_kb:.1f} KB)")
        except Exception:
            name = filepath.name if filepath else build_track_filename(audio)
            print(f"\x1b[2K  [OK] {name}")

    if mode == "1":
        for pl in playlist_jobs:
            pl_title = pl.get("title") or "playlist"
            safe_pl = sanitize_filename(pl_title, max_len=120)
            pl_dir = output_dir / "playlists" / safe_pl
            pl_dir.mkdir(parents=True, exist_ok=True)
            print()
            print(f"[LOG] Плейлист: {pl_title} (id={pl.get('id')})")
            tracks = list(downloader.iter_playlist_tracks(int(pl.get("id"))))
            if not tracks:
                print("[LOG] Нет треков или недоступно.")
                continue
            print(f"[LOG] Треков в плейлисте: {len(tracks)}")
            if jobs == 1:
                for i, audio in enumerate(tracks, 1):
                    _print_result(download_one(audio, pl_dir, i, len(tracks)))
                    time.sleep(0.2)
            else:
                print(f"[LOG] Параллельная загрузка: jobs={jobs}")
                with _futures.ThreadPoolExecutor(max_workers=jobs) as ex:
                    futs = []
                    total = len(tracks)
                    for i, audio in enumerate(tracks, 1):
                        futs.append(ex.submit(download_one, audio, pl_dir, i, total))
                        time.sleep(0.05)  # мягко разгружаем VK/CDN
                    for fut in _futures.as_completed(futs):
                        try:
                            _print_result(fut.result())
                        except Exception as e:
                            with lock:
                                errors += 1
                            print(f"\x1b[2K  [ОШИБКА] worker exception: {e}")
    else:
        if jobs == 1:
            for i, audio in enumerate(audios, 1):
                _print_result(download_one(audio, output_dir, i, len(audios)))
                time.sleep(0.2)
        else:
            print(f"[LOG] Параллельная загрузка: jobs={jobs}")
            with _futures.ThreadPoolExecutor(max_workers=jobs) as ex:
                futs = []
                total = len(audios)
                for i, audio in enumerate(audios, 1):
                    futs.append(ex.submit(download_one, audio, output_dir, i, total))
                    time.sleep(0.05)
                for fut in _futures.as_completed(futs):
                    try:
                        _print_result(fut.result())
                    except Exception as e:
                        with lock:
                            errors += 1
                        print(f"\x1b[2K  [ОШИБКА] worker exception: {e}")

    print()
    print("=" * 60)
    print(f"Завершено!")
    print(f"Успешно скачано: {downloaded}")
    print(f"Уже существовало: {skipped}")
    print(f"Ошибок: {errors}")
    print(f"Сохранено в: {output_dir}")
    print("=" * 60)


if __name__ == "__main__":
    main()
