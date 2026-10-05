import os
import re

import requests

# Son gönderimin teşhis bilgisi (gizli bilgi içermez); panelde status.json olarak yayınlanır
STATUS = {}


def send(text: str) -> bool:
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip().strip('"').strip("'")
    raw_len = len(token)
    m = re.search(r"\d{6,12}:[A-Za-z0-9_-]{30,}", token)   # URL ya da metin içine yapıştırılmışsa token'ı ayıkla
    if m:
        token = m.group(0)
    chat = (os.getenv("TELEGRAM_CHAT_ID") or "").strip().strip('"').strip("'")
    STATUS.update({"tokenShape": {"length": raw_len, "hasColon": ":" in token,
                                  "digitsBeforeColon": len(token.split(":")[0]) if ":" in token else None},
                   "tokenSet": bool(token), "tokenFormatOk": bool(re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{30,}", token)), "chatIdSet": bool(chat),
                   "chatIdLooksNumeric": bool(chat) and chat.strip().lstrip("-").isdigit()})
    if not token or not chat:
        STATUS["result"] = "secret eksik"
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
            try:
                STATUS["error"] = f"{r.status_code} {r.json().get('description', '')}"
            except ValueError:
                STATUS["error"] = str(r.status_code)
            ok = False
    STATUS["result"] = "gönderildi" if ok else "hata"
    if ok:
        print(f"Telegram: {len(chunks)} mesaj gönderildi.")
    return ok
