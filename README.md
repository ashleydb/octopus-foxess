# Energy Manager (FoxESS + Octopus + Zappi)

Prevent your car charger from draining your home battery! A lightweight, standalone Python script to automate FoxESS battery charging schedules based on Octopus Energy (Intelligent Go / Agile) tariffs and myenergi Zappi EV charger status.

## The Problem

_This project solves the same problem as my previous [homeassistant_foxess](https://github.com/ashleydb/homeassistant_foxess) integration, but without the overhead of Home Assistant. It runs as a simple background service on any Linux machine (like an Ubuntu Server or Raspberry Pi) and includes a mobile-friendly web dashboard._

I have Octopus Intelligent Go for my electicity tariff, which has a cheap rate during fixed hours at night, but can also have a variable rate during the day if Octopus deems that certain times are good for charging my EV car. I have a FoxESS solar generation system with batteries. However there are times when Octopus starts charging my car that it drains my home batteries, and I wanted to ensure the car charging came from the grid instead.

Most of the advice for solving this problem invole controlling the inverter using a modbus device installed on your inverter. That isn't really practical for my outdoor inverter (I couldn't run a huge ethernet cable, and had no power nearby to install a wifi dongle). However, the Open API from FoxESS allows for adjusting settings remotely, so this script leverages that API.

## How It Works

The script automatically decides whether your FoxESS inverter should be in **ForceCharge** (cheap) or **SelfUse** (expensive) mode. It runs every 5 minutes and evaluates the following conditions in priority order (highest wins):

1. **Manual Override:** Forced via the web dashboard or CLI (`--charge` / `--normal`).
2. **Fixed Off-Peak Window:** Automatically charges during your set quiet hours (default: 23:30 - 05:30).
3. **Octopus Free Electricity:** Detects active "Octoplus" free electricity sessions and forces a charge.
4. **Intelligent Go Dispatch:** Checks the Kraken API for dynamic smart charging slots. It will **only** trigger a battery charge if your car is actually plugged into the Zappi charger.
5. **Current Unit Rate:** Falls back to checking if the current Octopus unit rate has dropped below your custom threshold (e.g., `< 15.0p`).

## Features

* **Smart EV Context:** Integrates directly with your MyEnergi Zappi. It knows when your car is plugged in, preventing the house battery from draining into the car or triggering unnecessary grid charges when the car is away.
* **Local Web Dashboard:** A simple, password-protected Flask web interface to view the current status, active overrides, and recent logs.
* **Telegram Notifications:** Get instant alerts on your phone when the energy mode changes, API errors occur, or an Octopus Free Session is announced.
* **Diagnostic Mode:** Run a full simulation of the decision-making logic without actually writing any changes to your FoxESS inverter.

---

## Installation & Setup

### 1. Prerequisites & Dependencies

You will need a Linux environment (like an Ubuntu Server or Raspberry Pi) with Python 3 installed. 

Clone the repository to your chosen directory:

```bash
git clone https://github.com/ashleydb/energy-manager.git
cd energy-manager
```

### 2. Configuration

Before running the setup, ensure your credentials are in place:

```bash
cp config_template.json config.json
nano config.json
```

Replace all `REPLACE_ME` placeholders with your actual API keys and specific device serial numbers. Here is where to find your keys (see [config guide](config_guide.md) for more details):
* **Octopus:** Find your Account Number and API key under your online Octopus Dashboard.
* **FoxESS:** Generate an API key in the FoxESS Cloud User Centre. You will also need your Inverter Serial Number.
* **MyEnergi:** Generate an API key at `myaccount.myenergi.com` and find your Hub/Zappi serial numbers in the myenergi app.
* **Telegram:** Message `@BotFather` to create a bot and get a `bot_token`. The `chat_id` should be your own personal Telegram user ID number that you want the direct messages sent to (find this by messaging your bot and visiting `https://api.telegram.org/bot<TOKEN>/getUpdates`).
* **Dashboard:** Set a strong password. The dashboard is bound to `127.0.0.1` by default for security.

### 3. Automated Setup

Run the included setup script. This will automatically install the required Python packages (`requests` and `flask`), detect your current user and directory, generate the necessary Linux `systemd` background services, and start the system.

```bash
chmod +x setup.sh
./setup.sh
```

You will be prompted for your `sudo` password to write the background service files to `/etc/systemd/system/`.

### 4. Verify It's Running

Once the script finishes, check that both services are running smoothly:

```bash
sudo systemctl status energy-dashboard.service
sudo systemctl status energy-scheduler.timer
```

### 5. Exposing the Dashboard (Optional but Recommended)

Because the dashboard runs on `localhost` (127.0.0.1:8765), it is not directly accessible from the outside web. The safest and easiest way to access it from your phone is via a **Cloudflare Tunnel**.

1. Go to your Cloudflare Zero Trust dashboard.
2. Navigate to **Networks → Tunnels** and create/edit a tunnel for your server.
3. Add a public hostname (e.g., `energy.yourdomain.com`).
4. Point the service to `HTTP` and `localhost:8765`.

Cloudflare handles the HTTPS encryption automatically. When you visit your new subdomain, you'll be greeted with a Basic Auth prompt where you use the username `energy` and the password you set in `config.json`.

---

## Command Line Usage

You can also control the energy manager directly from the terminal if you prefer scripting or integrating it with tools like n8n:

```bash
python3 energy_manager.py              # Normal scheduled run
python3 energy_manager.py --diagnose   # Test all APIs, simulate decision (no FoxESS write)
python3 energy_manager.py --charge     # Manual override: force charge
python3 energy_manager.py --normal     # Manual override: force normal (SelfUse)
python3 energy_manager.py --clear      # Clear any manual override
python3 energy_manager.py --status     # Print current state.json
python3 energy_manager.py --get-schedule # Read current FoxESS schedule
```
