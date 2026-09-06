#!/usr/bin/env bash
# One-time VPS setup for the MailAfrica Agent. Run as root on the MailAfrica
# VPS (the same box that serves api.mailafrica.online). This sets up:
#   - a restricted deploy user + SSH key so GitHub Actions can deploy
#   - the project checkout + venv + .env
#   - the webhook systemd unit
#   - reverse-proxy notes for the agent.mailafrica.online subdomain
#
# Usage (on the VPS, as root):
#   scp deploy/setup_vps.sh root@VPS:/tmp/ && ssh root@VPS bash /tmp/setup_vps.sh
set -euo pipefail

APP_USER="${APP_USER:-mailafrica}"
APP_DIR="/home/${APP_USER}/projects/mailafrica-agent"
WEBHOOK_URL="${WEBHOOK_URL:-https://agent.mailafrica.online/webhooks/mailafrica}"

echo "==> creating system user (if missing)"
id -u "$APP_USER" 2>/dev/null || useradd --create-home --shell /bin/bash "$APP_USER"

echo "==> installing uv (if missing)"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

echo "==> cloning the agent repo (if missing)"
if [ ! -d "$APP_DIR/.git" ]; then
  sudo -u "$APP_USER" git clone git@github.com:Camel-Creatives/MailAfrica-Agent.git "$APP_DIR"
fi

echo "==> GitHub read-only deploy key (so deploy.sh can git pull)"
GH_KEY="/home/${APP_USER}/.ssh/github_deploy_key"
if [ ! -f "$GH_KEY" ]; then
  ssh-keygen -t ed25519 -N "" -C "mailafrica-agent-vps-deploy" -f "$GH_KEY" >/dev/null
  sudo -u "$APP_USER" git -C "$APP_DIR" config core.sshCommand "ssh -i ${GH_KEY} -o IdentitiesOnly=yes"
  echo
  echo ">>> Add this PUBLIC key to the repo Settings -> Deploy keys"
  echo ">>> (MailAfrica-Agent, with read access) so the VPS can pull:"
  cat "$GH_KEY.pub"
  echo ">>> ---------------------------------------------------------------"
fi

echo "==> creating .env (keep your keys here, never commit them)"
if [ ! -f "$APP_DIR/.env" ]; then
  sudo -u "$APP_USER" cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  cat <<'EOF'

Now edit the .env with real values:
  sudo -u mailafrica nano /home/mailafrica/projects/mailafrica-agent/.env

Required:
  MAILAFRICA_API_KEY=...          # a MAIL_... key for the agent's account
  NGAMIA_API_KEY=ngm_...          # the LLM gateway key
  AGENT_WEBHOOK_SECRET=...        # random secret; use it when creating the webhook
  AGENT_DB_PATH=/home/mailafrica/projects/mailafrica-agent/agent.db
EOF
fi

echo "==> installing dependencies"
cd "$APP_DIR"
sudo -u "$APP_USER" uv sync --frozen

echo "==> installing systemd units"
sed "s|/home/mailafrica|/home/${APP_USER}|g" "$APP_DIR/deploy/mailafrica-agent-webhook.service" > /etc/systemd/system/mailafrica-agent-webhook.service
sed "s|/home/mailafrica|/home/${APP_USER}|g" "$APP_DIR/deploy/mailafrica-agent-mcp.service" > /etc/systemd/system/mailafrica-agent-mcp.service
systemctl daemon-reload

echo "==> restricted deploy key (one-time)"
# Generate a dedicated key whose authorized_keys entry is locked to the deploy
# script, so GitHub Actions can only pull + restart, never get a shell.
install -d -m 700 -o "$APP_USER" "/home/${APP_USER}/.ssh"
if [ ! -f "/home/${APP_USER}/.ssh/deploy_key" ]; then
  ssh-keygen -t ed25519 -N "" -f "/home/${APP_USER}/.ssh/deploy_key" >/dev/null
fi
AUTH="/home/${APP_USER}/.ssh/authorized_keys"
PUB=$(cat "/home/${APP_USER}/.ssh/deploy_key.pub")
grep -qF "$PUB" "$AUTH" 2>/dev/null || echo "command=\"cd ${APP_DIR} && ./deploy.sh\",no-agent-forwarding,no-port-forwarding,no-pty,no-user-rc,no-X11-forwarding $PUB" >> "$AUTH"
chown -R "$APP_USER":"$APP_USER" "/home/${APP_USER}/.ssh"
chmod 600 "$AUTH"

echo
echo "==> NEXT STEPS ==============================================="
echo "1. Edit ${APP_DIR}/.env with real keys (see above)."
echo "2. Add the GitHub deploy key.pub (printed above) to the repo"
echo "   Settings -> Deploy keys (read access) so deploy.sh can git pull."
echo "3. Add NGAMIA_* + these to GitHub repo secrets of MailAfrica-Agent:"
echo "     SSH_PRIVATE_KEY = the private half of /home/${APP_USER}/.ssh/deploy_key"
echo "     SSH_HOST        = the VPS public IP (13.140.167.42)"
echo "     SSH_PORT        = 22"
echo "     SSH_USER        = ${APP_USER}"
echo "4. Point agent.mailafrica.online at the VPS, then reverse-proxy it:
   Caddy:
     agent.mailafrica.online {
       reverse_proxy 127.0.0.1:8097
     }
   nginx:
     server_name agent.mailafrica.online;
     location / { proxy_pass http://127.0.0.1:8097; }
5. Remote MCP (multi-tenant OAuth): same box, new listener + subdomain.
   Caddy:
     mcp.mailafrica.online {
       reverse_proxy 127.0.0.1:8098
     }
   nginx:
     server_name mcp.mailafrica.online;
     location / { proxy_pass http://127.0.0.1:8098; }
   Then set the CamelAccounts OAuth client in .env (redirect URI must be
   https://mcp.mailafrica.online/oauth/callback) and start both units once
   .env is valid:
     systemctl enable --now mailafrica-agent-webhook mailafrica-agent-mcp
 6. In the MailAfrica app, create a webhook on each inbound address:
     URL:    ${WEBHOOK_URL}
     Secret: the AGENT_WEBHOOK_SECRET you set in .env
   Or let the agent create them (MCP tool create_webhook)."
echo "==============================================================="
