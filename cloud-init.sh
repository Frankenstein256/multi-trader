#!/bin/bash
# Paste this into Oracle Cloud: Create instance -> Show advanced options -> Management -> Cloud-init script.
# Fill in the 5 values in CAPITALS below first.
set -e
REPO_URL="https://github.com/YOUR_GITHUB_USERNAME/YOUR_REPO.git"
DOMAIN="YOURNAME.duckdns.org"
ADMIN_PASSWORD="CHOOSE_A_LONG_PASSWORD"
WEBHOOK_SECRET="CHOOSE_ANOTHER_LONG_SECRET"
METAAPI_TOKEN=""

curl -fsSL https://get.docker.com | sh
apt-get install -y git iptables-persistent || true
iptables -I INPUT 1 -p tcp --dport 80 -j ACCEPT
iptables -I INPUT 1 -p tcp --dport 443 -j ACCEPT
netfilter-persistent save || true

git clone "$REPO_URL" /opt/app
cd /opt/app
cat > .env <<ENVEOF
DOMAIN=$DOMAIN
ADMIN_USER=admin
ADMIN_PASSWORD=$ADMIN_PASSWORD
WEBHOOK_SECRET=$WEBHOOK_SECRET
METAAPI_TOKEN=$METAAPI_TOKEN
ENVEOF
docker compose up -d --build
