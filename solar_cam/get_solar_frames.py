import threading
import time
from solar_cam.config import get_sites, get_active_solar_cameras_by_site
from solar_cam.monitor_camera import site_status_monitor, single_camera_capture_worker


def site_collection_manager():
    """
    Main manager loop:
    1. Spawns 1 status monitor thread per configured site (runs continuously to report online/offline).
    2. Spawns dedicated capture worker threads for cameras whose is_process=True and falls in current time schedule.
    """
    status_threads = {}
    capture_threads = {}
    CAMERA_BASE_PATH = "/var/pnayanam/cctv"

    while True:
        sites = get_sites()

        # 1. Manage site status monitors (1 per site)
        for site_name, site_config in sites.items():
            if site_name not in status_threads or not status_threads[site_name].is_alive():
                t = threading.Thread(
                    target=site_status_monitor,
                    args=(site_name, site_config),
                    daemon=True,
                    name=f"StatusMonitor-{site_name}"
                )
                t.start()
                status_threads[site_name] = t

        # 2. Manage scheduled capture workers
        active_cameras_by_site = get_active_solar_cameras_by_site()

        for site_name, active_cams in active_cameras_by_site.items():
            if site_name not in sites:
                continue

            site_config = sites[site_name]
            site_config["site_name"] = site_name

            for cam_info in active_cams:
                scn = cam_info["scn"]

                if scn not in capture_threads or not capture_threads[scn].is_alive():
                    t = threading.Thread(
                        target=single_camera_capture_worker,
                        args=(site_config, cam_info, CAMERA_BASE_PATH),
                        daemon=True,
                        name=f"CaptureWorker-{scn}"
                    )
                    t.start()
                    capture_threads[scn] = t

        time.sleep(10)


def start_solar_camera_collection_thread():
    t = threading.Thread(
        target=site_collection_manager,
        daemon=True,
        name="SiteCollectionManager",
    )
    t.start()
    return t


if __name__ == "__main__":
    site_collection_manager()