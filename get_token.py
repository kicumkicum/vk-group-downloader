#!/usr/bin/env python3
"""
Скрипт для автоматического получения токена доступа VK API через OAuth.

Просто откройте ссылку в браузере и скопируйте токен.
"""

import sys

# Стандартный client_id для VK (standalone приложение)
CLIENT_ID = "1"

# Ссылка для получения токена
AUTH_URL = f"https://oauth.vk.com/authorize?client_id={CLIENT_ID}&scope=audio&redirect_uri=https://oauth.vk.com/blank.html&display=mobile&response_type=token"

print("=" * 60)
print("VK Tokengetter")
print("=" * 60)
print()
print("Следуйте инструкции:")
print()
print("1. Откройте в браузере:")
print(f"   {AUTH_URL}")
print()
print("2. Если открывается в приложении VK:")
print("   - Введите логин/пароль от https://vk.com")
print("   - Нажмите 'Разрешить'")
print()
print("3. В адресной строке увидите:")
print("   https://oauth.vk.com/blank.html#access_token=ABCDEF123456...")
print()
print("4. Скопируйте токен (всё после 'access_token=')")
print()
print("5. Используйте этот токен в vk_audio_downloader.py")
print()
print("=" * 60)
print()
print("Альтернативно - откройте в браузере:")
print("https://vk.com/apps?act=manage")
print("Создайте standalone-приложение и используйте его client_id")
print()
