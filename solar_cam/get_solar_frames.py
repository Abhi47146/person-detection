from multiprocessing import Process
from configparser import ConfigParser
import threading
import time

from solar_cam.monitor_camera import monitor_camera

CONFIG_PATH = "/var/pnayanam/pnayanam.ini"

def get_cameras():
    config = ConfigParser()
    config.read(CONFIG_PATH)
    cameras = []

    if config.has_section("camera_scn"):
        scns = [
            scn.strip()
            for scn in config.get("camera_scn", "scns", fallback="").split(",")
            if scn.strip()
        ]

        for idx, scn in enumerate(scns, start=1):
            if not config.has_section(scn):
                continue

            is_solar_cam = config.getboolean( scn, "is_solar_cam",)
            # Collect only solar cameras
            if not is_solar_cam:
                continue

            camera_name = config.get( scn, "name", fallback=scn ).strip('"')
            cameras.append((camera_name, idx))

    return cameras


def camera_collection_manager():
    """
    Monitors the config every 5 seconds and
    starts/stops camera processes accordingly.
    """

    running_processes = {}

    while True:

        cameras = get_cameras()

        desired = {
            camera_name: window
            for camera_name, window in cameras
        }

        # Start newly added cameras
        for camera_name, window in desired.items():

            if (
                camera_name not in running_processes
                or not running_processes[camera_name].is_alive()
            ):

                p = Process(
                    target=monitor_camera,
                    args=(camera_name, window),
                    daemon=True,
                )

                p.start()
                running_processes[camera_name] = p
                time.sleep(2)

        # Stop removed cameras
        for camera_name in list(running_processes.keys()):

            if camera_name not in desired:

                running_processes[camera_name].terminate()
                running_processes[camera_name].join()

                del running_processes[camera_name]
        time.sleep(5)


def start_solar_camera_collection_thread():
    """
    Starts the camera collection manager in a background thread.
    """

    t = threading.Thread(
        target=camera_collection_manager,
        daemon=True,
        name="CameraCollectionManager",
    )

    t.start()
    return t


if __name__ == "__main__":
    camera_collection_manager()