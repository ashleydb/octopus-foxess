#!/usr/bin/env python3
"""
Energy Manager — FoxESS + Octopus Intelligent Go + Zappi
=========================================================
All configuration lives in config.json (same folder as this script).

Decision priority (highest wins):
 1. Manual override (--charge / --normal)
 2. Fixed off-peak window (default 23:30-05:30)
 3. Octopus Free Electricity Session (Octoplus)
 4. Intelligent Go planned dispatch (car plugged in)
 5. Current unit rate vs threshold

Usage
-----
 python3 energy_manager.py # Normal scheduled run
 python3 energy_manager.py --diagnose # Test all APIs, simulate decision (no FoxESS write)
 python3 energy_manager.py --charge # Manual override: force charge
 python3 energy_manager.py --normal # Manual override: force normal (SelfUse)
 python3 energy_manager.py --clear # Clear any manual override
 python3 energy_manager.py --dashboard # Start web dashboard
 python3 energy_manager.py --status # Print current state.json
 python3 energy_manager.py --get-schedule # Read current FoxESS schedule

Dependencies
------------
 pip3 install requests flask
"""

import sys
import json
import hashlib
import time
import datetime
import logging
import logging.handlers
import argparse
import subprocess
import functools
import io
from pathlib import Path

# =============================================================================
# PATHS
# =============================================================================

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "state.json"
LOG_FILE = BASE_DIR / "energy_manager.log"
DIAGNOSE_FILE = BASE_DIR / "diagnose_output.txt"

# =============================================================================
# CONFIG LOADING
# =============================================================================

def load_config() -> dict:
 try:
  return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
 except FileNotFoundError:
  print(f"ERROR: config.json not found at {CONFIG_FILE}")
  sys.exit(1)
 except json.JSONDecodeError as e:
  print(f"ERROR: config.json is not valid JSON: {e}")
  sys.exit(1)

CFG = load_config()

OC = CFG["octopus"]
FX = CFG["foxess"]
ME = CFG["myenergi"]
BAT = CFG["battery"]
OP = CFG["offpeak_window"]
DASH = CFG["dashboard"]
TG = CFG.get("telegram", {})
LG = CFG.get("logging", {})

OCTOPUS_API_KEY = OC["api_key"]
OCTOPUS_ACCOUNT = OC["account"]
OCTOPUS_MPAN = OC["mpan"]
CHEAP_THRESHOLD_P = float(OC["cheap_threshold_pence"])
CHECK_DISPATCHES = bool(OC["check_dispatches"])
CHECK_FREE_SESSIONS = bool(OC["check_free_sessions"])
FREE_SESSION_CACHE_MINUTES = int(OC["free_session_cache_minutes"])

FOXESS_API_KEY = FX["api_key"]
FOXESS_DEVICE_SN = FX["device_sn"]
FOXESS_BASE_URL = FX["base_url"]
CHARGE_POWER_WATTS = int(FX.get("charge_power_watts", 10500))

MYENERGI_HUB_SERIAL = str(ME["hub_serial"])
MYENERGI_API_KEY = ME["api_key"]
ZAPPI_SERIAL = str(ME["zappi_serial"])

MIN_SOC_CHEAP = int(BAT["min_soc_cheap"])
MIN_SOC_EXPENSIVE = int(BAT["min_soc_expensive"])

OFFPEAK_START = datetime.time(*[int(x) for x in OP["start"].split(":")])
OFFPEAK_END = datetime.time(*[int(x) for x in OP["end"].split(":")])

DASHBOARD_HOST = DASH["host"]
DASHBOARD_PORT = int(DASH["port"])
DASHBOARD_PASSWORD = DASH.get("password", "")

TG_TOKEN = TG.get("bot_token", "")
TG_CHAT_ID = TG.get("chat_id", "")
TG_NOTIFY_MODE_CHANGE = TG.get("notify_mode_change", True)
TG_NOTIFY_FOXESS_ERR = TG.get("notify_foxess_error", True)
TG_NOTIFY_FREE = TG.get("notify_free_session", True)

LOG_MAX_BYTES = int(LG.get("max_bytes", 500_000))
LOG_BACKUP_COUNT = int(LG.get("backup_count", 3))

ZAPPI_PLUGGED_IN_STATES = {"B1", "B2", "C1", "C2"}

PST_LABELS = {
 "A": "Disconnected",
 "B1": "Connected (not ready)",
 "B2": "Waiting for EV",
 "C1": "Charging (low power)",
 "C2": "Charging",
}

# =============================================================================
# LOGGING
# =============================================================================

def setup_logging(dashboard_mode: bool = False):
 fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
 root = logging.getLogger()
 root.setLevel(logging.INFO)
 root.handlers.clear()

 stdout_handler = logging.StreamHandler(sys.stdout)
 stdout_handler.setFormatter(fmt)
 root.addHandler(stdout_handler)

 if not dashboard_mode:
  file_handler = logging.handlers.RotatingFileHandler(
   LOG_FILE,
   maxBytes=LOG_MAX_BYTES,
   backupCount=LOG_BACKUP_COUNT,
   encoding="utf-8",
  )
  file_handler.setFormatter(fmt)
  root.addHandler(file_handler)

 logging.getLogger("werkzeug").setLevel(logging.ERROR)

log = logging.getLogger(__name__)

# =============================================================================
# TELEGRAM
# =============================================================================

def telegram_configured() -> bool:
 return bool(TG_TOKEN and TG_CHAT_ID and
  TG_TOKEN != "REPLACE_ME" and TG_CHAT_ID != "REPLACE_ME")


def send_telegram(message: str):
 """Send a message via Telegram bot. Silently skips if not configured."""
 if not telegram_configured():
  return

 import requests

 url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
 try:
  r = requests.post(
   url,
   json={"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML"},
   timeout=10,
  )
  if r.status_code == 200:
   log.info("Telegram notification sent")
  else:
   log.warning(f"Telegram send failed: {r.status_code} {r.text}")
 except Exception as e:
  log.warning(f"Telegram send error: {e}")


