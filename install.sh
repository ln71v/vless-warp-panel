#!/usr/bin/env bash
# vless-warp-panel — установщик.
# VLESS Reality «сам у себя» на своём домене + сайт-заглушка + WARP по пользователям + Telegram-бот.
# https://github.com/ln71v/vless-warp-panel · MIT · (c) 2026 Vaska_de_Gamma
#
# Запуск на сервере под root:
#   bash <(curl -Ls https://raw.githubusercontent.com/ln71v/vless-warp-panel/main/install.sh)
#
# Docker и всё, что в нём (например AmneziaWG), установщик не трогает.
set -euo pipefail

REPO="https://github.com/ln71v/vless-warp-panel"
DIR="/opt/vless-warp-panel"
CFG="/usr/local/etc/xray/config.json"
META="/usr/local/etc/xray/vpn-meta.json"
G='\033[32m'; R='\033[31m'; Y='\033[33m'; C='\033[36m'; N='\033[0m'

ok()   { echo -e "${G}✔ $*${N}"; }
warn() { echo -e "${Y}! $*${N}"; }
die()  { echo -e "${R}✘ $*${N}"; exit 1; }
step() { echo -e "\n${C}▶ $*${N}"; }
ask()  { local q="$1" def="${2:-}" a; read -rp "$q${def:+ [$def]}: " a </dev/tty; echo "${a:-$def}"; }
port_owner() { { ss -tlnpH "sport = :$1" 2>/dev/null | grep -o 'users:(("[^"]*' | head -1 | cut -d'"' -f2; } || true; }

[ "$EUID" -eq 0 ] || die "Запускай от root"
command -v apt-get >/dev/null || die "Нужна Ubuntu или Debian"

if [ -f "$META" ]; then
    warn "Панель уже установлена. Открываю меню (обновление — пункт «Обновить панель»)."
    exec /usr/local/bin/vpn
fi

echo -e "${C}══════ Установка vless-warp-panel ══════${N}"
echo "Понадобится домен, у которого A-запись уже указывает на этот сервер."
echo

DOMAIN=$(ask "Домен (например example.com)")
DOMAIN=${DOMAIN,,}
[[ "$DOMAIN" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}$ ]] || die "Это не похоже на домен: $DOMAIN"
NAME=$(ask "Название сервера — так подпишутся ключи в приложении" "VPN")
BRAND=$(ask "Название сайта-заглушки" "Lumen Studio")
FIRST=$(ask "Имя первого пользователя (латиница)" "me")
[[ "$FIRST" =~ ^[A-Za-z0-9_.-]+$ ]] || die "Имя: только латиница, цифры, _ . -"

step "Проверяю домен и порты"
IP=$(curl -s4 --max-time 10 https://api.ipify.org || curl -s4 --max-time 10 https://ifconfig.me || true)
[ -n "$IP" ] || die "Не смогла узнать внешний IP сервера"
getent ahostsv4 "$DOMAIN" | awk '{print $1}' | sort -u | grep -qx "$IP" \
    || die "$DOMAIN указывает не на этот сервер ($IP). Поправь A-запись (без прокси Cloudflare) и подожди пару минут."
ok "$DOMAIN → $IP"
WWW=""
getent ahostsv4 "www.$DOMAIN" 2>/dev/null | awk '{print $1}' | sort -u | grep -qx "$IP" && WWW="www.$DOMAIN"

o443=$(port_owner 443); [ -z "$o443" ] || die "Порт 443 занят ($o443). Освободи его и запусти снова."
o80=$(port_owner 80);   [ -z "$o80" ] || [ "$o80" = "nginx" ] || die "Порт 80 занят ($o80). Освободи его и запусти снова."
SITE_PORT=""
for p in 8443 9443 10443 11443; do [ -z "$(port_owner $p)" ] && { SITE_PORT=$p; break; }; done
[ -n "$SITE_PORT" ] || die "Нет свободного локального порта под сайт"
ok "порты 80 и 443 свободны, сайт будет на 127.0.0.1:$SITE_PORT"

step "Ставлю пакеты"
export DEBIAN_FRONTEND=noninteractive
# свежая система в первые минуты сама ставит обновления и держит apt — ждём, а не падаем
for i in $(seq 1 60); do
    fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock >/dev/null 2>&1 || break
    [ "$i" = 1 ] && warn "система ставит обновления, жду (до 10 минут)…"
    sleep 10
done
apt-get -o DPkg::Lock::Timeout=300 update -qq
apt-get -o DPkg::Lock::Timeout=300 install -y -qq nginx certbot curl unzip qrencode git python3 openssl >/dev/null
ok "nginx, certbot, qrencode, git"

step "Скачиваю панель"
if [ -d "$DIR/.git" ]; then git -C "$DIR" pull -q --ff-only; else git clone -q --depth 1 "$REPO" "$DIR"; fi
chmod +x "$DIR/vpn.py" "$DIR/bot.py"
ln -sf "$DIR/vpn.py" /usr/local/bin/vpn
ok "$DIR, команда vpn"

step "Сайт-заглушка и сертификат"
WEB="/var/www/$DOMAIN"
mkdir -p "$WEB"
python3 - "$DIR/site/index.html" "$WEB/index.html" "$BRAND" <<'PY'
import html, sys
src, dst, brand = sys.argv[1:]
b = html.escape(brand)   # название сайта не может сломать страницу
open(dst, "w").write(open(src).read().replace("{{BRAND_UPPER}}", b.upper()).replace("{{BRAND}}", b))
PY
cp "$DIR/site/favicon.svg" "$DIR/site/robots.txt" "$WEB/"
mkdir -p /var/www/html
NG="/etc/nginx/sites-available/$DOMAIN"
SN="$DOMAIN${WWW:+ $WWW}"
rm -f /etc/nginx/sites-enabled/default
cat > "$NG" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $SN;
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 301 https://\$host\$request_uri; }
}
EOF
ln -sf "$NG" /etc/nginx/sites-enabled/
nginx -t -q && { systemctl is-active -q nginx && systemctl reload nginx || systemctl restart nginx; }
certbot certonly --webroot -w /var/www/html -d "$DOMAIN" ${WWW:+-d "$WWW"} \
    --agree-tos --register-unsafely-without-email -n -q || die "Сертификат не выпустился. Проверь, что порт 80 открыт у хостера."
