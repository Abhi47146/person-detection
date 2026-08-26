from pnayanam.person_detection import (
    main,
    start_subscriber_thread,
    start_camDetails_thread,
    start_gpu_resources_thread,
)

from solar_cam.get_solar_frames import start_solar_camera_collection_thread

if __name__ == "__main__":

    # Always running
    start_camDetails_thread()

    # Always running
    start_subscriber_thread()

    # Always running
    start_gpu_resources_thread()

    # Always running
    # start_solar_camera_collection_thread()

    # Runs according to the SCNs written by camera_monitor()
    main()