def notify_mode_change(old_mode: str, new_mode: str, reason: str, is_cheap: bool):
 if not TG_NOTIFY_MODE_CHANGE:
  return
 icon = "🔋" if is_cheap else "💰"
 label = "Force Charge (cheap)" if is_cheap else "Self-Use (expensive)"
 send_telegram(
  f"{icon} <b>Energy mode changed</b>\n"
  f"<b>{old_mode}</b> → <b>{new_mode}</b>\n"
  f"Reason: {reason}\n"
  f"Mode: {label}"
 )


def notify_free_session(session: dict):
 if not TG_NOTIFY_FREE:
  return
 send_telegram(
  f"⚡ <b>Free electricity session starting!</b>\n"
  f"{session.get('name', 'Session')}\n"
  f"Until: {session.get('end', '?')}"
 )


def notify_foxess_error(min_soc: int, error: str):
 if not TG_NOTIFY_FOXESS_ERR:
  return
 send_telegram(
  f"⚠️ <b>FoxESS API error</b>\n"
  f"Failed to set MinSoc={min_soc}%\n"
  f"Error: {error}"
 )

# =============================================================================
# STATE FILE
# =============================================================================

def load_state() -> dict:
 try:
  return json.loads(STATE_FILE.read_text(encoding="utf-8"))
 except Exception:
  return {}

def save_state(updates: dict):
 state = load_state()
 state.update(updates)
 STATE_FILE.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")

# =============================================================================
# TIME HELPERS
# =============================================================================

def is_in_fixed_offpeak() -> bool:
 now = datetime.datetime.now().time()
 return now >= OFFPEAK_START or now <= OFFPEAK_END

def now_utc() -> datetime.datetime:
 return datetime.datetime.now(datetime.timezone.utc)

def now_local() -> datetime.datetime:
 return datetime.datetime.now()

def utc_now_iso() -> str:
 return now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")

def current_utc_slot() -> tuple[str, str]:
 n = now_utc()
 mins = 0 if n.minute < 30 else 30
 slot_start = n.replace(minute=mins, second=0, microsecond=0)
 slot_end = slot_start + datetime.timedelta(minutes=30)
 fmt = "%Y-%m-%dT%H:%M:%SZ"
 return slot_start.strftime(fmt), slot_end.strftime(fmt)

def parse_iso(s: str) -> datetime.datetime:
 return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))

def fmt_local(dt: datetime.datetime) -> str:
 return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

# =============================================================================
# FOXESS — signature
# =============================================================================

def foxess_signature(url_path: str) -> tuple[str, str]:
 timestamp = str(int(time.time() * 1000))
 sign_string = url_path + r"\r\n" + FOXESS_API_KEY + r"\r\n" + timestamp
 signature = hashlib.md5(sign_string.encode()).hexdigest()
 return timestamp, signature

# =============================================================================
# FOXESS — GET schedule
# =============================================================================

def get_foxess_schedule() -> dict | None:
 import requests

 url_path = "/op/v1/device/scheduler/get"
 timestamp, signature = foxess_signature(url_path)

 try:
  r = requests.post(
   FOXESS_BASE_URL + url_path,
   json={"deviceSN": FOXESS_DEVICE_SN},
   headers={
    "token": FOXESS_API_KEY,
    "signature": signature,
    "timestamp": timestamp,
    "lang": "en",
    "Content-Type": "application/json",
   },
   timeout=15,
  )
  r.raise_for_status()
  return r.json()
 except Exception as e:
  print(f"FoxESS GET schedule failed: {e}")
  return None

# =============================================================================
# FOXESS — SET schedule
# =============================================================================

def make_group(start_h, start_m, end_h, end_m, work_mode) -> dict:
 fd_pwr = CHARGE_POWER_WATTS if work_mode == "ForceCharge" else 0
 return {
  "enable": 1,
  "startHour": start_h,
  "startMinute": start_m,
  "endHour": end_h,
  "endMinute": end_m,
  "workMode": work_mode,
  "minSocOnGrid": 20,
  "fdSoc": 100,
  "fdPwr": fd_pwr,
  "maxSoc": 100,
 }


def set_foxess_schedule(min_soc: int) -> bool:
 import requests

 work_mode = "ForceCharge" if min_soc >= 100 else "SelfUse"
 url_path = "/op/v1/device/scheduler/enable"
 timestamp, signature = foxess_signature(url_path)

 body = {
  "deviceSN": FOXESS_DEVICE_SN,
  "groups": [
   make_group(0, 0, 5, 29, "ForceCharge"),
   make_group(5, 30, 23, 29, work_mode),
   make_group(23, 30, 23, 59, "ForceCharge"),
  ],
 }

 log.info(f"Sending FoxESS schedule: daytime={work_mode}, ForceCharge fdPwr={CHARGE_POWER_WATTS}W")

 try:
  r = requests.post(
   FOXESS_BASE_URL + url_path,
   json=body,
   headers={
    "token": FOXESS_API_KEY,
    "signature": signature,
    "timestamp": timestamp,
    "lang": "en",
    "Content-Type": "application/json",
   },
   timeout=15,
  )
  r.raise_for_status()
  errno = r.json().get("errno", 999)
  if errno == 0:
   log.info(f"FoxESS OK -- daytime={work_mode}, grid charge={CHARGE_POWER_WATTS}W")
   return True
  msg = f"errno {errno}"
  log.error(f"FoxESS API error {errno}: {r.text}")
  notify_foxess_error(min_soc, msg)
 except Exception as e:
  log.error(f"FoxESS request failed: {e}")
  notify_foxess_error(min_soc, str(e))
 return False

# =============================================================================
# OCTOPUS — Kraken JWT
# =============================================================================

def get_kraken_token() -> str | None:
 import requests

 mutation = """
 mutation obtainKrakenToken($input: ObtainJSONWebTokenInput!) {
  obtainKrakenToken(input: $input) { token }
 }
 """
 try:
  r = requests.post(
   "https://api.octopus.energy/v1/graphql/",
   json={"query": mutation, "variables": {"input": {"APIKey": OCTOPUS_API_KEY}}},
   timeout=10,
  )
  r.raise_for_status()
  return r.json()["data"]["obtainKrakenToken"]["token"]
 except Exception as e:
  log.warning(f"Kraken token failed: {e}")
  return None

