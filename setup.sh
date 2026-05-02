#!/bin/bash

# Exit immediately if a command exits with a non-zero status
set -e

echo "========================================"
echo "         Energy Manager Setup"
echo "========================================"

# 1. Determine current directory and user dynamically
APP_DIR=$(pwd)
USER_NAME=$(whoami)
echo "[*] Setting up in: $APP_DIR"
echo "[*] User: $USER_NAME"

# 2. Install Python dependencies
echo "[*] Installing Python dependencies..."
pip3 install requests flask

# 3. Check for configuration file
if [ ! -f "config.json" ]; then
    if [ -f "config_template.json" ]; then
        cp config_template.json config.json
        echo "[*] Created config.json from template."
    else
        echo "[!] Warning: config.json not found. Please create one before running."
    fi
else
    echo "[*] config.json already exists."
fi

# 4. Generate systemd service files
echo "[*] Creating systemd service files..."

# Dashboard Service
cat <<EOF | sudo tee /etc/systemd/system/energy-dashboard.service > /dev/null
[Unit]
Description=Energy Manager Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/python3 $APP_DIR/energy_manager.py --dashboard
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# Scheduler Service (Single Run)
cat <<EOF | sudo tee /etc/systemd/system/energy-scheduler.service > /dev/null
[Unit]
Description=Energy Manager Scheduler (single run)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$USER_NAME
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/python3 $APP_DIR/energy_manager.py
EOF

# Scheduler Timer (Every 5 mins)
cat <<EOF | sudo tee /etc/systemd/system/energy-scheduler.timer > /dev/null
[Unit]
Description=Run Energy Manager every 5 minutes
Requires=energy-scheduler.service

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min
AccuracySec=10s

[Install]
WantedBy=timers.target
EOF

# 5. Enable and start systemd services
echo "[*] Reloading systemd daemon..."
sudo systemctl daemon-reload

echo "[*] Enabling and starting services..."
sudo systemctl enable energy-dashboard.service energy-scheduler.timer
sudo systemctl restart energy-dashboard.service energy-scheduler.timer

echo "========================================"
echo "             Setup Complete!"
echo "========================================"
echo "Next steps:"
echo "1. IMPORTANT: Edit config.json with your API keys if you haven't already."
echo "2. Check the dashboard status: sudo systemctl status energy-dashboard.service"
echo "3. Check the scheduler status: sudo systemctl status energy-scheduler.timer"
echo "4. View your logs using: tail -f energy_manager.log"