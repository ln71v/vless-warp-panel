#!/usr/bin/env python3
"""vless-warp-panel: Telegram-бот поверх vpn.py — ключи VLESS, пауза, WARP, онлайн и трафик. Только стандартная библиотека.
https://github.com/ln71v/vless-warp-panel  ·  MIT  ·  (c) 2026 Vaska_de_Gamma
"""
import json, os, sys, time, traceback, urllib.error, urllib.request, uuid

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import vpn  # noqa: E402

TOKEN = os.environ["BOT_TOKEN"]
ADMIN = int(os.environ.get("ADMIN_ID", "0") or 0)
API = f"https://api.telegram.org/bot{TOKEN}"

B_USERS, B_ADD, B_WARP, B_STATUS = "👥 Пользователи", "➕ Добавить", "🌐 WARP", "📊 Статус"
KB = {"keyboard": [[{"text": B_USERS}, {"text": B_ADD}], [{"text": B_WARP}, {"text": B_STATUS}]],
      "resize_keyboard": True, "is_persistent": True}
waiting_name = set()


# ---------- Telegram ----------
def call(method, **params):
    data = json.dumps(params).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=70) as r:
        return json.load(r)


def send(chat, text, markup=None):
    p = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if markup:
        p["reply_markup"] = markup
    return call("sendMessage", **p)


def edit(chat, msg_id, text, markup=None):
    p = {"chat_id": chat, "message_id": msg_id, "text": text, "parse_mode": "HTML"}
    if markup:
        p["reply_markup"] = markup
    try:
        call("editMessageText", **p)
    except urllib.error.HTTPError:
        send(chat, text, markup)