# =============================================================================
# OCTOPUS — Free Electricity Sessions
# =============================================================================

def fetch_free_sessions(token: str) -> list[dict]:
 import requests

 query = """
 query FreeElectricityEvents($accountNumber: String!, $mpan: String!) {
  customerFlexibilityCampaignEvents(
   accountNumber: $accountNumber
   campaignSlug: "free_electricity"
   supplyPointIdentifier: $mpan
   first: 10
  ) {
   edges { node { name code startAt endAt } }
  }
 }
 """
 try:
  r = requests.post(
   "https://api.octopus.energy/v1/graphql/",
   json={
    "query": query,
    "variables": {"accountNumber": OCTOPUS_ACCOUNT, "mpan": OCTOPUS_MPAN},
   },
   headers={"Authorization": f"JWT {token}"},
   timeout=10,
  )
  r.raise_for_status()
  edges = (
   r.json()
   .get("data", {})
   .get("customerFlexibilityCampaignEvents", {})
   .get("edges", []) or []
  )
  sessions = []
  for edge in edges:
   node = edge.get("node", {})
   if node.get("startAt") and node.get("endAt"):
    sessions.append({
     "name": node.get("name", ""),
     "start": node["startAt"],
     "end": node["endAt"],
    })
  return sessions
 except Exception as e:
  log.warning(f"Free session fetch failed: {e}")
  return []


def get_free_sessions_cached() -> list[dict]:
 if not CHECK_FREE_SESSIONS:
  return []

 state = load_state()
 fetched = state.get("free_sessions_fetched_at")
 cache_ttl = FREE_SESSION_CACHE_MINUTES * 60

 if fetched:
  age = (datetime.datetime.now() - datetime.datetime.fromisoformat(fetched)).total_seconds()
  if age < cache_ttl:
   return state.get("free_sessions", [])

 log.info("Free session cache stale - refreshing from API")
 token = get_kraken_token()
 if not token:
  log.warning("Cannot refresh free sessions - Kraken token failed")
  return state.get("free_sessions", [])

 sessions = fetch_free_sessions(token)
 save_state({
  "free_sessions": sessions,
  "free_sessions_fetched_at": datetime.datetime.now().isoformat(),
 })
 return sessions


def get_active_free_session() -> dict | None:
 n = now_utc()
 for s in get_free_sessions_cached():
  if parse_iso(s["start"]) <= n <= parse_iso(s["end"]):
   log.info(f"Free electricity session ACTIVE: {s['name']}")
   return s
 return None


def get_next_free_session() -> dict | None:
 n = now_utc()
 upcoming = [s for s in get_free_sessions_cached() if parse_iso(s["start"]) > n]
 return sorted(upcoming, key=lambda s: s["start"])[0] if upcoming else None

# =============================================================================
# OCTOPUS — tariff discovery
# =============================================================================

def is_export_tariff(tariff_code: str) -> bool:
 upper = tariff_code.upper()
 return "OUTGOING" in upper or "EXPORT" in upper


def get_octopus_tariff() -> tuple[str, str] | None:
 import requests

 state = load_state()
 cached_at = state.get("tariff_cached_at")
 if cached_at:
  age = (datetime.datetime.now() - datetime.datetime.fromisoformat(cached_at)).total_seconds()
  if age < 86400:
   product = state.get("tariff_product")
   code = state.get("tariff_code")
   if product and code:
    return product, code

 url = f"https://api.octopus.energy/v1/accounts/{OCTOPUS_ACCOUNT}/"
 try:
  r = requests.get(url, auth=(OCTOPUS_API_KEY, ""), timeout=10)
  r.raise_for_status()
  n_iso = utc_now_iso()

  for prop in r.json().get("properties", []):
   for ep in prop.get("electricity_meter_points", []):
    for agreement in ep.get("agreements", []):
     valid_to = agreement.get("valid_to")
     tariff_code = agreement.get("tariff_code", "")

     if valid_to is not None and valid_to <= n_iso:
      continue
     if is_export_tariff(tariff_code):
      log.info(f"Skipping export tariff: {tariff_code}")
      continue
     if tariff_code:
      parts = tariff_code.split("-")
      product_code = "-".join(parts[2:-1])
      log.info(f"Import tariff: {tariff_code} (product: {product_code})")
      save_state({
       "tariff_product": product_code,
       "tariff_code": tariff_code,
       "tariff_cached_at": datetime.datetime.now().isoformat(),
      })
      return product_code, tariff_code
 except Exception as e:
  log.warning(f"Tariff discovery failed: {e}")
  return None

# =============================================================================
# OCTOPUS — current unit rate
# =============================================================================

def get_current_unit_rate() -> float | None:
 import requests

 tariff = get_octopus_tariff()
 if not tariff:
  return None

 product_code, tariff_code = tariff
 period_from, period_to = current_utc_slot()
 url = (
  f"https://api.octopus.energy/v1/products/{product_code}/"
  f"electricity-tariffs/{tariff_code}/standard-unit-rates/"
  f"?period_from={period_from}&period_to={period_to}"
 )
 try:
  r = requests.get(url, auth=(OCTOPUS_API_KEY, ""), timeout=10)
  r.raise_for_status()
  results = r.json().get("results", [])
  if results:
   rate = float(results[0]["value_inc_vat"])
   log.info(f"Current unit rate: {rate:.2f}p/kWh (threshold: {CHEAP_THRESHOLD_P}p)")
   return rate
  log.warning("Unit rate API returned no results for current slot")
 except Exception as e:
  log.warning(f"Unit rate fetch failed: {e}")
  return None

# =============================================================================
# OCTOPUS — dispatches
# =============================================================================

