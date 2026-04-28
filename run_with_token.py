#!/usr/bin/env python3
"""
Скрипт для автоматической загрузки токена и запуска скачивания.
"""

import os
import sys
import subprocess

# Токен для авторизации
ACCESS_TOKEN = os.environ.get("VK_ACCESS_TOKEN", "")

# Твой токен
if not ACCESS_TOKEN:
    print("Не задан VK_ACCESS_TOKEN. Пример:")
    print("  VK_ACCESS_TOKEN='...' python3 run_with_token.py")
    sys.exit(2)

# Запуск скрипта
cmd = [sys.executable, "vk_audio_downloader.py"]

print("Запуск vk_audio_downloader.py с токеном...")
print()

# Запустим и передадим токен черезstdin
from subprocess import Popen, PIPE, DEVNULL

p = Popen(cmd, stdin=PIPE, stdout=sys.stdout, stderr=sys.stderr)
p.communicate(input=b"3\n" + ACCESS_TOKEN.encode() + b"\n")
