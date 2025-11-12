import os
import re
import requests
import logging
import threading
import time
import json
import sys
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from flask import Flask, jsonify, request
from dotenv import load_dotenv
from ping3 import ping

# Load environment variables
load_dotenv()

app = Flask(__name__)

# Configure logging to both file and stdout
logger = logging.getLogger()
logger.setLevel(logging.INFO)

formatter = logging.Formatter(
    "%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)

# Ensure logs directory exists
os.makedirs("logs", exist_ok=True)

# File handler
file_handler = logging.FileHandler("logs/service_monitor.log")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# Stream handler (stdout)
stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

# Load configs
LARK_WEBHOOK_URLS = [
    u.strip()
    for u in os.getenv("LARK_WEBHOOK_URLS", "").replace("\n", ",").split(",")
    if u.strip()
]
FLASK_PORT = int(os.getenv("FLASK_PORT", 5001))
MONITORING_INTERVAL = int(os.getenv("MONITORING_INTERVAL", 60))

# Load services
services_to_monitor = {}
services_env = os.getenv("SERVICES_TO_MONITOR", "")


def _parse_service_entry(entry: str):
    """
    Parse a single service definition in the form:
      name:url:method[:payload]
    Supports URLs containing ':' (e.g., http://host:port) and optional JSON payload.
    Entries may be separated by commas or newlines in the env variable.
    """
    entry = entry.strip().strip('"').strip("'")
    if not entry:
        raise ValueError("Empty service entry")

    # Match ':METHOD' (GET|POST) with optional ':{JSON}' at the end of the line
    m = re.search(r":\s*(GET|POST)\s*(?::\s*(\{.*\}))?\s*$", entry, re.IGNORECASE)
    if not m:
        raise ValueError("Missing or invalid method (expected GET or POST)")

    method = m.group(1).upper()
    payload_str = m.group(2) if m.lastindex and m.group(2) else "{}"

    # Remove the ':METHOD[:payload]' suffix to leave 'name:url'
    prefix = entry[:m.start()]
    # Split 'name:url' at the first colon only
    if ":" not in prefix:
        raise ValueError("Expected 'name:url:method' format")
    name, url = prefix.split(":", 1)
    name = name.strip()
    url = url.strip()

    # Validate payload JSON
    try:
        json.loads(payload_str)
    except json.JSONDecodeError:
        logging.error(f"Invalid JSON payload for {name}, defaulting to {{}}")
        payload_str = "{}"

    return name, {"url": url, "method": method, "payload": payload_str}


if services_env:
    # Split by comma or newline; keep non-empty entries
    raw_entries = [e for e in re.split(r"[,\n]+", services_env) if e.strip()]
    for raw in raw_entries:
        try:
            svc_name, svc_def = _parse_service_entry(raw)
            services_to_monitor[svc_name] = svc_def
        except Exception as e:
            logging.error(f"Error parsing service '{raw}': {e}")
else:
    logging.warning("SERVICES_TO_MONITOR env is empty. No services to monitor.")

monitoring_status = {"is_running": False}
# Track last known state to notify only on transitions (down & resolved)
last_state = {name: None for name in services_to_monitor}


def _host_from_url(url: str):
    try:
        return urlparse(url).hostname
    except Exception:
        return None


def _icmp_latency(host: str, timeout: float = 2.0):
    try:
        rtt = ping(host, timeout=timeout, unit="ms", privileged=False)
        return float(rtt) if rtt is not None else None
    except Exception:
        return None


def check_service_status(service):
    """
    Check reachability of a single service with detailed diagnostics.
    Returns dict:
      {
        "ok": bool,
        "status_code": int|None,
        "reason": str,               # human-readable diagnostic text
        "elapsed_ms": float|None     # HTTP elapsed time (ms)
      }
    """
    url = service["url"]
    method = service["method"].upper()
    payload = json.loads(service.get("payload", "{}") or "{}")
    host = _host_from_url(url)

    reason_parts = []
    status_code = None
    elapsed_ms = None
    reachable = False

    try:
        if method == "GET":
            # Try HEAD first (lightweight)
            head_ok = False
            try:
                r = requests.head(url, timeout=8, allow_redirects=True)
                status_code = r.status_code
                elapsed_ms = r.elapsed.total_seconds() * 1000.0
                if 200 <= r.status_code < 300:
                    reachable = True
                    head_ok = True
                elif r.status_code in (405, 501):
                    # HEAD not allowed/supported
                    pass
                else:
                    reason_parts.append(f"HEAD returned {r.status_code}")
            except requests.RequestException:
                # HEAD failed; fall back to GET below
                pass

            if not head_ok:
                r = requests.get(url, timeout=10)
                status_code = r.status_code
                elapsed_ms = r.elapsed.total_seconds() * 1000.0
                reachable = 200 <= r.status_code < 300
                if 300 <= r.status_code < 400:
                    reason_parts.append(f"redirected ({r.status_code})")
                elif 400 <= r.status_code < 500:
                    reason_parts.append(f"client error ({r.status_code})")
                elif 500 <= r.status_code < 600:
                    reason_parts.append(f"server error ({r.status_code})")
        else:
            r = requests.post(url, json=payload, timeout=12)
            status_code = r.status_code
            elapsed_ms = r.elapsed.total_seconds() * 1000.0
            reachable = 200 <= r.status_code < 300
            if not reachable:
                if 300 <= r.status_code < 400:
                    reason_parts.append(f"redirected ({r.status_code})")
                elif 400 <= r.status_code < 500:
                    reason_parts.append(f"client error ({r.status_code})")
                elif 500 <= r.status_code < 600:
                    reason_parts.append(f"server error ({r.status_code})")
    except requests.exceptions.ConnectTimeout:
        reason_parts.append("connection timed out")
    except requests.exceptions.ReadTimeout:
        reason_parts.append("read timed out")
    except requests.exceptions.SSLError:
        reason_parts.append("SSL/TLS error")
    except requests.exceptions.ConnectionError as ce:
        msg = str(ce).lower()
        if "name or service not known" in msg or "temporary failure in name resolution" in msg or "dns" in msg:
            reason_parts.append("DNS resolution failed")
        elif "connection refused" in msg:
            reason_parts.append("connection refused (service not listening)")
        elif "network is unreachable" in msg:
            reason_parts.append("network unreachable")
        else:
            reason_parts.append("network connection error")
    except Exception as e:
        reason_parts.append(f"unexpected error: {e}")

    # ICMP ping to enrich diagnostics (best-effort)
    if host:
        icmp_ms = _icmp_latency(host)
        if icmp_ms is None:
            reason_parts.append("host ICMP ping failed")
        else:
            reason_parts.append(f"host reachable via ICMP ({icmp_ms:.1f} ms)")

    reason = "; ".join(reason_parts) if reason_parts else ""
    return {"ok": reachable, "status_code": status_code, "reason": reason, "elapsed_ms": elapsed_ms}


def _code_color(sc: Optional[int], ok: bool) -> str:
    if sc is None:
        return "grey"
    if 200 <= sc < 300:
        return "blue"
    if 300 <= sc < 400:
        return "yellow"  # redirects
    if 400 <= sc < 500:
        return "orange"
    if 500 <= sc < 600:
        return "red"
    return "grey"


def build_status_card(service_name: str, ok: bool, status_code: Optional[int], elapsed_ms: Optional[float], resolved: bool, reason: Optional[str]):
    """
    Build a Lark interactive card payload containing a colored header and colored fields.
    """
    header_template = "green" if ok else "red"
    status_text = "RESOLVED" if resolved else ("UP" if ok else "DOWN")
    status_color = "green" if ok else "red"
    code_color = _code_color(status_code, ok)
    time_color = "purple" if elapsed_ms is not None else "grey"
    emoji = "🟢" if ok else "🟠"

    status_field = {
        "is_short": True,
        "text": {
            "tag": "lark_md",
            "content": f"**Status**\n<font color='{status_color}'>{status_text} {emoji}</font>",
        },
    }
    code_txt = f"{status_code}" if status_code is not None else "—"
    code_field = {
        "is_short": True,
        "text": {
            "tag": "lark_md",
            "content": f"**Response Code**\n<font color='{code_color}'>{code_txt}</font>",
        },
    }
    time_txt = f"{elapsed_ms:.0f} ms" if elapsed_ms is not None else "—"
    time_field = {
        "is_short": True,
        "text": {
            "tag": "lark_md",
            "content": f"**Response Time**\n<font color='{time_color}'>{time_txt}</font>",
        },
    }

    title_color = "blue"
    title_block = {
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": f"<font color='{title_color}'><b>{service_name}</b></font>",
        },
    }
    elements = [
        title_block,
        {"tag": "div", "fields": [status_field, code_field, time_field]},
    ]

    # Add details for incident
    if not ok and reason:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**Details**\n{reason}",
                },
            }
        )
    # Add recovery note
    if resolved:
        elements.append(
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "Previous incident resolved. Monitoring will continue.",
                    }
                ],
            }
        )

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            # Header background/stripe uses template color; title stays neutral for readability
            "title": {"tag": "plain_text", "content": f"{service_name}"},
            "template": header_template,
        },
        "elements": elements,
    }
    return card