def get_all_dispatches() -> list[dict]:
 import requests

 token = get_kraken_token()
 if not token:
  return []

 query = """
 query plannedDispatches($accountNumber: String!) {
  plannedDispatches(accountNumber: $accountNumber) { startDt endDt }
 }
 """
 try:
  r = requests.post(
   "https://api.octopus.energy/v1/graphql/",
   json={"query": query, "variables": {"accountNumber": OCTOPUS_ACCOUNT}},
   headers={"Authorization": f"JWT {token}"},
   timeout=10,
  )
  r.raise_for_status()
  return r.json().get("data", {}).get("plannedDispatches", []) or []
 except Exception as e:
  log.warning(f"Dispatch fetch failed: {e}")
  return []


def is_dispatch_active() -> bool:
 if not CHECK_DISPATCHES:
  return False

 dispatches = get_all_dispatches()
 n = now_utc()
 for d in dispatches:
  if parse_iso(d["startDt"]) <= n <= parse_iso(d["endDt"]):
   log.info(f"Active dispatch: {d['startDt']} -> {d['endDt']}")
   return True

 log.info(f"No active dispatch ({len(dispatches)} planned total)")
 return False

# =============================================================================
# MYENERGI / ZAPPI
# =============================================================================

def get_myenergi_server() -> str:
 import requests
 from requests.auth import HTTPDigestAuth

 try:
  r = requests.get(
   "https://director.myenergi.net",
   auth=HTTPDigestAuth(MYENERGI_HUB_SERIAL, MYENERGI_API_KEY),
   timeout=10,
  )
  asn = r.headers.get("x_myenergi-asn", "").strip()
  if asn and asn != "undefined":
   return asn
 except Exception as e:
  log.debug(f"myenergi director unreachable ({e}), using s18 fallback")
  return "s18.myenergi.net"


def get_zappi_plug_state() -> str | None:
 import requests
 from requests.auth import HTTPDigestAuth

 server = get_myenergi_server()
 url = f"https://{server}/cgi-jstatus-Z{ZAPPI_SERIAL}"
 try:
  r = requests.get(
   url,
   auth=HTTPDigestAuth(MYENERGI_HUB_SERIAL, MYENERGI_API_KEY),
   timeout=10,
  )
  r.raise_for_status()
  zappis = r.json().get("zappi", [])
  if zappis:
   pst = zappis[0].get("pst", "A")
   log.info(f"Zappi plug state: {pst}")
   return pst
  log.info("Zappi not reporting - treating as disconnected")
  return "A"
 except Exception as e:
  log.warning(f"Zappi status check failed: {e}")
  return None


def is_car_plugged_in() -> bool:
 pst = get_zappi_plug_state()
 return False if pst is None else pst in ZAPPI_PLUGGED_IN_STATES

# =============================================================================
# MAIN SCHEDULER LOGIC
# =============================================================================

def run():
 log.info("--- Energy Manager run started ---")
 state = load_state()
 override = state.get("override")

 if override == "charge":
  log.info("Manual override: FORCE CHARGE")
  is_cheap, min_soc, reason = True, MIN_SOC_CHEAP, "manual_override_charge"
  car_plugged_in = active_session = None

 elif override == "normal":
  log.info("Manual override: FORCE NORMAL")
  is_cheap, min_soc, reason = False, MIN_SOC_EXPENSIVE, "manual_override_normal"
  car_plugged_in = active_session = None

 else:
  active_session = None
  car_plugged_in = None

  if is_in_fixed_offpeak():
   is_cheap = True
   reason = "fixed_offpeak_window"
   log.info("In fixed off-peak window (23:30-05:30)")

  elif (active_session := get_active_free_session()) is not None:
   is_cheap = True
   reason = f"free_electricity_session:{active_session.get('name', '')}"

  else:
   car_plugged_in = is_car_plugged_in()
   if car_plugged_in and is_dispatch_active():
    is_cheap = True
    reason = "intelligent_dispatch"
   else:
    rate = get_current_unit_rate()
    if rate is not None:
     is_cheap = rate < CHEAP_THRESHOLD_P
     reason = f"rate_{rate:.1f}p_{'cheap' if is_cheap else 'expensive'}"
    else:
     is_cheap = False
     reason = "rate_unknown_defaulting_expensive"

  min_soc = MIN_SOC_CHEAP if is_cheap else MIN_SOC_EXPENSIVE
  log.info(f"Decision: {'CHEAP' if is_cheap else 'EXPENSIVE'} ({reason}) -- MinSoc={min_soc}%")

  last_min_soc = state.get("min_soc")
  last_work_mode = "ForceCharge" if (last_min_soc or 0) >= 100 else "SelfUse"
  new_work_mode = "ForceCharge" if min_soc >= 100 else "SelfUse"
  foxess_ok = state.get("foxess_ok", False)

  if min_soc != last_min_soc:
   log.info(f"Mode changed ({last_min_soc}% -> {min_soc}%) -- updating FoxESS")
   foxess_ok = set_foxess_schedule(min_soc)
   if foxess_ok:
    # Notify on transition — but suppress the first run (last_min_soc is None)
    if last_min_soc is not None:
     notify_mode_change(last_work_mode, new_work_mode, reason, is_cheap)
    # Also send a specific free session alert when one becomes active
    if active_session and last_min_soc != MIN_SOC_CHEAP:
     notify_free_session(active_session)
   elif not foxess_ok:
    log.info(f"Retrying FoxESS (previous call failed, MinSoc still {min_soc}%)")
    foxess_ok = set_foxess_schedule(min_soc)
  else:
   log.info(f"Mode unchanged ({min_soc}%) -- skipping FoxESS update")

 save_state({
  "last_run": datetime.datetime.now().isoformat(timespec="seconds"),
  "is_cheap": is_cheap,
  "reason": reason,
  "min_soc": min_soc,
  "foxess_ok": foxess_ok,
  "override": override,
  "car_plugged_in": car_plugged_in,
  "active_free_session": active_session,
  "next_free_session": get_next_free_session(),
 })
 log.info("--- Run complete ---")

# =============================================================================
# DIAGNOSE MODE
# =============================================================================

SEP = "=" * 60
SEP2 = "-" * 60

def hdr(title, out=None):
 line = f"\n{SEP}\n {title}\n{SEP}"
 print(line)
 if out is not None:
  out.write(line + "\n")

def _p(prefix, msg, out=None):
 line = f" {prefix} {msg}"
 print(line)
 if out is not None:
  out.write(line + "\n")

