from multiprocessing import Process
import threading
import time

from solar_cam.config import get_sites
from solar_cam.monitor_camera import monitor_site


def site_collection_manager():

    running_processes = {}

    while True:

        sites = get_sites()

        # Start newly added sites
        for site_name, site_config in sites.items():

            if (
                site_name not in running_processes
                or not running_processes[site_name].is_alive()
            ):

                p = Process(
                    target=monitor_site,
                    args=(site_name, site_config),
                    daemon=True,
                )

                p.start()
                running_processes[site_name] = p
                time.sleep(5)

        # Stop removed sites
        for site_name in list(running_processes.keys()):

            if site_name not in sites:

                running_processes[site_name].terminate()
                running_processes[site_name].join()

                del running_processes[site_name]

        time.sleep(5)


def start_solar_camera_collection_thread():
    """
    Starts the site collection manager in a background thread.
    """

    t = threading.Thread(
        target=site_collection_manager,
        daemon=True,
        name="SiteCollectionManager",
    )

    t.start()
    return t


if __name__ == "__main__":
    site_collection_manager()
