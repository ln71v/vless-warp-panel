#!/usr/bin/env python3
"""vless-warp-panel — VLESS Reality со своим доменом + WARP по пользователям + Telegram-бот.
https://github.com/ln71v/vless-warp-panel  ·  MIT  ·  (c) 2026 Vaska_de_Gamma

Запуск:  vpn            — меню в терминале
         vpn status     — статус одной командой
         vpn update     — обновить панель из GitHub
         vpn flush      — сохранить счётчики трафика (вызывается cron'ом)
Бот импортирует этот файл и пользуется теми же функциями.
"""
import fcntl, json, os, platform, re, shutil, subprocess, sys, tempfile, time, urllib.parse, urllib.request, uuid

CFG = "/usr/local/etc/xray/config.json"
META = "/usr/local/etc/xray/vpn-meta.json"
WARP_DIR = "/usr/local/etc/xray/warp"
WGCF = "/usr/local/bin/wgcf"
LOCK = "/run/lock/vpn-xray.lock"
BOT_ENV = "/etc/vpn-bot.env"
BOT_UNIT = "/etc/systemd/system/vpn-bot.service"
HERE = os.path.dirname(os.path.realpath(__file__))

TRAFFIC = "/usr/local/etc/xray/vpn-traffic.json"
CRON = "/etc/cron.d/vpn-traffic"
API_ADDR = "127.0.0.1:10085"

MAIN_TAG = "vless-in"
WARP_TAG = "warp"
TEST_TAG = "warp-test"
TEST_PORT = 10808


class VpnError(Exception):
    pass


# ---------- служебное ----------
def sh(cmd, check=False, timeout=60):
    r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise VpnError((r.stderr or r.stdout).strip()[-400:])
    return r


def load_cfg():
    with open(CFG) as f:
        return json.load(f)


def load_meta():
    m = {"name": "VPN", "host": "", "warp_users": [], "warp_all": False, "disabled": {}}
    if os.path.exists(META):
        with open(META) as f:
            m.update(json.load(f))
    return m


def save_meta(m):
    tmp = META + ".tmp"
    with open(tmp, "w") as f:
        json.dump(m, f, indent=2, ensure_ascii=False)
    os.replace(tmp, META)


def main_inbound(cfg):
    for ib in cfg["inbounds"]:
        if ib.get("protocol") == "vless":
            ib.setdefault("tag", MAIN_TAG)
            return ib
    raise VpnError("в конфиге Xray нет VLESS-входа")


def clients(cfg):
    return main_inbound(cfg)["settings"]["clients"]


def warp_outbound(cfg):
    return next((o for o in cfg.get("outbounds", []) if o.get("tag") == WARP_TAG), None)


def warp_installed(cfg=None):
    return warp_outbound(cfg or load_cfg()) is not None


def rebuild_routing(cfg, meta):
    """Пересобрать правила маршрутизации из списка пользователей WARP."""
    rules = []
    if warp_outbound(cfg):
        rules.append({"type": "field", "inboundTag": [TEST_TAG], "outboundTag": WARP_TAG})
        if meta["warp_all"]:
            rules.append({"type": "field", "inboundTag": [main_inbound(cfg)["tag"]], "outboundTag": WARP_TAG})
        else:
            names = {c.get("email") for c in clients(cfg)}
            meta["warp_users"] = [u for u in meta["warp_users"] if u in names or u in meta["disabled"]]
            users = [u for u in meta["warp_users"] if u in names]
            if users:
                rules.append({"type": "field", "user": users, "outboundTag": WARP_TAG})
    cfg["routing"] = {"domainStrategy": "AsIs", "rules": rules}


def ensure_stats(cfg):
    """Включить в Xray счётчики трафика и онлайна по пользователям."""
    cfg["api"] = {"tag": "api", "listen": API_ADDR, "services": ["StatsService"]}
    cfg["stats"] = {}
    lv = cfg.setdefault("policy", {}).setdefault("levels", {}).setdefault("0", {})
    lv.update({"statsUserUplink": True, "statsUserDownlink": True, "statsUserOnline": True})