def ok(msg, out=None): _p("✓", msg, out)
def warn(msg, out=None): _p("⚠", msg, out)
def err(msg, out=None): _p("✗", msg, out)
def info(msg, out=None): _p(" ", msg, out)


def diagnose(out=None):
 """
 Run every API call independently, print results and simulate the full
 decision — without writing to FoxESS. Pass a file-like object as `out`
 to capture output (used by the dashboard endpoint).
 """
 import requests
 from requests.auth import HTTPDigestAuth

 header = (
  f"\n{'*' * 60}\n"
  f" Energy Manager — Diagnostic Mode\n"
  f" {now_local().strftime('%Y-%m-%d %H:%M:%S')} local / "
  f"{now_utc().strftime('%H:%M:%S')} UTC\n"
  f"{'*' * 60}"
 )
 print(header)
 if out:
  out.write(header + "\n")

 results = {}

 # 1. Off-peak window
 hdr("1. Fixed off-peak window", out)
 in_offpeak = is_in_fixed_offpeak()
 info(f"Window: {OFFPEAK_START.strftime('%H:%M')} - {OFFPEAK_END.strftime('%H:%M')} (local)", out)
 info(f"Current local time: {now_local().strftime('%H:%M:%S')}", out)
 if in_offpeak:
  ok("Currently IN fixed off-peak window", out)
 else:
  info("Not in fixed off-peak window", out)
 results["in_offpeak"] = in_offpeak

 # 2. Zappi
 hdr("2. myenergi Zappi plug state", out)
 info(f"Hub serial: {MYENERGI_HUB_SERIAL}", out)
 info(f"Zappi serial: {ZAPPI_SERIAL}", out)

 try:
  server = get_myenergi_server()
  info(f"Director assigned server: {server}", out)
  url = f"https://{server}/cgi-jstatus-Z{ZAPPI_SERIAL}"
  auth = HTTPDigestAuth(MYENERGI_HUB_SERIAL, MYENERGI_API_KEY)
  resp = requests.get(url, auth=auth, timeout=10)
  resp.raise_for_status()
  raw_zappi = resp.json()
  info(f"Raw response: {json.dumps(raw_zappi, indent=4)}", out)

  zappis = raw_zappi.get("zappi", [])
  if zappis:
   z = zappis[0]
   pst = z.get("pst", "A")
   label = PST_LABELS.get(pst, f"Unknown ({pst})")
   results["zappi_pst"] = pst
   results["car_plugged_in"] = pst in ZAPPI_PLUGGED_IN_STATES
   if pst in ZAPPI_PLUGGED_IN_STATES:
    ok(f"Car IS plugged in — pst={pst} ({label})", out)
   else:
    warn(f"Car NOT plugged in — pst={pst} ({label})", out)
    warn("Dispatch check will be SKIPPED", out)
   info(f"Charge power (ccp): {z.get('ccp', 'n/a')}W", out)
   info(f"Charge added (che): {z.get('che', 'n/a')} kWh", out)
   info(f"Mode (zmo): {z.get('zmo', 'n/a')}", out)
  else:
   err("No Zappi found — check ZAPPI_SERIAL in config.json", out)
   results["zappi_pst"] = None
   results["car_plugged_in"] = False
 except Exception as e:
  err(f"Zappi API call failed: {e}", out)
  results["zappi_pst"] = None
  results["car_plugged_in"] = False

 # 3. Free sessions
 hdr("3. Octopus Free Electricity Sessions", out)
 token = get_kraken_token()
 if not token:
  err("Could not obtain Kraken JWT — check OCTOPUS_API_KEY", out)
  results["token_ok"] = False
 else:
  ok("Kraken JWT obtained", out)
  results["token_ok"] = True

 free_sessions = fetch_free_sessions(token) if token else []
 n = now_utc()
 if not free_sessions:
  warn("No free sessions returned from API", out)
 else:
  info(f"{len(free_sessions)} session(s) found:", out)
  for s in free_sessions:
   start = parse_iso(s["start"])
   end = parse_iso(s["end"])
   active = start <= n <= end
   prefix = " → ACTIVE NOW" if active else " "
   info(f"{prefix} {s['name']}", out)
   info(f"    Start: {fmt_local(start)}", out)
   info(f"    End:   {fmt_local(end)}", out)

 active_session = next(
  (s for s in free_sessions if parse_iso(s["start"]) <= n <= parse_iso(s["end"])),
  None
 )
 results["active_free_session"] = active_session
 if active_session:
  ok(f"FREE SESSION ACTIVE: {active_session['name']}", out)
 else:
  info("No free session active right now", out)

 # 4. Dispatches
 hdr("4. Octopus Intelligent Go dispatches", out)
 if not results.get("car_plugged_in"):
  warn("Skipping dispatch check — car not plugged in", out)
  warn("If car IS connected but pst shows disconnected, check ZAPPI_SERIAL", out)
  results["dispatch_active"] = False
  results["dispatches"] = []
 elif not token:
  err("Skipping dispatch check — no Kraken token", out)
  results["dispatch_active"] = False
  results["dispatches"] = []
 else:
  dispatches = get_all_dispatches()
  results["dispatches"] = dispatches
  info(f"{len(dispatches)} planned dispatch(es):", out)
  if not dispatches:
   warn("No planned dispatches — Octopus has not scheduled extra cheap slots yet", out)
   warn("This is normal shortly after plugging in. Octopus usually plans ahead.", out)
  else:
   for d in dispatches:
    start = parse_iso(d["startDt"])
    end = parse_iso(d["endDt"])
    active = start <= n <= end
    mins_away = (start - n).total_seconds() / 60
    if active:
     info(f" → ACTIVE NOW           {fmt_local(start)} -> {fmt_local(end)}", out)
    elif start > n:
     info(f" → in {mins_away:5.0f} min      {fmt_local(start)} -> {fmt_local(end)}", out)
    else:
     info(f" → PAST                 {fmt_local(start)} -> {fmt_local(end)}", out)

  active_dispatch = any(
   parse_iso(d["startDt"]) <= n <= parse_iso(d["endDt"])
   for d in dispatches
  )
  results["dispatch_active"] = active_dispatch
  if active_dispatch:
   ok("A dispatch IS active right now", out)
  else:
   info("No dispatch active right now", out)

 # 5. Unit rate
 hdr("5. Octopus current unit rate", out)
 tariff_result = None
 try:
  import requests as req
  r = req.get(
   f"https://api.octopus.energy/v1/accounts/{OCTOPUS_ACCOUNT}/",
   auth=(OCTOPUS_API_KEY, ""), timeout=10,
  )
  r.raise_for_status()
  n_iso = utc_now_iso()
  for prop in r.json().get("properties", []):
   for ep in prop.get("electricity_meter_points", []):
    for agreement in ep.get("agreements", []):
     valid_to = agreement.get("valid_to")
     tariff_code = agreement.get("tariff_code", "")
     if valid_to is not None and valid_to <= n_iso:
      continue
     if is_export_tariff(tariff_code):
      continue
     if tariff_code:
      parts = tariff_code.split("-")
      product_code = "-".join(parts[2:-1])
      tariff_result = (product_code, tariff_code)
      break
 except Exception as e:
  err(f"Account/tariff fetch failed: {e}", out)

 if tariff_result:
  product_code, tariff_code = tariff_result
  ok(f"Import tariff: {tariff_code}", out)
  period_from, period_to = current_utc_slot()
  info(f"Querying slot: {period_from} -> {period_to}", out)
  try:
   import requests as req
   url = (
    f"https://api.octopus.energy/v1/products/{product_code}/"
    f"electricity-tariffs/{tariff_code}/standard-unit-rates/"
    f"?period_from={period_from}&period_to={period_to}"
   )
   r = req.get(url, auth=(OCTOPUS_API_KEY, ""), timeout=10)
   r.raise_for_status()
   rate_results = r.json().get("results", [])
   if rate_results:
    rate = float(rate_results[0]["value_inc_vat"])
    results["rate"] = rate
    if rate < CHEAP_THRESHOLD_P:
     ok(f"Rate: {rate:.2f}p/kWh — BELOW threshold ({CHEAP_THRESHOLD_P}p) -> cheap", out)
    else:
     info(f"Rate: {rate:.2f}p/kWh — above threshold ({CHEAP_THRESHOLD_P}p) -> expensive", out)
    info("Note: on Intelligent Go the rate may not drop during dispatch slots.", out)
    info("Dispatches are the signal; the rate check is only the fallback.", out)
   else:
    warn("No rate data for this slot", out)
    results["rate"] = None
  except Exception as e:
   err(f"Unit rate fetch failed: {e}", out)
   results["rate"] = None
 else:
  err("Could not determine import tariff", out)
  results["rate"] = None

 # 6. Decision simulation
 hdr("6. Decision simulation (no FoxESS write)", out)
 if results.get("in_offpeak"):
  decision, reason, mode = "CHEAP", "fixed_offpeak_window", "ForceCharge"
 elif results.get("active_free_session"):
  decision = "CHEAP (FREE)"
  reason = f"free_electricity_session: {results['active_free_session']['name']}"
  mode = "ForceCharge"
 elif results.get("car_plugged_in") and results.get("dispatch_active"):
  decision, reason, mode = "CHEAP", "intelligent_dispatch", "ForceCharge"
 else:
  rate = results.get("rate")
  if rate is not None:
   if rate < CHEAP_THRESHOLD_P:
    decision, reason, mode = "CHEAP", f"rate_{rate:.1f}p < threshold", "ForceCharge"
   else:
    decision, reason, mode = "EXPENSIVE", f"rate_{rate:.1f}p > threshold", "SelfUse"
  else:
   decision, reason, mode = "EXPENSIVE (default)", "rate_unknown", "SelfUse"

 summary = f"\n Decision : {decision}\n Reason   : {reason}\n FoxESS   : would set daytime to {mode}\n"
 print(summary)
 if out:
  out.write(summary)

 # 7. Systemd timer
 hdr("7. Systemd scheduler timer status", out)
 try:
  result = subprocess.run(
   ["systemctl", "show", "energy-scheduler.timer",
    "--property=ActiveState,LastTriggerUSec,NextElapseUSecRealtime"],
   capture_output=True, text=True, timeout=5,
  )
  if result.returncode == 0:
   for line in result.stdout.strip().splitlines():
    info(line, out)
  else:
   warn("Could not read timer status", out)
 except Exception as e:
  warn(f"systemctl check skipped: {e}", out)

 # 8. Telegram
 hdr("8. Telegram configuration", out)
 if telegram_configured():
  ok(f"Telegram configured (chat_id: {TG_CHAT_ID})", out)
  info("notify_mode_change:  " + str(TG_NOTIFY_MODE_CHANGE), out)
  info("notify_foxess_error: " + str(TG_NOTIFY_FOXESS_ERR), out)
  info("notify_free_session: " + str(TG_NOTIFY_FREE), out)
  send_telegram(
   f"<b>Energy Manager:</b> Test Message for Diagnostic\n"
  )
 else:
  warn("Telegram not configured — no notifications will be sent", out)
  warn("Fill in telegram.bot_token and telegram.chat_id in config.json", out)

 # 9. State file
 hdr("9. Last known state (state.json)", out)
 state = load_state()
 if state:
  info(f"Last run    : {state.get('last_run', 'never')}", out)
  info(f"Last reason : {state.get('reason', '-')}", out)
  info(f"Last min_soc: {state.get('min_soc', '-')}%", out)
  info(f"FoxESS OK   : {state.get('foxess_ok', '-')}", out)
  info(f"Override    : {state.get('override', 'none')}", out)
 else:
  warn("state.json is empty — script may not have run yet", out)

 footer = f"\n{'*' * 60}\n Diagnostic complete. No changes were made to FoxESS.\n{'*' * 60}\n"
 print(footer)
 if out:
  out.write(footer)