def send_lark_card(card: dict, fallback_text: str = ""):
    """
    Send an interactive card to each configured Lark webhook URL.
    Falls back to plain text on failure.
    """
    headers = {"Content-Type": "application/json"}
    for url in LARK_WEBHOOK_URLS:
        url = url.strip()
        if not url:
            continue
        try:
            payload = {"msg_type": "interactive", "card": card}
            response = requests.post(url, json=payload, headers=headers, timeout=10)
            if response.status_code != 200:
                # Fallback text
                if fallback_text:
                    requests.post(
                        url,
                        json={"msg_type": "text", "content": {"text": fallback_text}},
                        headers=headers,
                        timeout=10,
                    )
                logging.warning(f"Lark card send returned HTTP {response.status_code} for {url}")
            else:
                logging.info(f"Lark card sent to {url}")
        except requests.exceptions.RequestException as e:
            logging.error(f"Error sending card to {url}: {e}")
            # Best-effort fallback text
            if fallback_text:
                try:
                    requests.post(
                        url,
                        json={"msg_type": "text", "content": {"text": fallback_text}},
                        headers=headers,
                        timeout=10,
                    )
                except Exception:
                    pass


def monitor_services():
    """Background loop that monitors services concurrently and logs detailed reasons."""
    logging.info("Service monitoring thread started.")
    global last_state

    while monitoring_status["is_running"]:
        if not services_to_monitor:
            logging.warning("No services configured to monitor.")
            time.sleep(MONITORING_INTERVAL)
            continue

        with ThreadPoolExecutor(max_workers=min(8, max(1, len(services_to_monitor)))) as executor:
            future_to_name = {
                executor.submit(check_service_status, service): name
                for name, service in services_to_monitor.items()
            }
            for future in as_completed(future_to_name):
                name = future_to_name[future]
                try:
                    result = future.result()
                except Exception as e:
                    logging.error(f"{name} check failed: {e}")
                    result = {"ok": False, "status_code": None, "reason": f"unexpected exception: {e}", "elapsed_ms": None}

                ok = bool(result.get("ok"))
                sc = result.get("status_code")
                elapsed_ms = result.get("elapsed_ms")
                reason_text = result.get("reason", "")

                prev = last_state.get(name)

                if ok:
                    code_text = f"{sc}" if sc is not None else "—"
                    time_text = f"{elapsed_ms:.0f} ms" if elapsed_ms is not None else "—"
                    logging.info(f"{name} is REACHABLE and RUNNING (HTTP {code_text}, {time_text}).")
                    # If previously down, send a RESOLVED card
                    if prev is False:
                        card = build_status_card(
                            service_name=name,
                            ok=True,
                            status_code=sc,
                            elapsed_ms=elapsed_ms,
                            resolved=True,
                            reason=""
                        )
                        fallback = f"{name} recovered. HTTP {sc if sc is not None else '—'}. {elapsed_ms:.0f} ms." if elapsed_ms is not None else f"{name} recovered. HTTP {sc if sc is not None else '—'}."
                        send_lark_card(card, fallback_text=fallback)
                else:
                    code_text = f"HTTP {sc}" if sc is not None else "no HTTP response"
                    logging.error(f"{name} is NOT RUNNING or NOT REACHABLE ({code_text}). Possible reasons: {reason_text}")
                    # Notify on transition to down (from up or unknown)
                    if prev is True or prev is None:
                        card = build_status_card(
                            service_name=name,
                            ok=False,
                            status_code=sc,
                            elapsed_ms=elapsed_ms,
                            resolved=False,
                            reason=reason_text
                        )
                        fallback = f"{name} issue: {code_text}. Details: {reason_text}"
                        send_lark_card(card, fallback_text=fallback)

                last_state[name] = ok

        time.sleep(MONITORING_INTERVAL)