def _api(*args):
    r = sh(["xray", "api", *args, f"--server={API_ADDR}"], timeout=10)
    try:
        return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else {}
    except ValueError:
        return {}


def _counters(reset=False):
    """{имя: [отдано, получено]} из счётчиков Xray с момента последнего сброса."""
    args = ["statsquery", "-pattern", "user>>>"] + (["-reset"] if reset else [])
    out = {}
    for s in _api(*args).get("stat", []):
        parts = s["name"].split(">>>")
        if len(parts) == 4 and parts[2] == "traffic":
            out.setdefault(parts[1], [0, 0])[0 if parts[3] == "uplink" else 1] += int(s.get("value", 0))
    return out


def flush_traffic():
    """Перенести счётчики Xray в файл и обнулить их — чтобы трафик не терялся при перезапуске."""
    cur = _counters(reset=True)
    if not cur:
        return
    tot = {}
    if os.path.exists(TRAFFIC):
        with open(TRAFFIC) as f:
            tot = json.load(f)
    for u, (up, down) in cur.items():
        t = tot.setdefault(u, [0, 0])
        t[0] += up
        t[1] += down
    tmp = TRAFFIC + ".tmp"
    with open(tmp, "w") as f:
        json.dump(tot, f)
    os.replace(tmp, TRAFFIC)


def ensure_cron():
    if not os.path.exists(CRON):
        with open(CRON, "w") as f:
            f.write(f"*/5 * * * * root /usr/bin/python3 {os.path.realpath(__file__)} flush >/dev/null 2>&1\n")


def apply(cfg, meta=None):
    """Проверить конфиг, сохранить, перезапустить Xray; при сбое откатить."""
    if meta is not None:
        rebuild_routing(cfg, meta)
    ensure_stats(cfg)
    os.makedirs("/run/lock", exist_ok=True)
    with open(LOCK, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CFG), suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.chmod(tmp, 0o644)
        r = sh(["xray", "run", "-test", "-config", tmp])
        if r.returncode != 0:
            os.remove(tmp)
            raise VpnError("конфиг не прошёл проверку, ничего не изменила:\n" + (r.stdout + r.stderr).strip()[-300:])
        try:
            flush_traffic()
        except Exception:
            pass
        shutil.copy2(CFG, CFG + ".bak")
        os.replace(tmp, CFG)
        sh(["systemctl", "restart", "xray"])
        time.sleep(1.5)
        if sh(["systemctl", "is-active", "xray"]).stdout.strip() != "active":
            shutil.copy2(CFG + ".bak", CFG)
            sh(["systemctl", "restart", "xray"])
            raise VpnError("Xray не запустился с новым конфигом, откатила назад")
        if meta is not None:
            save_meta(meta)


def pubkey(cfg):
    priv = main_inbound(cfg)["streamSettings"]["realitySettings"]["privateKey"]
    out = sh(["xray", "x25519", "-i", priv], check=True).stdout
    for line in out.splitlines():
        if "Password" in line or "Public" in line:
            return line.split()[-1]
    raise VpnError("не смогла вычислить публичный ключ")


def valid_name(name):
    name = re.sub(r"\s+", "_", name.strip())[:32]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise VpnError("имя: только латиница, цифры, точка, дефис, подчёркивание")
    return name


