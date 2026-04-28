# VK Audio Downloader

Скрипт для скачивания аудио из VK групп/пабликов по плейлистам/альбомам.

## Установка

```bash
make install
```

## Запуск

```bash
make run
```

### Быстрый старт (рекомендуется)

1) Запусти Vivaldi/Chrome с DevTools протоколом (CDP):

```bash
vivaldi-stable --remote-debugging-port=9222 --remote-allow-origins=*
```

2) Залогинься на `vk.com` в этом браузере.

3) Сохрани cookies в `cookies.json` одной командой:

```bash
./venv/bin/python cdp_dump_vk_cookies.py --base http://127.0.0.1:9222 --out cookies.json
```

4) Запусти загрузку:

```bash
./venv/bin/python vk_audio_downloader.py --group settlersspb --what audio
```

По умолчанию сохраняет в `./out/<group>/audio/`, а плейлисты — в `./out/<group>/audio/playlists/<playlist>/`.

## Методы авторизации

Оставлен один рабочий способ: **cookies залогиненной сессии**.

- **Почему так**: OAuth токены и “клиентские” токены часто блокируются/режутся для `audio.*`, а web‑плеер работает через HLS (`.m3u8`), который мы скачиваем и собираем.

### Получение cookies (без ручной копипасты)
Самый надёжный вариант — через CDP (см. “Быстрый старт” выше).

## CLI опции

Посмотреть помощь:

```bash
./venv/bin/python vk_audio_downloader.py --help
```

Основные параметры:
- `--group`: группа (screen name или id)
- `--what`: что скачивать (`audio`, `photos`, `videos`, `clips`, `all`)
- `--out`: куда сохранять (по умолчанию `./out/<group>`)
- `--cookies-path`: путь к cookies (`cookies.json`)
- `--debug`: подробные логи (или `VK_DEBUG=1`)

## Про формат аудио
VK отдаёт реальные треки как **HLS** (`index.m3u8` + `seg-*.ts` + `AES-128 key.pub`), поэтому скрипт:
- декодирует `audio_api_unavailable` в `index.m3u8`
- скачивает сегменты и собирает итоговый mp3

