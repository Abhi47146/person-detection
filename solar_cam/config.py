from configparser import ConfigParser
from datetime import datetime
import requests

CONFIG_PATH = "/var/pnayanam/pnayanam.ini"


DEFAULT_SOLAR_URL = "https://v.dvr163.com/?type=truecloud#/login"


def _gui_base_url():
    config = ConfigParser()
    config.read(CONFIG_PATH)
    protocol = config.get("gui_ip_address", "protocol", fallback="http")
    gui_ip = config.get("gui_ip_address", "ip_address", fallback="127.0.0.1")
    port = config.get("gui_ip_address", "port", fallback="8000")
    return f"{protocol}://{gui_ip}:{port}"


def report_camera_status(scn, status, logger=None):
    """
    Pushes true online/offline (independent of is_process/schedule) to the
    same GUI endpoint cam_reading.py's readers use
    (Requirements.update_camera_status_api). Kept dependency-light so the
    solar status page doesn't need to import the whole pnayanam_utils stack.
    `status` should be "active" or "inactive".
    """
    try:
        url = f"{_gui_base_url()}/api/cameras/status/{scn}"
        response = requests.patch(url, json={"status": status}, timeout=3)
        if response.status_code == 200:
            msg = f"[{scn}] Camera status successfully updated to '{status}'."
            logger.info(msg) if logger else print(msg)
        else:
            msg = (
                f"[{scn}] Failed status update. Code: {response.status_code}, "
                f"Response: {response.text}"
            )
            logger.warning(msg) if logger else print(msg)
    except Exception as e:
        msg = f"[{scn}] Exception occurred while updating status: {e}"
        logger.error(msg) if logger else print(msg)


# ---------------------------------------------------------------------------
# Schedule helpers
# ---------------------------------------------------------------------------

def _parse_clock(value):
    """'HH:MM' -> datetime.time. Raises ValueError on bad input."""
    return datetime.strptime(value.strip(), "%H:%M").time()


def _within_any_window(start_list, end_list, now_time):
    """
    True if now_time falls inside any (start, end) pair.
    Pairs are matched positionally (start_time[i] <-> end_time[i]),
    same convention already used by fetch_cam_details.py.
    Windows where end < start are treated as spanning midnight.
    """
    for start_str, end_str in zip(start_list, end_list):
        try:
            start = _parse_clock(start_str)
            end = _parse_clock(end_str)
        except ValueError:
            continue

        if start <= end:
            if start <= now_time <= end:
                return True
        else:
            # overnight window, e.g. 22:00 -> 05:00
            if now_time >= start or now_time <= end:
                return True

    return False

def get_sites():
    config = ConfigParser()
    config.read(CONFIG_PATH)

    sites = {}

    if not config.has_section("sites"):
        return sites

    names = [
        name.strip()
        for name in config.get("sites", "names", fallback="").split(",")
        if name.strip()
    ]

    for site_name in names:
        if not config.has_section(site_name):
            continue

        url = config.get(site_name, "url", fallback=DEFAULT_SOLAR_URL).strip('"')
        if not url:
            url = DEFAULT_SOLAR_URL

        sites[site_name] = {
            "url": url,
            "email": config.get(site_name, "email", fallback="").strip('"'),
            "password": config.get(site_name, "password", fallback="").strip('"'),
        }

    return sites


def get_cameras_by_site():
    config = ConfigParser()
    config.read(CONFIG_PATH)

    cameras_by_site = {}

    if not config.has_section("camera_scn"):
        return cameras_by_site

    scns = [
        scn.strip()
        for scn in config.get("camera_scn", "scns", fallback="").split(",")
        if scn.strip()
    ]

    for scn in scns:
        if not config.has_section(scn):
            continue

        is_solar_cam = config.getboolean(scn, "is_solar_cam", fallback=False)

        if not is_solar_cam:
            continue

        camera_name = config.get(scn, "name", fallback=scn).strip('"')

        # FIX: Check 'site_name' first, fallback to 'site'
        site_name = config.get(scn, "site_name", fallback=config.get(scn, "site", fallback="")).strip('"')

        if not site_name:
            print(f"{scn}: skipped - no 'site_name' key set in config.")
            continue

        cameras_by_site.setdefault(site_name, []).append(camera_name)

    return cameras_by_site


def get_active_solar_cameras_by_site(now=None):
    """
    Cameras that should actually be *captured* right now:
    is_solar_cam=True, is_process=True, and the current time falls inside
    one of the camera's configured start_time/end_time windows.

    Returns {site_name: [{"scn": "CAM007", "name": "camera 1 ..."}, ...]}
    """
    config = ConfigParser()
    config.read(CONFIG_PATH)

    active_by_site = {}

    if not config.has_section("camera_scn"):
        return active_by_site

    scns = [
        scn.strip()
        for scn in config.get("camera_scn", "scns", fallback="").split(",")
        if scn.strip()
    ]

    now_time = (now or datetime.now()).time()

    for scn in scns:
        if not config.has_section(scn):
            continue

        if not config.getboolean(scn, "is_solar_cam", fallback=False):
            continue

        if not config.getboolean(scn, "is_process", fallback=False):
            continue

        start_list = [
            x.strip()
            for x in config.get(scn, "start_time", fallback="").split(",")
            if x.strip()
        ]
        end_list = [
            x.strip()
            for x in config.get(scn, "end_time", fallback="").split(",")
            if x.strip()
        ]

        if not _within_any_window(start_list, end_list, now_time):
            continue

        camera_name = config.get(scn, "name", fallback=scn).strip('"')
        site_name = config.get(
            scn, "site_name", fallback=config.get(scn, "site", fallback="")
        ).strip('"')

        if not site_name:
            continue

        active_by_site.setdefault(site_name, []).append(
            {"scn": scn, "name": camera_name}
        )

    return active_by_site


def get_solar_camera_scn_map_by_site():
    """
    All solar cameras for every site, regardless of is_process/schedule.
    Used by the status-only page, which needs to know which SCN a given
    on-screen camera name in the DVR sidebar belongs to.

    Returns {site_name: {camera_name: scn}}
    """
    config = ConfigParser()
    config.read(CONFIG_PATH)

    result = {}

    if not config.has_section("camera_scn"):
        return result

    scns = [
        scn.strip()
        for scn in config.get("camera_scn", "scns", fallback="").split(",")
        if scn.strip()
    ]

    for scn in scns:
        if not config.has_section(scn):
            continue

        if not config.getboolean(scn, "is_solar_cam", fallback=False):
            continue

        camera_name = config.get(scn, "name", fallback=scn).strip('"')
        site_name = config.get(
            scn, "site_name", fallback=config.get(scn, "site", fallback="")
        ).strip('"')

        if not site_name:
            continue

        result.setdefault(site_name, {})[camera_name] = scn

    return result


def update_camera_online_status(scn, is_online):
    """
    Persists the live online/offline reading (from the DVR sidebar) back
    into the camera's section as `is_online`. Only writes to disk when the
    value actually changed, to avoid needless churn on the shared ini file.
    """
    config = ConfigParser()
    config.read(CONFIG_PATH)

    if not config.has_section(scn):
        return

    new_value = "True" if is_online else "False"
    current_value = config.get(scn, "is_online", fallback=None)

    if current_value is not None and current_value.strip() == new_value:
        return

    config[scn]["is_online"] = new_value

    with open(CONFIG_PATH, "w") as f:
        config.write(f)