def start_monitoring_background():
    """Start monitoring in a background thread if not already running."""
    if not monitoring_status["is_running"]:
        monitoring_status["is_running"] = True
        threading.Thread(target=monitor_services, daemon=True).start()
        logging.info("Monitoring started automatically.")


# Flask routes
@app.route("/ping", methods=["GET"])
def ping_route():
    return jsonify({"message": "Service monitor is alive"}), 200


@app.route("/status", methods=["GET"])
def get_status():
    return jsonify(
        {
            "monitoring": monitoring_status["is_running"],
            "services": services_to_monitor,
            "last_state": last_state,
        }
    )


@app.route("/start-monitoring", methods=["POST"])
def start_monitoring():
    if monitoring_status["is_running"]:
        return jsonify({"message": "Monitoring already running."}), 400
    start_monitoring_background()
    return jsonify({"message": "Monitoring started."})


@app.route("/stop-monitoring", methods=["POST"])
def stop_monitoring():
    monitoring_status["is_running"] = False
    return jsonify({"message": "Monitoring stopped."})


@app.route("/update-services", methods=["POST"])
def update_services():
    data = request.json
    if not isinstance(data.get("services"), dict):
        return jsonify({"error": "Invalid format. Send a dict of services."}), 400
    services_to_monitor.clear()
    services_to_monitor.update(data["services"])
    # Reset state tracking for updated services
    global last_state
    last_state = {name: None for name in services_to_monitor}
    return jsonify({"message": "Services updated.", "services": services_to_monitor})


if __name__ == "__main__":
    logging.info("Starting Flask server...")
    # Auto-start monitoring when container launches
    start_monitoring_background()
    app.run(host="0.0.0.0", port=FLASK_PORT)