# ---------- пользователи ----------
def list_users(stats=False):
    cfg, meta = load_cfg(), load_meta()
    warp = warp_installed(cfg)
    users = [{"name": c.get("email", "?"), "off": False,
              "warp": warp and (meta["warp_all"] or c.get("email") in meta["warp_users"])}
             for c in clients(cfg)]
    users += [{"name": n, "off": True, "warp": warp and (meta["warp_all"] or n in meta["warp_users"])}
              for n in meta["disabled"]]
    if stats:
        tot = {}
        if os.path.exists(TRAFFIC):
            with open(TRAFFIC) as f:
                tot = json.load(f)
        cur = _counters()
        for u in users:
            a, b = tot.get(u["name"], [0, 0]), cur.get(u["name"], [0, 0])
            u["up"], u["down"] = a[0] + b[0], a[1] + b[1]
            u["ips"] = sorted(_api("statsonlineiplist", "-email", u["name"]).get("ips", {}))
    return users


def human(n):
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def user_line(u):
    """Одна строка о пользователе: онлайн, WARP, трафик."""
    on = "⏸ отключён" if u.get("off") else (f"🟢 {len(u['ips'])} IP" if u.get("ips") else "⚪ офлайн")
    return (f"{u['name']}{'  🌐' if u['warp'] else ''} — {on}, "
            f"↓{human(u.get('down', 0))} ↑{human(u.get('up', 0))}")


def add_user(name):
    name = valid_name(name)
    cfg, meta = load_cfg(), load_meta()
    taken = [c.get("email", "") for c in clients(cfg)] + list(meta["disabled"])
    if any(t.lower() == name.lower() for t in taken):
        raise VpnError(f"пользователь {name} уже есть")
    clients(cfg).append({"id": str(uuid.uuid4()), "flow": "xtls-rprx-vision", "email": name})
    apply(cfg, meta)
    return name


def del_user(name):
    cfg, meta = load_cfg(), load_meta()
    if meta["disabled"].pop(name, None) is not None:
        apply(cfg, meta)
        return
    cl = clients(cfg)
    new = [c for c in cl if c.get("email") != name]
    if len(new) == len(cl):
        raise VpnError(f"нет пользователя {name}")
    if not new:
        raise VpnError("нельзя удалить последнего пользователя")
    main_inbound(cfg)["settings"]["clients"] = new
    apply(cfg, meta)


def set_enabled(name, on):
    """Пауза ключа: отключённый убирается из Xray, но хранится и возвращается с тем же ключом."""
    cfg, meta = load_cfg(), load_meta()
    cl = clients(cfg)
    if on:
        c = meta["disabled"].pop(name, None)
        if c is None:
            raise VpnError(f"{name} и так включён" if any(x.get("email") == name for x in cl) else f"нет пользователя {name}")
        cl.append(c)
    else:
        c = next((x for x in cl if x.get("email") == name), None)
        if c is None:
            raise VpnError(f"{name} и так отключён" if name in meta["disabled"] else f"нет пользователя {name}")
        if len(cl) == 1:
            raise VpnError("нельзя отключить последнего активного пользователя")
        meta["disabled"][name] = c
        main_inbound(cfg)["settings"]["clients"] = [x for x in cl if x is not c]
    apply(cfg, meta)


def link(name):
    cfg, meta = load_cfg(), load_meta()
    c = next((c for c in clients(cfg) if c.get("email") == name), None) or meta["disabled"].get(name)
    if not c:
        raise VpnError(f"нет пользователя {name}")
    ib = main_inbound(cfg)
    rs = ib["streamSettings"]["realitySettings"]
    host = meta["host"] or rs["serverNames"][0]
    q = {"encryption": "none", "flow": c.get("flow", "xtls-rprx-vision"), "security": "reality",
         "sni": rs["serverNames"][0], "fp": "chrome", "pbk": pubkey(cfg), "sid": rs["shortIds"][0], "type": "tcp"}
    return (f"vless://{c['id']}@{host}:{ib['port']}?{urllib.parse.urlencode(q)}#"
            + urllib.parse.quote(f"{meta['name']}-{name}"))


def qr_png(text):
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    sh(["qrencode", "-s", "8", "-m", "2", "-o", path, text], check=True)
    return path


