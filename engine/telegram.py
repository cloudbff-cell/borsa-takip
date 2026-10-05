import os

import requests


def send(text: str) -> bool:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("---- TELEGRAM (token yok, sadece yazdırılıyor) ----\n" + text)
        return False
    ok = True
    # Telegram mesaj sınırı 4096 karakter; satır bazında böl
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3900:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    for ch in chunks:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": ch, "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=20)
        if not r.ok:
            print("Telegram hatası:", r.status_code, r.text[:300])
            ok = False
    if ok:
        print(f"Telegram: {len(chunks)} mesaj gönderildi.")
    return ok
