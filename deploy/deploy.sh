#!/bin/bash
# 在目标 Linux 服务器上以 sudo 执行：安装依赖、创建 venv、注册 systemd 服务与每日备份
set -e
cd /opt/specimen-manager
echo "== 创建虚拟环境并安装依赖 =="
if [ ! -d venv ]; then
  python3 -m venv venv 2>/dev/null || (apt-get -o DPkg::Lock::Timeout=150 install -y python3.12-venv && python3 -m venv venv)
fi
./venv/bin/pip install -q -r requirements.txt
echo "== 注册 systemd 服务（崩溃自动重启 + 每天 03:00 备份）=="
# 服务以执行 sudo 的普通用户运行（避免 root 属主问题）
RUN_AS="${SUDO_USER:-$(logname 2>/dev/null || echo root)}"
for f in specimen-manager.service specimen-manager-backup.service; do
  grep -q '^User=' "deploy/$f" || sed -i "/^Type=simple/a User=$RUN_AS" "deploy/$f"
done
cp deploy/*.service deploy/*.timer /etc/systemd/system/
chown -R "$RUN_AS:$RUN_AS" /opt/specimen-manager
mkdir -p backups uploads && chown -R "$RUN_AS:$RUN_AS" backups uploads data.db
systemctl daemon-reload
systemctl enable --now specimen-manager.timer
systemctl enable --now specimen-manager.service
sleep 2
systemctl --no-pager status specimen-manager.service | head -6
echo "== 部署完成：浏览器访问 http://服务器IP:8000 =="
