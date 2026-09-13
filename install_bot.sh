#!/bin/bash
# Установка бота УИ как сервиса на Ubuntu/Debian сервере.
# Запуск:  bash install_bot.sh <ТОКЕН_БОТА>
# Рядом должен лежать ui_bot.py (и, если переносишь, ui_bot.db).

set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "Нужны права администратора. Запусти так:  sudo bash install_bot.sh"
    exit 1
fi

TOKEN="$1"
DIR=/opt/ui_bot

# Токен можно передать аргументом, но лучше ввести по запросу —
# тогда он не останется в истории команд.
if [ -z "$TOKEN" ]; then
    printf "Вставь токен бота и нажми Enter: "
    read -r TOKEN
fi

if [ -z "$TOKEN" ]; then
    echo "Токен не введён — установка отменена."
    exit 1
fi

if [ ! -f ui_bot.py ]; then
    echo "Рядом со скриптом нет ui_bot.py — положи его в эту же папку."
    exit 1
fi

echo "==> Ставлю Python и venv"
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip cron >/dev/null || true

echo "==> Готовлю папку $DIR"
mkdir -p "$DIR"
cp ui_bot.py "$DIR/"
# базу копируем только если её там ещё нет — чтобы не затереть рабочую
if [ -f ui_bot.db ] && [ ! -f "$DIR/ui_bot.db" ]; then
    cp ui_bot.db "$DIR/"
    echo "    перенёс базу с заявками"
fi

echo "==> Ставлю библиотеку maxapi"
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q --upgrade pip
"$DIR/venv/bin/pip" install -q maxapi

echo "==> Создаю сервис автозапуска"
cat > /etc/systemd/system/ui-bot.service <<EOF
[Unit]
Description=CYBERSTEEL UI bot (MAX)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$DIR
Environment=MAX_UI_BOT_TOKEN=$TOKEN
ExecStart=$DIR/venv/bin/python $DIR/ui_bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

echo "==> Настраиваю ежедневный бэкап базы"
mkdir -p "$DIR/backups"
cat > "$DIR/backup.sh" <<EOF
#!/bin/bash
# копия базы раз в сутки, храним 14 последних
cp $DIR/ui_bot.db $DIR/backups/ui_bot_\$(date +%F).db 2>/dev/null || exit 0
ls -1t $DIR/backups/*.db | tail -n +15 | xargs -r rm --
EOF
chmod +x "$DIR/backup.sh"
if command -v crontab >/dev/null 2>&1; then
    ( crontab -l 2>/dev/null | grep -v "$DIR/backup.sh" ; echo "0 3 * * * $DIR/backup.sh" ) | crontab -
else
    echo "    cron не найден — бэкап можно запускать вручную: $DIR/backup.sh"
fi

echo "==> Запускаю"
if ! command -v systemctl >/dev/null 2>&1; then
    echo "На этом сервере нет systemd. Запусти вручную:"
    echo "  cd $DIR && MAX_UI_BOT_TOKEN=$TOKEN venv/bin/python ui_bot.py"
    exit 0
fi
systemctl daemon-reload
systemctl enable --now ui-bot >/dev/null 2>&1
sleep 3
systemctl --no-pager status ui-bot | head -n 12 || true

echo
echo "Готово. Полезные команды:"
echo "  journalctl -u ui-bot -f      — смотреть работу бота вживую"
echo "  systemctl restart ui-bot     — перезапустить"
echo "  systemctl stop ui-bot        — остановить"
echo "Файлы бота: $DIR (база ui_bot.db, фото в photos/, бэкапы в backups/)"