# ---------- WARP ----------
def _wgcf_download():
    arch = {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "armv7"}.get(platform.machine(), "amd64")
    req = urllib.request.Request("https://api.github.com/repos/ViRb3/wgcf/releases/latest",
                                 headers={"User-Agent": "vpn-menu"})
    rel = json.load(urllib.request.urlopen(req, timeout=30))
    url = next(a["browser_download_url"] for a in rel["assets"] if a["name"].endswith(f"linux_{arch}"))
    urllib.request.urlretrieve(url, WGCF)
    os.chmod(WGCF, 0o755)


def _wgcf_profile(fresh):
    if not os.path.exists(WGCF):
        _wgcf_download()
    if fresh and os.path.isdir(WARP_DIR):
        shutil.rmtree(WARP_DIR)
    os.makedirs(WARP_DIR, exist_ok=True)
    if not os.path.exists(f"{WARP_DIR}/wgcf-account.toml"):
        subprocess.run([WGCF, "register", "--accept-tos"], cwd=WARP_DIR, capture_output=True, text=True, timeout=60, check=True)
    subprocess.run([WGCF, "generate"], cwd=WARP_DIR, capture_output=True, text=True, timeout=60, check=True)
    prof = open(f"{WARP_DIR}/wgcf-profile.conf").read()
    get = lambda k: re.findall(rf"^\s*{k}\s*=\s*(.+)$", prof, re.M)
    addrs = [a.strip() for line in get("Address") for a in line.split(",")]
    return {"secretKey": get("PrivateKey")[0].strip(), "address": addrs,
            "peer": get("PublicKey")[0].strip(), "endpoint": get("Endpoint")[0].strip()}


def warp_install(reissue=False):
    """Поставить WARP или перевыпустить ключ. Пользователи WARP сохраняются."""
    p = _wgcf_profile(fresh=reissue)
    cfg, meta = load_cfg(), load_meta()
    ob = {"tag": WARP_TAG, "protocol": "wireguard",
          "settings": {"secretKey": p["secretKey"], "address": p["address"], "mtu": 1280,
                       "noKernelTun": True, "domainStrategy": "ForceIPv4",
                       "peers": [{"publicKey": p["peer"], "endpoint": p["endpoint"], "keepAlive": 25}]}}
    cfg["outbounds"] = [o for o in cfg["outbounds"] if o.get("tag") != WARP_TAG]
    cfg["outbounds"].append(ob)
    cfg["inbounds"] = [i for i in cfg["inbounds"] if i.get("tag") != TEST_TAG]
    cfg["inbounds"].append({"tag": TEST_TAG, "listen": "127.0.0.1", "port": TEST_PORT,
                            "protocol": "socks", "settings": {"udp": False}})
    apply(cfg, meta)


def warp_remove():
    cfg, meta = load_cfg(), load_meta()
    cfg["outbounds"] = [o for o in cfg["outbounds"] if o.get("tag") != WARP_TAG]
    cfg["inbounds"] = [i for i in cfg["inbounds"] if i.get("tag") != TEST_TAG]
    apply(cfg, meta)


def warp_set_user(name, on):
    cfg, meta = load_cfg(), load_meta()
    if not warp_outbound(cfg):
        raise VpnError("WARP не установлен")
    if not any(c.get("email") == name for c in clients(cfg)) and name not in meta["disabled"]:
        raise VpnError(f"нет пользователя {name}")
    users = [u for u in meta["warp_users"] if u != name]
    if on:
        users.append(name)
    meta["warp_users"] = users
    apply(cfg, meta)


def warp_set_all(on):
    cfg, meta = load_cfg(), load_meta()
    if not warp_outbound(cfg):
        raise VpnError("WARP не установлен")
    meta["warp_all"] = bool(on)
    if not on:
        meta["warp_users"] = []
    apply(cfg, meta)


