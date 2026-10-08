"""Telegram-бот PushkaVPN: заглушка для /start + подтверждение входа на сайт.

Ссылка вида https://t.me/<bot>?start=auth_<token> приходит с сайта —
бот подтверждает вход и сайт автоматически создаёт/открывает аккаунт.
"""

import os
import time

import requests

TOKEN = os.environ.get("BOT_TOKEN", "8867577154:AAHxbdS7xa1-NV2IqQcFf_Ls3oPP5OKXpSA")
SITE_URL = os.environ.get("SITE_URL", "https://your-site.railway.app").rstrip("/")
LINK_SECRET = os.environ.get("LINK_SECRET", os.environ.get("VPS_SECRET", "pushka_secret_2026"))

API = f"https://api.telegram.org/bot{TOKEN}"
START_TEXT = (
    "Привет! Наш проект ещё в разработке. "
    "Сайт с личным кабинетом скоро заработает, "
    f"следите за обновлениями: {SITE_URL}"
)


def confirm_auth(token: str, chat_id: int, username: str) -> bool:
    """Отправляет подтверждение входа на сайт."""
    try:
        resp = requests.post(
            f"{SITE_URL}/auth/telegram/confirm",
            json={"token": token, "telegram_id": str(chat_id), "username": username},
            headers={"X-Secret": LINK_SECRET},
            timeout=15,
        )
        return bool(resp.ok and resp.json().get("success"))
    except Exception as exc:  # noqa: BLE001
        print("confirm_auth error:", exc)
        return False


def send_text(chat_id: int, text: str) -> None:
    try:
        requests.post(
            f"{API}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        print("sendMessage error:", exc)


def handle_update(update: dict) -> None:
    message = update.get("message") or {}
    text = (message.get("text") or "").strip()
    chat_id = message.get("chat", {}).get("id")
    if not chat_id or not text.startswith("/start"):
        return

    parts = text.split(maxsplit=1)
    payload = parts[1] if len(parts) > 1 else ""

    if payload.startswith("auth_"):
        # Вход/регистрация через ссылку с сайта
        username = (message.get("from") or {}).get("username") or ""
        if confirm_auth(payload[5:], chat_id, username):
            send_text(chat_id, f"✅ Готово! Аккаунт открыт — вернитесь на сайт: {SITE_URL}/dashboard")
        else:
            send_text(chat_id, "❌ Не удалось подтвердить вход. Вернитесь на сайт и попробуйте ещё раз.")
        return

    send_text(chat_id, START_TEXT)


def main() -> None:
    offset = None
    print("Bot started")
    while True:
        try:
            params = {"timeout": 30, "offset": offset, "allowed_updates": ["message"]}
            resp = requests.get(f"{API}/getUpdates", params=params, timeout=40)
            data = resp.json()
            if not data.get("ok"):
                print("Telegram API error:", data)
                time.sleep(5)
                continue
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                handle_update(update)
        except Exception as exc:  # noqa: BLE001
            print("Polling error:", exc)
            time.sleep(5)


if __name__ == "__main__":
    main()
