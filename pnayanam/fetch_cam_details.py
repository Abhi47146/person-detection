import os
import time
import json
import requests
import configparser

from pnayanam.logger import Logger

import threading
from datetime import datetime

restart_event = threading.Event()

CONFIG_FILE = "/var/pnayanam/pnayanam.ini"   # change if needed

camera_details_cache = []

config_out = configparser.ConfigParser()
config_out.read("/var/pnayanam/pnayanam.ini")
cycle_time = config_out.getint("cycle_time", "time") 

protocol = config_out.get(
    "gui_ip_address",
    "protocol",
    fallback="http"
)

gui_ip = config_out.get(
    "gui_ip_address",
    "ip_address",
    fallback="127.0.0.1"
)

port = config_out.get(
    "gui_ip_address",
    "port",
    fallback="8000"
)

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

        if isinstance(data, list) and len(data) == 0:
            raise ValueError("Camera API returned an empty list. No camera data collected.")

        if isinstance(data, dict) and len(data) == 0:
            raise ValueError("Camera API returned an empty dict. No camera data collected.")

        return data


    except requests.exceptions.Timeout:
        logger.info("[CAMERA MONITOR] Request timed out while fetching camera details.")

    except requests.exceptions.ConnectionError as e:
        logger.info(f"[CAMERA MONITOR] Connection error: {e}")

    except requests.exceptions.RequestException as e:
        logger.info(f"[CAMERA MONITOR] HTTP error: {e}")

    except ValueError as e:
        logger.info(f"[CAMERA MONITOR] No camera data collected: {e}")

    except Exception as e:
        logger.info(f"[CAMERA MONITOR] Unexpected error: {e}")

    return None


def print_camera_details(camera_data):
    # logger.info("\n===== CAMERA DETAILS =====")
    if not camera_data:
        logger.info("No camera details found.")
        logger.info("==========================\n")
        return

    # logger.info(json.dumps(camera_data, indent=4))
    logger.info("==========================\n")


def get_camera_details():
    return camera_details_cache


def load_config():
    config = configparser.ConfigParser()
    config.optionxform = str   # preserve key case if needed
    config.read(CONFIG_FILE)
    return config


def restart_application():
    """
    Restart the current Python process.
    Since this monitor runs inside the same process as run.py,
    execv will restart the whole application with updated config.
    """
    logger.info("[CAMERA MONITOR] Config changed. Restarting application...")
    # os.execv(sys.executable, [sys.executable] + sys.argv)
    restart_event.set()


def sync_camera_config(camera_list):

    config = load_config()
    changed = False

    # Change this if your section name is different
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

        # API fields
        site_name = str(cam.get("site_name", "")).strip()
        name = str(cam.get("name", "")).strip()

        # Resolve stream path
        stream_url = cam.get("stream_url")
        SOLAR_BASE_PATH = "/home/itspe/Desktop/p_nayanam/solar_cam/cctv/"

        # Assume not a solar camera
        is_solar_cam = False

        # Treat None, "null", "", and "None" as solar cameras
        if ( stream_url is None or str(stream_url).strip().lower() in ("", "null", "none")):
            is_solar_cam = True

            camera_folder = ( str(cam.get("name", "")) .strip())
            stream_url = os.path.join(SOLAR_BASE_PATH, camera_folder)
            logger.info(
                f"[CAMERA MONITOR] Solar camera detected "
                f"({scn}) -> {stream_url}"
            )
        else:
            stream_url = str(stream_url).strip()

      

        status = str(cam.get("status", "")).strip().lower()

        raw_is_process = cam.get("is_process", False)

        if isinstance(raw_is_process, bool):
            is_process = raw_is_process
        else:
            is_process = str(raw_is_process).lower() == "true"

        monitoring_start_time = str(
            cam.get("monitoring_start_time", "00:00:00")
        ).strip()

        monitoring_end_time = str(
            cam.get("monitoring_end_time", "23:59:59")
        ).strip()

        # Store only HH:MM
        start_time = monitoring_start_time[:5]
        end_time = monitoring_end_time[:5]

        # Time window check
        try:
            start = datetime.strptime(
                monitoring_start_time,
                "%H:%M:%S"
            ).time()

            end = datetime.strptime(
                monitoring_end_time,
                "%H:%M:%S"
            ).time()

            now = datetime.now().time()

            if start <= end:
                between_time_range = start <= now <= end
            else:
                # Overnight schedule
                between_time_range = (
                    now >= start or now <= end
                )
        except Exception:
            between_time_range = False

        is_add = (
            is_process
            and between_time_range
        )

        if is_add:
            active_scns_from_payload.append(scn)

        # Create new section
        if not config.has_section(scn):
            config[scn] = {
                "site_name": f'"{site_name}"',
                "name": f'"{name}"',
                "stream_url": stream_url,
                "is_process": str(is_process),
                "is_solar_cam": str(is_solar_cam),
                "debug": "True",
                "result_storage_path": f"{scn.lower()}_result.mp4",
                "start_time": start_time,
                "end_time": end_time,
            }
            changed = True
            logger.info(f"[CAMERA MONITOR] Added camera: {scn}")

        else:
            updates = {
                "site_name": f'"{site_name}"',
                "name": f'"{name}"',
                "stream_url": stream_url,
                "is_process": str(is_process),
                "is_solar_cam": str(is_solar_cam),
                "start_time": start_time,
                "end_time": end_time,
            }
            for key, value in updates.items():
                old = config.get(scn, key, fallback="").strip()
                if old != value:
                    config[scn][key] = value
                    changed = True
                    logger.info(f"[CAMERA MONITOR] Updated {key} for {scn}")
        logger.info(
            f"[{scn}] "
            f"is_process={is_process}, "
            f"status={status}, "
            f"in_time={between_time_range}, "
            f"added={is_add}"
        )

    # Update active SCN list
    new_scns = ",".join(active_scns_from_payload)
    old_scns = ",".join(existing_scns)
    if old_scns != new_scns:
        config[SCN_SECTION]["scns"] = new_scns
        changed = True
        logger.info(
            f"[CAMERA MONITOR] Updated active SCNs: {new_scns}"
        )

    # Save
    if changed:
        with open(CONFIG_FILE, "w") as f:
            config.write(f)
        logger.info("[CAMERA MONITOR] Config updated.")
        restart_application()
    else:
        logger.info("[CAMERA MONITOR] No config changes.")


def camera_monitor():
    """
    Continuously fetch camera details every 30 seconds.
    If config changes based on payload, update config and restart app.
    """
    global camera_details_cache

    logger.info("[CAMERA MONITOR] thread started")

    while True:
        try:
            data = fetch_camera_details()

            if data is not None:
                camera_details_cache = data
                # logger.info(camera_details_cache)

                if isinstance(data, list):
                    sync_camera_config(data)
                else:
                    logger.info("[CAMERA MONITOR] Unexpected payload format. Expected list of cameras.")
            else:
                logger.info("[CAMERA MONITOR] No data collected in this cycle.")

        except Exception as e:
            logger.info(f"[CAMERA MONITOR] Error: {e}")

        time.sleep(cycle_time)