cat >> "$NG" <<EOF

server {
    listen 127.0.0.1:$SITE_PORT ssl http2;
    server_name $SN;
    ssl_certificate /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    root $WEB;
    index index.html;
    location / { try_files \$uri \$uri/ =404; }
}
EOF
mkdir -p /etc/letsencrypt/renewal-hooks/deploy
printf '#!/bin/sh\nsystemctl reload nginx\n' > /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
nginx -t -q && systemctl reload nginx
ok "https://$DOMAIN — сертификат Let's Encrypt, продлевается сам"

step "Xray: VLESS Reality «сам у себя»"
if ! command -v xray >/dev/null; then
    bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install >/dev/null
fi
command -v xray >/dev/null || die "Xray не установился"
PRIV=$(xray x25519 | awk '/Private/{print $NF}')
UUID=$(xray uuid)
SID=$(openssl rand -hex 8)
if [ -s "$CFG" ]; then B="$CFG.before-panel.$(date +%F-%H%M)"; cp "$CFG" "$B"; chmod 600 "$B"; fi
mkdir -p "$(dirname "$CFG")"
python3 - "$CFG" "$UUID" "$FIRST" "$SITE_PORT" "$PRIV" "$SID" "$DOMAIN" "$WWW" <<'PY'
import json, sys
cfg, uid, user, port, priv, sid, dom, www = sys.argv[1:]
names = [dom] + ([www] if www else [])
c = {"log": {"loglevel": "warning"},
     "inbounds": [{"tag": "vless-in", "port": 443, "protocol": "vless",
                   "settings": {"clients": [{"id": uid, "flow": "xtls-rprx-vision", "email": user}], "decryption": "none"},
                   "streamSettings": {"network": "tcp", "security": "reality",
                                      "realitySettings": {"target": f"127.0.0.1:{port}", "xver": 0, "serverNames": names,
                                                          "privateKey": priv, "shortIds": [sid]}},
                   "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": True}}],
     "outbounds": [{"protocol": "freedom", "tag": "direct"}, {"protocol": "blackhole", "tag": "block"}]}
json.dump(c, open(cfg, "w"), indent=2)
PY
chmod 600 "$CFG"
xray run -test -config "$CFG" >/dev/null || die "Конфиг Xray не прошёл проверку"
python3 - "$META" "$NAME" "$DOMAIN" "$IP" <<'PY'
import json, os, sys
meta, name, dom, ip = sys.argv[1:]
fd = os.open(meta, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump({"name": name, "host": dom, "ips": [ip], "warp_users": [], "warp_all": False, "disabled": {}},
              f, indent=2, ensure_ascii=False)
PY
systemctl enable -q xray
python3 - "$DIR" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import vpn
vpn.ensure_cron()
vpn.apply(vpn.load_cfg(), vpn.load_meta())   # включает счётчики трафика и перезапускает Xray
PY
ok "Xray слушает 443, чужим показывает сайт $DOMAIN"

step "Файрвол"
if ufw status 2>/dev/null | grep -q "Status: active"; then
    for p in $( { ss -tlnpH 2>/dev/null | grep sshd | grep -oE ':[0-9]+ ' | tr -d ': ' | sort -u; } || true); do ufw allow "$p/tcp" >/dev/null; done
    ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
    ok "ufw: открыты SSH, 80, 443"
else
    warn "ufw выключен — порты 80 и 443 должны быть открыты у хостера"
fi
rm -f /etc/cron.d/vless-warp-panel-update
ok "автообновление выключено — обновляй вручную (vpn update) или включи в меню"

step "Готово"
LINK=$(python3 - "$DIR" "$FIRST" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import vpn
print(vpn.link(sys.argv[2]))
PY
)
qrencode -t ansiutf8 -m 1 "$LINK"
echo
echo "$LINK"
echo
ok "Ключ пользователя $FIRST выше: QR или ссылка в Hiddify / v2rayNG / Happ"
echo -e "Меню: ${C}vpn${N}   ·   WARP — пункт «WARP: установить»   ·   Telegram-бот — пункт «Бот: установить»"