def _trace(proxy=None):
    cmd = ["curl", "-s4", "--max-time", "10", "https://www.cloudflare.com/cdn-cgi/trace"]
    if proxy:
        cmd[1:1] = ["-x", proxy]
    out = sh(cmd, timeout=15).stdout
    d = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    return d.get("ip", "нет связи"), d.get("warp", "?")


# ---------- статус ----------
def status_text():
    cfg, meta = load_cfg(), load_meta()
    act = lambda s: "✅" if sh(["systemctl", "is-active", s]).stdout.strip() == "active" else "❌"
    lines = [f"🖥 {meta['name']} — {meta['host'] or '?'}",
             f"Xray {act('xray')}   nginx {act('nginx')}"]
    host = meta["host"]
    if host:
        end = sh(f"openssl x509 -enddate -noout -in /etc/letsencrypt/live/{host}/fullchain.pem 2>/dev/null").stdout
        if "=" in end:
            days = int((time.mktime(time.strptime(end.split("=", 1)[1].strip(), "%b %d %H:%M:%S %Y %Z")) - time.time()) // 86400)
            lines.append(f"Сертификат: ещё {days} дн.")
    users = list_users(stats=True)
    online = sum(1 for u in users if u["ips"])
    lines.append(f"Пользователей: {len(users)}, сейчас онлайн: {online}")
    ip, _ = _trace()
    lines.append(f"IP сервера: {ip}")
    if warp_installed(cfg):
        wip, w = _trace(f"socks5h://127.0.0.1:{TEST_PORT}")
        mode = "всем" if meta["warp_all"] else (", ".join(u["name"] for u in users if u["warp"]) or "никому")
        lines.append(f"WARP: {'✅' if w in ('on', 'plus') else '❌'} IP {wip}")
        lines.append(f"Через WARP: {mode}")
    else:
        lines.append("WARP: не установлен")
    return "\n".join(lines)


# ---------- бот ----------
def bot_install(token, admin_id):
    with open(BOT_ENV, "w") as f:
        f.write(f"BOT_TOKEN={token}\nADMIN_ID={admin_id}\n")
    os.chmod(BOT_ENV, 0o600)
    with open(BOT_UNIT, "w") as f:
        f.write(f"""[Unit]
Description=VPN Telegram bot
After=network-online.target xray.service
Wants=network-online.target

[Service]
EnvironmentFile={BOT_ENV}
ExecStart=/usr/bin/python3 {HERE}/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
""")
    sh("systemctl daemon-reload && systemctl enable --now vpn-bot && systemctl restart vpn-bot", check=True)


# ---------- обновление ----------
def update():
    """Подтянуть свежую версию из GitHub и перезапустить бота, если он стоит."""
    before = sh(["git", "-C", HERE, "rev-parse", "--short", "HEAD"]).stdout.strip()
    sh(["git", "-C", HERE, "pull", "-q", "--ff-only"], check=True)
    after = sh(["git", "-C", HERE, "rev-parse", "--short", "HEAD"]).stdout.strip()
    if before != after and os.path.exists(BOT_UNIT):
        sh(["systemctl", "restart", "vpn-bot"])
    return before, after


# ---------- меню ----------
G, R, Y, C, N = "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[0m"


def ok(t): print(f"{G}✔ {t}{N}")
def err(t): print(f"{R}✘ {t}{N}")
def pause(): input("\nEnter — назад в меню ")


def pick_user():
    users = list_users()
    for i, u in enumerate(users, 1):
        print(f"  {i}) {u['name']}{'  🌐 WARP' if u['warp'] else ''}")
    n = input("Номер: ").strip()
    if not n.isdigit() or not 1 <= int(n) <= len(users):
        raise VpnError("нет такого номера")
    return users[int(n) - 1]["name"]


def show_key(name):
    l = link(name)
    print()
    subprocess.run(["qrencode", "-t", "ansiutf8", "-m", "1", l])
    print(f"\n{l}\n")


def setup():
    meta = load_meta()
    print(f"{C}Первичная настройка{N}")
    meta["name"] = input(f"Название сервера в ключах [{meta['name']}]: ").strip() or meta["name"]
    sni = main_inbound(load_cfg())["streamSettings"]["realitySettings"]["serverNames"][0]
    meta["host"] = input(f"Домен для ключей [{meta['host'] or sni}]: ").strip() or meta["host"] or sni
    save_meta(meta)
    ok("сохранила")


def menu():
    if os.geteuid() != 0:
        sys.exit("Запускай от root")
    if not os.path.exists(META):
        setup()
    ensure_cron()
    if "stats" not in load_cfg():
        print("Включаю счётчики трафика…")
        apply(load_cfg(), load_meta())
    items = [
        ("Статус", lambda: print(status_text())),
        ("Пользователи: кто онлайн, трафик", lambda: [print(f"  • {user_line(u)}" + (f"\n      IP: {', '.join(u['ips'])}" if u['ips'] else "")) for u in list_users(stats=True)]),
        ("Показать ключ (QR + ссылка)", lambda: show_key(pick_user())),
        ("Добавить пользователя", lambda: show_key(add_user(input("Имя: ")))),
        ("Удалить пользователя", lambda: (lambda n: input(f"Удалить {n}? (да/нет): ").strip().lower() in ("да", "д", "y") and (del_user(n), ok(f"{n} удалён")))(pick_user())),
        ("Отключить пользователя (пауза)", lambda: (lambda n: (set_enabled(n, False), ok(f"{n} на паузе")))(pick_user())),
        ("Включить пользователя", lambda: (lambda n: (set_enabled(n, True), ok(f"{n} снова в деле")))(pick_user())),
        ("WARP: установить / перевыпустить ключ", lambda: (warp_install(reissue=warp_installed()), ok("WARP готов"), print(status_text()))),
        ("WARP: включить пользователю", lambda: (lambda n: (warp_set_user(n, True), ok(f"{n} → WARP")))(pick_user())),
        ("WARP: выключить пользователю", lambda: (lambda n: (warp_set_user(n, False), ok(f"{n} → напрямую")))(pick_user())),
        ("WARP: всем вкл / всем выкл", lambda: (lambda a: (warp_set_all(a == "1"), ok("готово")))(input("1 — всем через WARP, 0 — всем напрямую: ").strip())),
        ("WARP: удалить", lambda: (warp_remove(), ok("WARP убран, все напрямую"))),
        ("Бот: установить / перенастроить", lambda: (bot_install(input("Токен бота: ").strip(), input("Твой Telegram ID (0 — узнать через /id): ").strip() or "0"), ok("бот запущен"))),
        ("Бот: лог", lambda: print(sh("journalctl -u vpn-bot -n 30 --no-pager").stdout)),
        ("Название и домен в ключах", setup),
        ("Обновить панель из GitHub", lambda: (lambda r: ok("уже последняя версия" if r[0] == r[1] else f"обновила {r[0]} → {r[1]}"))(update())),
    ]
    while True:
        print(f"\n{C}══════ vless-warp-panel ══════{N}")
        for i, (t, _) in enumerate(items, 1):
            print(f" {i:2}) {t}")
        print("  0) Выход")
        ch = input("Выбор: ").strip()
        if ch == "0":
            return
        if not ch.isdigit() or not 1 <= int(ch) <= len(items):
            continue
        try:
            items[int(ch) - 1][1]()
        except VpnError as e:
            err(str(e))
        except subprocess.CalledProcessError as e:
            err((e.stderr or str(e)).strip()[-300:])
        except Exception as e:
            err(f"{type(e).__name__}: {e}")
        pause()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        print(status_text())
    elif len(sys.argv) > 1 and sys.argv[1] == "flush":
        flush_traffic()
    elif len(sys.argv) > 1 and sys.argv[1] == "update":
        b, a = update()
        print("уже последняя версия" if b == a else f"обновлено {b} → {a}")
    else:
        menu()
