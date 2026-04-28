# VK Audio Downloader

Скрипт для скачивания аудио с VK групп/пабликов.

## Установка

```bash
make install
```

## Запуск

```bash
make run
```

После старта скрипт попросит:
- cookies JSON (экспорт из DevTools)
- ID группы (например `settlersspb` или `-179835916`)
- что скачивать: **плейлисты/альбомы** или **все треки**

## Методы авторизации

Сейчас оставлен один простой и рабочий способ: **cookies из браузера** (экспорт из DevTools).

Альтернатива (часто стабильнее для audio): получить **Kate Mobile audio token** через `vkaudiotoken`:

```bash
make get-kate
```

Сохранит `kate_token.json` (token + обязательный User-Agent).

### Как получить cookies (Chrome/Vivaldi/Chromium)
1. Открой `vk.com` и залогинься
2. F12 → Application (или Storage) → Cookies → `https://vk.com`
3. ПКМ по таблице cookies → Copy → Copy as JSON
4. Сохрани в файл `cookies.json` в папке проекта

### Как получить токен доступа (рекомендуется)

1. Перейдите на: https://oauth.vk.com/authorize?client_id=1&scope=audio&redirect_uri=https://oauth.vk.com/blank.html&display=mobile&response_type=token
2. Нажмите **"Разрешить"**
3. В адресной строке браузера увидите URL вида:
   ```
   https://oauth.vk.com/blank.html#access_token=ABCDEF123456...
   ```
4. Скопируйте токен (всё после `access_token=`)
5. Вставьте его при запуске скрипта (метод 3)

**Важно:**cookies из браузера (Vivaldi) - это session cookies, которые не подходят для VK API. Нужен отдельный access token с правом `audio`.

## Cookies файл

Если используете метод 2, создайте файл `cookies.json` в папке проекта:

```json
[
  {"name": "remixstlid", "value": "YOUR_VALUE"},
  {"name": "remixlgck", "value": "YOUR_VALUE"}
]
```

## Управление группой

https://vk.com/audios-179835916?section=playlists

Где `-179835916` - ID группы.
