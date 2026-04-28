#!/usr/bin/env python3
"""
Получение "аудио-токена" (Kate Mobile) через vkaudiotoken.

Важно:
- Этот токен работает с приватными audio.* методами VK при условии, что вы используете соответствующий User-Agent.
- Лучше использовать отдельный "пароль приложения" из настроек VK, чтобы не упираться в 2FA/лимиты по паролю.
"""

import json
import os
from pathlib import Path

from vkaudiotoken import get_kate_token


def main():
    login = os.environ.get("VK_LOGIN", "").strip() or input("VK login (phone/email): ").strip()
    password = os.environ.get("VK_PASSWORD", "").strip() or input("VK password (or app password): ").strip()

    token, user_agent = get_kate_token(login, password)
    out = Path("kate_token.json")
    out.write_text(
        json.dumps(
            {"access_token": token, "user_agent": user_agent},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Saved to {out}")
    print("Use:")
    print("  export VK_AUDIO_TOKEN=$(jq -r .access_token kate_token.json)")
    print("  export VK_AUDIO_UA=$(jq -r .user_agent kate_token.json)")


if __name__ == "__main__":
    main()

