import os
import time
import json
import requests
import configparser
import threading
from datetime import datetime

from pnayanam.logger import Logger

restart_event = threading.Event()

CONFIG_FILE = "/var/pnayanam/pnayanam.ini"
camera_details_cache = []

config_out = configparser.ConfigParser()
config_out.read(CONFIG_FILE)
cycle_time = config_out.getint("cycle_time", "time", fallback=30) 

protocol = config_out.get("gui_ip_address", "protocol", fallback="http")
gui_ip = config_out.get("gui_ip_address", "ip_address", fallback="127.0.0.1")
port = config_out.get("gui_ip_address", "port", fallback="8000")

CAMERA_API_URL = f"{protocol}://{gui_ip}:{port}/api/cameras"
SITES_API_URL = f"{protocol}://{gui_ip}:{port}/api/sites/active"

logger = Logger("fetch_cam_details")


def fetch_camera_details():
    try:
        response = requests.get(CAMERA_API_URL, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.info(f"[CAMERA MONITOR] Error fetching cameras: {e}")
        return None


def fetch_site_details():
    try:
        response = requests.get(SITES_API_URL, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.info(f"[CAMERA MONITOR] Error fetching sites: {e}")
        return None


def load_config():
    config = configparser.ConfigParser()
    config.optionxform = str
    config.read(CONFIG_FILE)
    return config


def clean_val(val):
    if val is None:
        return ""
    return str(val).strip().strip('"').strip("'")


def sync_camera_and_site_config(camera_list, site_list):
    config = load_config()
    requires_restart = False

    # 1. SYNC SITES SECTION
    if site_list:
        solar_sites = [s for s in site_list if s.get("email") and s.get("password")]
        site_names = [s["name"] for s in solar_sites if s.get("name")]
        
        if "sites" not in config:
            config["sites"] = {}
            requires_restart = True

        new_names_str = ", ".join(site_names)
        old_names_str = config.get("sites", "names", fallback="")

        if clean_val(old_names_str) != clean_val(new_names_str):
            config["sites"]["names"] = new_names_str
            requires_restart = True

        DEFAULT_SOLAR_URL = "https://v.dvr163.com/?type=truecloud#/login"

        for site in solar_sites:
            s_name = site["name"]
            s_email = site.get("email") or ""
            s_pass = site.get("password") or ""

            site_dict = {
                "url": DEFAULT_SOLAR_URL,
                "email": s_email,
                "password": s_pass
            }

            if not config.has_section(s_name):
                config[s_name] = site_dict
                requires_restart = True
            else:
                for k, v in site_dict.items():
                    if clean_val(config.get(s_name, k, fallback="")) != clean_val(v):
                        config[s_name][k] = str(v)
                        requires_restart = True

    # 2. SYNC CAMERAS SECTION
    if camera_list:
        SCN_SECTION = "camera_scn"
        if SCN_SECTION not in config:
            config[SCN_SECTION] = {}
            requires_restart = True

        existing_scns = [
            x.strip()
            for x in config.get(SCN_SECTION, "scns", fallback="").split(",")
            if x.strip()
        ]

        all_scns_from_payload = []

        for cam in camera_list:
            scn = str(cam.get("camera_scn", "")).strip()
            if not scn:
                continue

            all_scns_from_payload.append(scn)

            site_name = str(cam.get("site_name", "")).strip()
            name = str(cam.get("name", "")).strip()
            status = str(cam.get("status", "")).strip().lower()

            raw_is_process = cam.get("is_process", False)
            is_process = raw_is_process if isinstance(raw_is_process, bool) else (str(raw_is_process).lower() == "true")

            raw_stream_url = cam.get("stream_url")
            SOLAR_BASE_PATH = "/var/pnayanam/cctv/"

            # --- STRICT URL PROTECTION LOGIC ---
            existing_stream_url = config.get(scn, "stream_url", fallback="").strip()
            existing_is_solar = config.get(scn, "is_solar_cam", fallback="").strip()

            # Rule 1: If an existing stream_url is already present in INI, LOCK IT IN PLACE and DO NOT CHANGE IT.
            if existing_stream_url and existing_stream_url.lower() not in ("", "null", "none"):
                stream_url = existing_stream_url
                is_solar_cam = existing_is_solar if existing_is_solar else str(os.path.isdir(existing_stream_url))
            
            # Rule 2: If no stream_url exists yet, check if API provided a valid non-null string
            elif raw_stream_url and str(raw_stream_url).strip().lower() not in ("", "null", "none"):
                stream_url = str(raw_stream_url).strip()
                is_solar_cam = "False"
            
            # Rule 3: Fallback default ONLY for brand new solar cameras
            else:
                is_solar_cam = "True"
                stream_url = os.path.join(SOLAR_BASE_PATH, name)

            monitoring_times = cam.get("monitoring_time") or []
            start_list, end_list = [], []

            if isinstance(monitoring_times, list):
                for window in monitoring_times:
                    if not isinstance(window, dict):
                        continue
                    s_str = window.get("start", "00:00").strip()
                    e_str = window.get("end", "23:59").strip()
                    start_list.append(s_str)
                    end_list.append(e_str)
            else:
                start_list, end_list = ["00:00"], ["23:59"]

            camera_config_data = {
                "site_name": f'"{site_name}"',
                "site": f'"{site_name}"',
                "name": f'"{name}"',
                "stream_url": stream_url,
                "is_process": str(is_process),
                "is_solar_cam": str(is_solar_cam),
                "status": status,
                "debug": "True",
                "start_time": ", ".join(start_list) if start_list else "00:00",
                "end_time": ", ".join(end_list) if end_list else "23:59",
            }

            if not config.has_section(scn):
                config[scn] = camera_config_data
                config[scn]["result_storage_path"] = f"{scn.lower()}_result.mp4"
                requires_restart = True
            else:
                for k, v in camera_config_data.items():
                    old_v = config.get(scn, k, fallback="")
                    if clean_val(old_v) != clean_val(v):
                        config[scn][k] = str(v)

        new_scns = ",".join(all_scns_from_payload)
        old_scns = ",".join(existing_scns)
        if clean_val(old_scns) != clean_val(new_scns):
            config[SCN_SECTION]["scns"] = new_scns
            requires_restart = True

    if requires_restart:
        with open(CONFIG_FILE, "w") as f:
            config.write(f)
        logger.info("[CAMERA MONITOR] Camera list structural update detected. Restarting pipeline...")
        restart_event.set()
    else:
        # Save online/offline status, schedules, and is_process updates silently
        with open(CONFIG_FILE, "w") as f:
            config.write(f)


def camera_monitor():
    global camera_details_cache
    logger.info("[CAMERA MONITOR] thread started")

    while True:
        try:
            cameras = fetch_camera_details()
            sites = fetch_site_details()
            
            if cameras is not None or sites is not None:
                if cameras:
                    camera_details_cache = cameras
                sync_camera_and_site_config(cameras, sites)
        except Exception as e:
            logger.info(f"[CAMERA MONITOR] Error: {e}")

        time.sleep(cycle_time)