# =============================================================================
# WEB DASHBOARD
# =============================================================================

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
 <meta charset="UTF-8">
 <meta name="viewport" content="width=device-width, initial-scale=1">
 <meta http-equiv="refresh" content="60">
 <title>Energy Manager</title>
 <style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
   font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
   background: #f0f2f5; color: #1a1a2e; padding: 1rem;
   max-width: 520px; margin: 0 auto;
  }
  h1 { font-size: 1.3rem; margin: 1rem 0 0.75rem; }
  .card {
   background: #fff; border-radius: 12px; padding: 1rem 1.25rem;
   margin-bottom: 1rem; box-shadow: 0 1px 4px rgba(0,0,0,.08);
  }
  .status-cheap { border-left: 5px solid #2e7d32; }
  .status-expensive { border-left: 5px solid #e65100; }
  .status-override { border-left: 5px solid #1565c0; }
  .status-free { border-left: 5px solid #6a1b9a; }
  .badge {
   display: inline-block; padding: 0.2rem 0.6rem;
   border-radius: 999px; font-size: 0.78rem; font-weight: 600;
  }
  .badge-cheap { background: #e8f5e9; color: #2e7d32; }
  .badge-expensive { background: #fff3e0; color: #e65100; }
  .badge-override { background: #e3f2fd; color: #1565c0; }
  .badge-free { background: #f3e5f5; color: #6a1b9a; }
  .mode-label { font-size: 1.5rem; font-weight: 700; margin: 0.4rem 0; }
  .meta { font-size: 0.85rem; color: #555; line-height: 1.9; }
  .section-title { font-size: 0.75rem; text-transform: uppercase;
                   letter-spacing: .05em; color: #888; margin-bottom: 0.5rem; }
  .btn-row { display: flex; gap: 0.5rem; flex-wrap: wrap; }
  button {
   flex: 1; min-width: 120px; padding: 0.65rem 0.5rem;
   border: none; border-radius: 8px; font-size: 0.9rem;
   font-weight: 600; cursor: pointer; transition: opacity .15s;
  }
  button:hover { opacity: 0.85; }
  .btn-charge { background: #2e7d32; color: #fff; }
  .btn-normal { background: #e65100; color: #fff; }
  .btn-clear { background: #1565c0; color: #fff; }
  .btn-run { background: #4a148c; color: #fff; width: 100%; margin-top: 0.5rem; }
  .btn-diag { background: #37474f; color: #fff; width: 100%; margin-top: 0.5rem; }
  .session-pill {
   display: inline-block; background: #f3e5f5; color: #6a1b9a;
   border-radius: 8px; padding: 0.4rem 0.75rem; font-size: 0.82rem;
   margin-top: 0.4rem; font-weight: 500; width: 100%;
  }
  pre {
   font-size: 0.72rem; line-height: 1.5; overflow-x: auto;
   background: #f7f8fa; border-radius: 6px; padding: 0.75rem;
   white-space: pre-wrap; word-break: break-all;
  }
  .ok { color: #2e7d32; }
  .err { color: #c62828; }
  .car-yes { color: #2e7d32; font-weight: 600; }
  .car-no { color: #888; }
  .refresh-note { font-size: 0.75rem; color: #aaa; text-align: right;
                  margin-top: -0.5rem; margin-bottom: 0.5rem; }
  .diag-age { font-size: 0.75rem; color: #888; margin-bottom: 0.4rem; }
 </style>
</head>
<body>
 <h1>Energy Manager</h1>
 <p class="refresh-note">Auto-refreshes every 60s</p>

 {%- set override = state.get("override") %}
 {%- set is_cheap = state.get("is_cheap") %}
 {%- set reason = state.get("reason", "") %}
 {%- set is_free = reason.startswith("free_electricity_session") if reason else false %}

 {%- if override %}
  {%- set cls = "status-override" %}{%- set badge_cls = "badge-override" %}
 {%- elif is_free %}
  {%- set cls = "status-free" %}{%- set badge_cls = "badge-free" %}
 {%- elif is_cheap %}
  {%- set cls = "status-cheap" %}{%- set badge_cls = "badge-cheap" %}
 {%- else %}
  {%- set cls = "status-expensive" %}{%- set badge_cls = "badge-expensive" %}
 {%- endif %}

 <div class="card {{ cls }}">
  <div class="section-title">Current Status</div>
  <div class="mode-label">
   {% if override %}Manual Override
   {% elif is_free %}Free Electricity Session
   {% elif is_cheap %}Cheap - Charging
   {% else %}Expensive - Saving
   {% endif %}
  </div>
  <span class="badge {{ badge_cls }}">
   {% if override %}{{ override | upper }}
   {% elif is_free %}FREE
   {% elif is_cheap %}CHEAP
   {% else %}EXPENSIVE
   {% endif %}
  </span>
  <div class="meta" style="margin-top:0.75rem">
   <div>Reason: <b>{{ state.get("reason", "-") }}</b></div>
   <div>MinSoc on grid: <b>{{ state.get("min_soc", "?") }}%</b></div>
   {%- set car = state.get("car_plugged_in") %}
   <div>Car plugged in:
    {% if car is none %}<span class="car-no">n/a</span>
    {% elif car %}<span class="car-yes">Yes</span>
    {% else %}<span class="car-no">No</span>{% endif %}
   </div>
   <div>FoxESS:
    <span class="{{ 'ok' if state.get('foxess_ok') else 'err' }}">
     {{ "OK" if state.get("foxess_ok") else "Error" }}</span>
   </div>
   <div>Last run: <b>{{ state.get("last_run", "never") }}</b></div>
  </div>
 </div>

 {%- set active = state.get("active_free_session") %}
 {%- set upcoming = state.get("next_free_session") %}
 {% if active or upcoming %}
 <div class="card" style="border-left:5px solid #6a1b9a;">
  <div class="section-title">Octopus Free Electricity</div>
  {% if active %}
   <div style="font-weight:700;color:#6a1b9a;margin-bottom:0.3rem">Active Now</div>
   <div class="session-pill">{{ active.get("name","Session") }}<br>
   {{ active.get("start","") }} &rarr; {{ active.get("end","") }}</div>
  {% endif %}
  {% if upcoming %}
   <div style="font-weight:600;margin-top:0.6rem;margin-bottom:0.3rem;color:#555">Next Session</div>
   <div class="session-pill">{{ upcoming.get("name","Session") }}<br>
   {{ upcoming.get("start","") }} &rarr; {{ upcoming.get("end","") }}</div>
  {% endif %}
 </div>
 {% endif %}

 <div class="card">
  <div class="section-title">Manual Override</div>
  <form method="post" action="/override">
   <div class="btn-row">
    <button class="btn-charge" name="mode" value="charge">Force Charge</button>
    <button class="btn-normal" name="mode" value="normal">Force Normal</button>
    <button class="btn-clear" name="mode" value="clear">Clear Override</button>
   </div>
  </form>
  <form method="post" action="/run">
   <button class="btn-run">Run Now</button>
  </form>
  <form method="post" action="/diagnose-run">
   <button class="btn-diag">Run Diagnostics</button>
  </form>
  <div class="meta" style="margin-top:0.6rem">
   Overrides persist until cleared. Diagnostics take ~10 seconds to complete.
  </div>
 </div>

 {% if diag_output %}
 <div class="card">
  <div class="section-title">Diagnostic Output</div>
  {% if diag_age %}
   <div class="diag-age">Last run: {{ diag_age }}</div>
  {% endif %}
  <pre>{{ diag_output }}</pre>
 </div>
 {% endif %}

 <div class="card">
  <div class="section-title">Recent Log</div>
  <pre>{{ log_tail }}</pre>
 </div>
</body>
</html>
"""


def require_auth(f):
 @functools.wraps(f)
 def decorated(*args, **kwargs):
  if not DASHBOARD_PASSWORD:
   return f(*args, **kwargs)
  from flask import request, Response
  auth = request.authorization
  if auth and auth.username == "energy" and auth.password == DASHBOARD_PASSWORD:
   return f(*args, **kwargs)
  return Response(
   "Authentication required.",
   401,
   {"WWW-Authenticate": 'Basic realm="Energy Manager"'},
  )
 return decorated


def start_dashboard():
 try:
  from flask import Flask, request, redirect, render_template_string
 except ImportError:
  print("Flask not found. Run: pip3 install flask")
  sys.exit(1)

 if DASHBOARD_PASSWORD:
  log.info("Dashboard: password protection enabled")
 else:
  log.warning("Dashboard: no password set — set dashboard.password in config.json")

 app = Flask(__name__)

 def _render(state, log_tail, diag_output=None, diag_age=None):
  return render_template_string(
   DASHBOARD_HTML,
   state=state,
   log_tail=log_tail,
   diag_output=diag_output,
   diag_age=diag_age,
  )

 def _log_tail():
  try:
   lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
   return "\n".join(lines[-25:])
  except Exception:
   return "(log not yet available)"

 def _diag_content():
  """Return (text, age_string) for most recent diagnose output, or (None, None)."""
  try:
   mtime = DIAGNOSE_FILE.stat().st_mtime
   text = DIAGNOSE_FILE.read_text(encoding="utf-8")
   age = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
   return text, age
  except Exception:
   return None, None

 @app.route("/")
 @require_auth
 def index():
  diag_text, diag_age = _diag_content()
  return _render(load_state(), _log_tail(), diag_text, diag_age)

 @app.route("/override", methods=["POST"])
 @require_auth
 def set_override():
  mode = request.form.get("mode", "")
  if mode in ("charge", "normal"):
   save_state({"override": mode})
   log.info(f"Dashboard: override set to '{mode}'")
  elif mode == "clear":
   save_state({"override": None})
   log.info("Dashboard: override cleared")
  return redirect("/")

 @app.route("/run", methods=["POST"])
 @require_auth
 def run_now():
  subprocess.Popen([sys.executable, str(Path(__file__).resolve())])
  log.info("Dashboard: manual run triggered")
  return redirect("/")

 @app.route("/diagnose-run", methods=["POST"])
 @require_auth
 def diagnose_run():
  """
  Run diagnostics synchronously (blocks for ~10s) then redirect back.
  Output is saved to diagnose_output.txt and shown on the dashboard.
  """
  log.info("Dashboard: running diagnostics")
  buf = io.StringIO()
  diagnose(out=buf)
  DIAGNOSE_FILE.write_text(buf.getvalue(), encoding="utf-8")
  return redirect("/")

 log.info(f"Dashboard starting on http://{DASHBOARD_HOST}:{DASHBOARD_PORT}")
 app.run(host=DASHBOARD_HOST, port=DASHBOARD_PORT, debug=False, use_reloader=False)

# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
 parser = argparse.ArgumentParser(description="Energy Manager -- FoxESS + Octopus + Zappi")
 parser.add_argument("--charge", action="store_true", help="Set override: force charge")
 parser.add_argument("--normal", action="store_true", help="Set override: force normal")
 parser.add_argument("--clear", action="store_true", help="Clear manual override")
 parser.add_argument("--dashboard", action="store_true", help="Start web dashboard")
 parser.add_argument("--status", action="store_true", help="Print current state")
 parser.add_argument("--get-schedule", action="store_true", help="Read current FoxESS schedule")
 parser.add_argument("--diagnose", action="store_true", help="Test all APIs and simulate decision")
 args = parser.parse_args()

 setup_logging(dashboard_mode=args.dashboard)

 if args.dashboard:
  start_dashboard()
 elif args.diagnose:
  diagnose()
 elif args.status:
  print(json.dumps(load_state(), indent=2))
 elif args.get_schedule:
  print("Reading current FoxESS schedule...\n")
  result = get_foxess_schedule()
  if result:
   print(json.dumps(result, indent=2))
   print(f"\ncharge_power_watts in config.json: {CHARGE_POWER_WATTS}W")
  else:
   print("Failed to retrieve schedule.")
 elif args.charge:
  save_state({"override": "charge"})
  print("Override set to 'charge'. Takes effect on next run.")
 elif args.normal:
  save_state({"override": "normal"})
  print("Override set to 'normal'. Takes effect on next run.")
 elif args.clear:
  save_state({"override": None})
  print("Override cleared. Scheduler will resume auto mode.")
 else:
  run()