def send_photo(chat, path, caption):
    b = uuid.uuid4().hex
    parts = []
    for k, v in (("chat_id", str(chat)), ("caption", caption), ("parse_mode", "HTML")):
        parts.append(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    with open(path, "rb") as f:
        img = f.read()
    parts.append(f"--{b}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"qr.png\"\r\n"
                 f"Content-Type: image/png\r\n\r\n".encode() + img + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    req = urllib.request.Request(f"{API}/sendPhoto", data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    urllib.request.urlopen(req, timeout=60).read()


def esc(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def btn(text, data):
    return {"text": text, "callback_data": data[:64]}


# ---------- экраны ----------
def users_screen():
    users = vpn.list_users(stats=True)
    icon = lambda u: "⏸" if u["off"] else ("🟢" if u["ips"] else "⚪")
    rows = [[btn(f"{icon(u)} {u['name']}{'  🌐' if u['warp'] else ''}", f"u:{u['name']}")] for u in users]
    online = sum(1 for u in users if u["ips"])
    body = "\n".join(esc(vpn.user_line(u)) for u in users)
    return (f"👥 Пользователи: {len(users)}, онлайн: {online}\n\n{body}\n\n"
            f"🟢 — подключён сейчас, ⏸ — отключён, 🌐 — через WARP"), {"inline_keyboard": rows}


def user_screen(name):
    u = next((u for u in vpn.list_users(stats=True) if u["name"] == name), None)
    if not u:
        return "Такого пользователя уже нет.", None
    rows = [[btn("🔑 Ключ", f"k:{name}")]]
    if vpn.warp_installed():
        rows.append([btn("🌐 WARP: выключить" if u["warp"] else "🌐 WARP: включить", f"w{0 if u['warp'] else 1}:{name}")])
    rows.append([btn("▶️ Включить", f"on:{name}") if u["off"] else btn("⏸ Отключить", f"off:{name}")])
    rows.append([btn("⬅️ Назад", "list")])
    state = ("⏸ ОТКЛЮЧЁН — ключ не пускает\n" if u["off"] else "") + ("Выход: через WARP" if u["warp"] else "Выход: напрямую")
    ips = ", ".join(u["ips"]) if u["ips"] else "сейчас не подключён"
    return (f"👤 <b>{esc(name)}</b>\n{state}\n"
            f"Трафик: ↓{vpn.human(u['down'])} ↑{vpn.human(u['up'])}\nОнлайн: {esc(ips)}"), {"inline_keyboard": rows}


def warp_screen():
    if not vpn.warp_installed():
        return "🌐 WARP не установлен.", {"inline_keyboard": [[btn("Установить WARP", "wi")]]}
    meta = vpn.load_meta()
    users = vpn.list_users()
    mode = "всем" if meta["warp_all"] else (", ".join(u["name"] for u in users if u["warp"]) or "никому")
    ip, w = vpn._trace(f"socks5h://127.0.0.1:{vpn.TEST_PORT}")
    rows = [[btn("Всем включить", "wa1"), btn("Всем выключить", "wa0")],
            [btn("🔁 Перевыпустить ключ", "wr")]]
    return (f"🌐 WARP {'✅' if w in ('on', 'plus') else '❌'}  IP {esc(ip)}\n"
            f"Через WARP: {esc(mode)}\n\nКому включить по одному — в «Пользователи»."), {"inline_keyboard": rows}


def send_key(chat, name):
    l = vpn.link(name)
    path = vpn.qr_png(l)
    try:
        send_photo(chat, path, f"🔑 <b>{esc(name)}</b>\n\n<code>{esc(l)}</code>\n\nНажми на ссылку — скопируется.")
    finally:
        os.remove(path)


# ---------- обработка ----------
def on_message(m):
    chat, text = m["chat"]["id"], (m.get("text") or "").strip()
    uid = m["from"]["id"]
    if text == "/id":
        if not ADMIN or uid == ADMIN:
            send(chat, f"Твой ID: <code>{uid}</code>")
        return
    if uid != ADMIN:
        return
    if chat in waiting_name and text not in (B_USERS, B_ADD, B_WARP, B_STATUS):
        waiting_name.discard(chat)
        name = vpn.add_user(text)
        send(chat, f"✅ Добавлен {esc(name)}", KB)
        send_key(chat, name)
        return
    waiting_name.discard(chat)
    if text in ("/start", "/menu"):
        send(chat, "Готова. Кнопки внизу.", KB)
    elif text == B_USERS:
        t, mk = users_screen()
        send(chat, t, mk)
    elif text == B_ADD:
        waiting_name.add(chat)
        send(chat, "Имя нового пользователя (латиница, цифры, _ - .):")
    elif text == B_WARP:
        send(chat, "⏳ Проверяю WARP…")
        t, mk = warp_screen()
        send(chat, t, mk)
    elif text == B_STATUS:
        send(chat, "⏳ Собираю статус…")
        send(chat, esc(vpn.status_text()), KB)


def on_callback(q):
    if q["from"]["id"] != ADMIN:
        return
    call("answerCallbackQuery", callback_query_id=q["id"])
    chat, mid, d = q["message"]["chat"]["id"], q["message"]["message_id"], q.get("data", "")
    kind, _, name = d.partition(":")
    if d == "list":
        edit(chat, mid, *users_screen())
    elif kind == "u":
        edit(chat, mid, *user_screen(name))
    elif kind == "k":
        send_key(chat, name)
    elif kind in ("w0", "w1"):
        edit(chat, mid, "⏳ Применяю, связь моргнёт на секунду…")
        vpn.warp_set_user(name, kind == "w1")
        edit(chat, mid, *user_screen(name))
    elif kind in ("on", "off"):
        edit(chat, mid, "⏳ Применяю, связь моргнёт на секунду…")
        vpn.set_enabled(name, kind == "on")
        edit(chat, mid, *user_screen(name))
    elif d in ("wa0", "wa1"):
        edit(chat, mid, "⏳ Применяю…")
        vpn.warp_set_all(d == "wa1")
        edit(chat, mid, *warp_screen())
    elif d in ("wi", "wr"):
        edit(chat, mid, "⏳ Получаю ключ у Cloudflare, до минуты…")
        vpn.warp_install(reissue=(d == "wr"))
        edit(chat, mid, *warp_screen())


def main():
    offset = 0
    while True:
        try:
            r = call("getUpdates", offset=offset, timeout=50, allowed_updates=["message", "callback_query"])
        except Exception:
            time.sleep(5)
            continue
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            chat = (u.get("message") or u.get("callback_query", {}).get("message") or {}).get("chat", {}).get("id")
            try:
                if "message" in u:
                    on_message(u["message"])
                elif "callback_query" in u:
                    on_callback(u["callback_query"])
            except vpn.VpnError as e:
                if chat:
                    send(chat, f"✘ {esc(e)}")
            except Exception as e:
                traceback.print_exc()
                if chat:
                    send(chat, f"✘ Ошибка: {esc(type(e).__name__)}: {esc(e)}")


if __name__ == "__main__":
    main()
