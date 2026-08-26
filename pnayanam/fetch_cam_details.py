import os
import time
import json
import requests
import configparser
import threading
from datetime import datetime

from pnayanam.logger import Logger

restart_event = threading.Event()

CONFIG_FILE = "/var/pnayanam/pnayanam.ini"  # change if needed

camera_details_cache = []

config_out = configparser.ConfigParser()
config_out.read(CONFIG_FILE)
cycle_time = config_out.getint("cycle_time", "time", fallback=30) 

protocol = config_out.get("gui_ip_address", "protocol", fallback="http")
gui_ip = config_out.get("gui_ip_address", "ip_address", fallback="127.0.0.1")
port = config_out.get("gui_ip_address", "port", fallback="8000")

CAMERA_API_URL = f"{protocol}://{gui_ip}:{port}/api/cameras"

logger = Logger("fetch_cam_details")


def fetch_camera_details():
    """
    Fetch camera details once from the external API.
    Returns parsed JSON on success, else None.
    """
    try:
        response = requests.get(CAMERA_API_URL, timeout=10)
        response.raise_for_status()

        data = response.json()

        if data is None:
            raise ValueError("Camera API returned None.")

        if isinstance(data, (list, dict)) and len(data) == 0:
            raise ValueError("Camera API returned empty data structure.")

        return data

    except requests.exceptions.Timeout:
        logger.info("[CAMERA MONITOR] Request timed out while fetching camera details.")
    except requests.exceptions.ConnectionError as e:
        logger.info(f"[CAMERA MONITOR] Connection error: {e}")
    except requests.exceptions.RequestException as e:
        logger.info(f"[CAMERA MONITOR] HTTP error: {e}")
    except ValueError as e:
        logger.info(f"[CAMERA MONITOR] Validation error: {e}")
    except Exception as e:
        logger.info(f"[CAMERA MONITOR] Unexpected error: {e}")

    return None


def print_camera_details(camera_data):
    if not camera_data:
        logger.info("No camera details found.")
        logger.info("==========================\n")
        return
    logger.info("==========================\n")


def get_camera_details():
    return camera_details_cache


def load_config():
    config = configparser.ConfigParser()
    config.optionxform = str  # preserve key case
    config.read(CONFIG_FILE)
    return config

def restart_application():
    """
    Signal application restart upon configuration modification.
    """
    logger.info("[CAMERA MONITOR] Config changed. Restarting application...")
    restart_event.set()


def sync_camera_config(camera_list):
    config = load_config()
    changed = False

    SCN_SECTION = "camera_scn"

    if SCN_SECTION not in config:
        config[SCN_SECTION] = {}
        changed = True

    existing_scns = []
    if config.has_option(SCN_SECTION, "scns"):
        existing_scns = [
            x.strip()
            for x in config.get(SCN_SECTION, "scns").split(",")
            if x.strip()
        ]

    active_scns_from_payload = []

    for cam in camera_list:
        scn = str(cam.get("camera_scn", "")).strip()
        if not scn:
            continue

        site_name = str(cam.get("site_name", "")).strip()
        name = str(cam.get("name", "")).strip()
        status = str(cam.get("status", "")).strip().lower()

        # Parse is_process boolean safely
        raw_is_process = cam.get("is_process", False)
        if isinstance(raw_is_process, bool):
            is_process = raw_is_process
        else:
            is_process = str(raw_is_process).lower() == "true"

        # Resolve stream path & detect solar cameras
        stream_url = cam.get("stream_url")
        SOLAR_BASE_PATH = "/var/pnayanam/cctv/"
        is_solar_cam = False

        if stream_url is None or str(stream_url).strip().lower() in ("", "null", "none"):
            is_solar_cam = True
            camera_folder = name
            stream_url = os.path.join(SOLAR_BASE_PATH, camera_folder)
            logger.info(
                f"[CAMERA MONITOR] Solar camera detected ({scn}) -> {stream_url}"
            )
        else:
            stream_url = str(stream_url).strip()

        # Monitoring Time evaluation for multiple intervals
        monitoring_times = cam.get("monitoring_time") or []
        between_time_range = False
        active_start_time = "00:00"
        active_end_time = "23:59"
        now = datetime.now().time()

        for window in monitoring_times:
            try:
                s_str = window.get("start", "00:00").strip()
                e_str = window.get("end", "23:59").strip()

                s_parse = s_str + ":00" if len(s_str) == 5 else s_str
                e_parse = e_str + ":00" if len(e_str) == 5 else e_str

                start = datetime.strptime(s_parse, "%H:%M:%S").time()
                end = datetime.strptime(e_parse, "%H:%M:%S").time()

                if start <= end:
                    in_window = start <= now <= end
                else:  # Handles overnight schedules (e.g., 22:00 to 06:00)
                    in_window = now >= start or now <= end

                if in_window:
                    between_time_range = True
                    active_start_time = s_str
                    active_end_time = e_str
                    break  # Stop at the first matching active interval
            except Exception:
                continue

        # Rely ONLY on is_process and current active window match
        is_add = is_process and between_time_range

        if is_add:
            active_scns_from_payload.append(scn)

        # Fallback values if camera is not inside any active window currently
        if not between_time_range and monitoring_times:
            start_time = monitoring_times[0].get("start", "00:00")
            end_time = monitoring_times[-1].get("end", "23:59")
        else:
            start_time = active_start_time
            end_time = active_end_time

        # Configuration dictionary payload
        camera_config_data = {
            "site_name": f'"{site_name}"',
            "name": f'"{name}"',
            "stream_url": stream_url,
            "is_process": str(is_process),
            "is_solar_cam": str(is_solar_cam),
            "status": status,
            "debug": "True",
            "start_time": start_time,
            "end_time": end_time,
        }

        if not config.has_section(scn):
            config[scn] = camera_config_data
            config[scn]["result_storage_path"] = f"{scn.lower()}_result.mp4"
            changed = True
            logger.info(f"[CAMERA MONITOR] Added camera: {scn}")
        else:
            for key, value in camera_config_data.items():
                old = config.get(scn, key, fallback="").strip()
                if old != str(value):
                    config[scn][key] = str(value)
                    changed = True
                    logger.info(f"[CAMERA MONITOR] Updated {key} for {scn}")

        logger.info(
            f"[{scn}] is_process={is_process}, status={status}, "
            f"in_time={between_time_range}, added={is_add}"
        )

    # Active SCN list update
    new_scns = ",".join(active_scns_from_payload)
    old_scns = ",".join(existing_scns)
    if old_scns != new_scns:
        config[SCN_SECTION]["scns"] = new_scns
        changed = True
        logger.info(f"[CAMERA MONITOR] Updated active SCNs: {new_scns}")

    # Write changes if modified
    if changed:
        with open(CONFIG_FILE, "w") as f:
            config.write(f)
        logger.info("[CAMERA MONITOR] Config updated.")
        restart_application()
    else:
        logger.info("[CAMERA MONITOR] No config changes.")


def camera_monitor():
    """
    Continuously fetch camera details every cycle_time seconds.
    """
    global camera_details_cache

    logger.info("[CAMERA MONITOR] thread started")

    while True:
        try:
            data = fetch_camera_details()

            if data is not None:
                camera_details_cache = data
                if isinstance(data, list):
                    sync_camera_config(data)
                else:
                    logger.info("[CAMERA MONITOR] Unexpected payload format. Expected list.")
            else:
                logger.info("[CAMERA MONITOR] No data collected in this cycle.")

        except Exception as e:
            logger.info(f"[CAMERA MONITOR] Error: {e}")

        time.sleep(cycle_time)