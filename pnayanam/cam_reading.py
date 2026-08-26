import cv2
import time
import threading
from typing import  Optional
from datetime import datetime
from multiprocessing import shared_memory
from configparser import ConfigParser
import numpy as np
import traceback
import os

from pnayanam.telegram_alert import send_camera_disconnect_alert

from pnayanam.pnayanam_utils import Requirements
from pnayanam.logger import Logger

requirements = Requirements()

logger = Logger("camera_reading")

config_out = ConfigParser()
config_out.read("/var/pnayanam/pnayanam.ini")

blur_score = config_out.getint("blur_score", "blur_score")

# small helper
def format_timestamp(dt: datetime) -> str:
    return dt.strftime("%d-%m-%Y%H:%M:%S")

def reconnect_to_camera(source_path, logger, stop_event=None):
    while True:
        if stop_event is not None and stop_event.is_set():
            return None

        video_capture = cv2.VideoCapture(source_path)

        if video_capture.isOpened():
            logger.debug(f"Successfully reconnected to camera {source_path}")
            return video_capture

        logger.warning("Reconnect failed. Retrying...")

        for _ in range(50):
            if stop_event is not None and stop_event.is_set():
                return None
            time.sleep(0.1)

def _make_offline_frame(h: int, w: int, c: int, text: str = "No frames") -> np.ndarray:
    frame = np.zeros((h, w, c), dtype=np.uint8)
    try:
        cv2.putText(frame, text, (10, max(20, h // 2)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)
    except Exception:
        pass
    return frame


def write_frame_seq(header_arr: np.ndarray, frame_view: np.ndarray, frame: np.ndarray):
    """
    Lock-free writer: seq -> seq+1 (odd) write -> seq+2 (even)
    header_arr is a numpy uint64 view length 1; frame_view is numpy uint8 mapped to SHM
    """
    seq = int(header_arr[0])
    header_arr[0] = seq + 1           # mark in-progress (odd)
    # copy frame bytes
    np.copyto(frame_view, frame)
    header_arr[0] = seq + 2  

# ensures connection
def connect_to_cam(
        scn: str,
        stream_url: str,
        retry_delay: int = 10,
        max_attempts: Optional[int] = None,
        stop_event=None
    ):
        attempt = 0
        while True:
            if stop_event is not None and stop_event.is_set():
                return None

            attempt += 1
            cap = cv2.VideoCapture(stream_url)
            if cap is None or not cap.isOpened():
                logger.warning(f"[{scn}] VideoCapture not opened; retrying in {retry_delay} s")
                try:
                    if cap:
                        cap.release()
                except Exception:
                    pass
                if max_attempts and attempt >= max_attempts:
                    attempt=0
                
                # time.sleep(retry_delay): Sleep in small intervals so restart is responsive
                for _ in range(retry_delay * 10):

                    if stop_event is not None and stop_event.is_set():
                        try:
                            cap.release()
                        except Exception:
                            pass
                        return None

                    time.sleep(0.1)

                continue

            # test single read
            ret, frame = cap.read()
            if not ret or frame is None:
                logger.warning(f"[{scn}] Connected but no frames; closing and retrying")
                try:
                    cap.release()
                except Exception:
                    pass
                if max_attempts and attempt >= max_attempts:
                    attempt=0
                # Sleep in small intervals so restart is responsive
                for _ in range(retry_delay * 10):

                    if stop_event is not None and stop_event.is_set():
                        try:
                            cap.release()
                        except Exception:
                            pass
                        return None

                    time.sleep(0.1)
                
                continue
            # success
            logger.info(f"Successfully connected to camera stream for [{scn}]")
            return cap
        return None

def _camera_reader_thread_rtsp(scn: str, debug, send_gui, rtsp: str, shm_name: str, header_name: str, frame_shape: tuple, max_attempts: int, stop_event, TARGET_FPS: int):
    """
    Lock-free camera reader: writer marks header odd while writing, then even when done.
    This function attaches to the frame_shm (buf) and header_shm (uint64) and writes frames using write_frame_seq.
    """
    H, W, C = frame_shape
    cap = None
    shm = None
    header_shm = None

    try:
        shm = shared_memory.SharedMemory(name=shm_name)
        header_shm = shared_memory.SharedMemory(name=header_name)
        buf = np.ndarray((H, W, C), dtype=np.uint8, buffer=shm.buf)
        header_arr = np.ndarray((1,), dtype=np.uint64, buffer=header_shm.buf)
    except Exception as e:
        logger.exception(f"[CameraReader-{scn}] cannot attach SHM: {e}")
        return

    offline_frame = _make_offline_frame(H, W, C, "No frames")

    # First call Purpose: initial connection, Starts the stream
    cap = connect_to_cam(
        scn=scn,
        stream_url=rtsp,
        max_attempts=max_attempts,
        stop_event=stop_event
    )
    if cap is None:
        return 

    if stop_event.is_set():
        return

    if cap is None or not cap.isOpened():
        logger.error(f"[CameraReader-{scn}] cannot open {rtsp} — will write offline frames and retry")

    sleep_t = 1.0 / TARGET_FPS if TARGET_FPS else 0.0

    try:
        # while True:
        while not stop_event.is_set():

            if cap is None or not getattr(cap, "isOpened", lambda: False)():

                write_frame_seq(header_arr, buf, offline_frame)
                cap = connect_to_cam(scn=scn, stream_url=rtsp, max_attempts=max_attempts, stop_event=stop_event)

                if stop_event.is_set():
                    break

                continue

                # 1. ALERT FOR CAMERA DISCONNECTED/OFFLINE..!
                alert = "Camera Offline Alert"
                encoded_img = ""
                    
                # SEND TO GUI
                if send_gui == 1:
                    timestamp = datetime.now().strftime("%d-%m-%y %H:%M:%S")
                    scn = scn.strip()
                    cam_cfg = config_out[scn]
                    site = cam_cfg.get("site_name", "").strip('"')
                    camera_name = cam_cfg.get("name", "").strip('"')
                    try:
                        payload_gui = {
                            "alert_type_name": alert,
                            "camera_scn": scn,
                            "camera_name": camera_name,
                            "site_name": site,
                            "created_at": timestamp,
                            "image_url": encoded_img
                        }
                        if debug: 
                            logger.info("called for sendingh data")
                        requirements.send_to_gui(payload_gui)
                        if debug:
                            logger.info("Done !")
                    except Exception as e:
                        logger.exception(f"[{scn}] send_to_gui failed {e}")

                logger.error(f"Camera disconnected for {scn}")
                send_camera_disconnect_alert()

                if cap is not None:
                    cap.release()

                cap = reconnect_to_camera(rtsp, logger, stop_event)
                if cap is None:
                    break
                if stop_event.is_set():
                    break
                continue

            ret, frame = cap.read()

            # Don't process invalid frames
            if not ret or frame is None:
                continue

            # CHECK SHARED MEMORY FRAME SIZE, Log once every 15 seconds if mismatch exists
            frame = cv2.resize(frame, (W,H))
            frame_h, frame_w, frame_c = frame.shape

            # 2. ALERT FOR FRAME IS CORRUPTED/IMPROPER_READ/EMPTY..! Possible reasons: decoder issue, corrupted frame, or temporary stream glitch.
            if requirements.is_invalid_frame(frame):
                alert = "Empty Frame Alert"

                # ENCODE IMAGE
                encoded_img = ""
                try:
                    if frame is not None:
                        encoded_img = requirements.encode_image(frame)
                except Exception:
                    logger.exception(f"{scn} encode_image failed")
                    
                # SEND TO GUI
                if send_gui == 1:
                    timestamp = datetime.now().strftime("%d-%m-%y %H:%M:%S")
                    scn = scn.strip()
                    cam_cfg = config_out[scn]
                    site = cam_cfg.get("site_name", "").strip('"')
                    camera_name = cam_cfg.get("name", "").strip('"')
                    try:
                        payload_gui = {
                            "alert_type_name": alert,
                            "camera_scn": scn,
                            "camera_name": camera_name,
                            "site_name": site,
                            "created_at": timestamp,
                            "image_url": encoded_img
                        }
                        if debug: 
                            logger.info("called for sendingh data")
                        requirements.send_to_gui(payload_gui)
                        if debug:
                            logger.exception("Done !")
                    except Exception as e:
                        logger.exception(f"[{scn}] send_to_gui failed: {e}")

                # send_invalid_frame_alert()
                continue

            # 3. ALERT FOR FRAME IS BLURRED..!
            blur_score_frame = requirements.get_blur_score(frame)
            if blur_score_frame < blur_score:
                alert = "Camera Obstruction Alert"

                # ENCODE IMAGE
                encoded_img = ""
                try:
                    if frame is not None:
                        encoded_img = requirements.encode_image(frame)
                except Exception:
                    logger.exception(f"[{scn}] encode_image failed")
                    
                # SEND TO GUI
                if send_gui == 0:
                    timestamp = datetime.now().strftime("%d-%m-%y %H:%M:%S")
                    scn = scn.strip()
                    cam_cfg = config_out[scn]
                    site = cam_cfg.get("site_name", "").strip('"')
                    camera_name = cam_cfg.get("name", "").strip('"')
                    try:
                        payload_gui = {
                            "alert_type_name": alert,
                            "camera_scn": scn,
                            "camera_name": camera_name,
                            "site_name": site,
                            "created_at": timestamp,
                            "image_url": encoded_img
                        }
                        if debug: 
                            logger.info("called for sendingh data")
                        requirements.send_to_gui(payload_gui)
                        if debug:
                            logger.info("Done !")
                    except Exception as e:
                        logger.exception(f"[{scn}] send_to_gui failed: {e}")

                # send_camera_obstruction_alert()
                # logger.info(f"Blurred Camera: {blur_score}")


            if not ret or frame is None:
                # write offline and reconnect
                write_frame_seq(header_arr, buf, offline_frame)
                try:
                    cap.release()
                except Exception:
                    pass

                cap = None
                continue

            try:
                write_frame_seq(header_arr, buf, frame)
            except Exception:
                # in case of any error writing SHM, write offline frame to keep readers stable
                try:
                    write_frame_seq(header_arr, buf, offline_frame)
                except Exception:
                    pass

            if sleep_t:
                time.sleep(sleep_t)
            
    except Exception:
        logger.exception(traceback.format_exc())
    finally:
        try:
            if cap is not None:
                cap.release()
        except Exception:
            pass
        try:
            if shm is not None:
                shm.close()
        except Exception:
            pass
        try:
            if header_shm is not None:
                header_shm.close()
        except Exception:
            pass

        logger.info(f"[{scn}] Camera thread exited cleanly.")


def _camera_reader_thread_solarcam( scn: str, debug, send_gui, frame_folder, shm_name: str, header_name: str, frame_shape: tuple, max_attempts: int, stop_event, TARGET_FPS: int,):
    """
    Reads frames from filesystem instead of RTSP stream and writes them to shared memory.
    """
    H, W, C = frame_shape

    shm = None
    header_shm = None

    try:
        shm = shared_memory.SharedMemory(name=shm_name)
        header_shm = shared_memory.SharedMemory(name=header_name)

        buf = np.ndarray((H, W, C), dtype=np.uint8, buffer=shm.buf)
        header_arr = np.ndarray((1,), dtype=np.uint64, buffer=header_shm.buf)

    except Exception as e:
        logger.exception(f"[CameraReader-{scn}] Cannot attach shared memory: {e}")
        return

    #offline_frame = _make_offline_frame(H, W, C, "No Frames")

    frame_path = os.path.join(frame_folder, "latest_frame.png")
    logger.info(f"Frame Path is : {frame_path}")

    sleep_t = 1.0 / TARGET_FPS if TARGET_FPS else 0.125

    logger.info(f"[{scn}] Reading frames from: {frame_path}")

    try:

        # =======

        camera_status = "active"
        last_frame_mtime = None
        last_log_time = time.time()

        while not stop_event.is_set():

            # Case 1: Frame file missing entirely
            if not os.path.exists(frame_path):
                if camera_status != "inactive":
                    camera_status = "inactive"
                    requirements.update_camera_status_api(scn, "inactive")

                time.sleep(sleep_t)
                continue

            current_mtime = os.path.getmtime(frame_path)

            # Case 2: Frame file exists but HAS NOT updated
            if current_mtime == last_frame_mtime:

                if time.time() - last_log_time >= 3:
                    logger.warning(
                        f"[{scn}] No new frame received for the last 3 seconds. "
                        f"Skipping SHM write and YOLO processing."
                    )
                    last_log_time = time.time()

                    # Trigger API update ONLY when transitioning from active -> inactive
                    if camera_status != "inactive":
                        camera_status = "inactive"
                        requirements.update_camera_status_api(scn, "inactive")

                if sleep_t:
                    time.sleep(sleep_t)

                continue

            # Case 3: New frame file detected
            img = cv2.imread(frame_path)

            if img is None:
                logger.warning(f"[{scn}] Failed to read {frame_path}")
                if sleep_t:
                    time.sleep(sleep_t)
                continue

            # Successful frame read: transition back to active/online
            if camera_status != "active":
                camera_status = "active"
                requirements.update_camera_status_api(scn, "active")

            frame = cv2.resize(img, (W, H))

            last_frame_mtime = current_mtime
            last_log_time = time.time()

            logger.info(f"[{scn}] Loaded new frame.")


        # =======


            # 1. ALERT FOR FRAME IS CORRUPTED/IMPROPER_READ/EMPTY..! Possible reasons: decoder issue, corrupted frame, or temporary stream glitch.
            if requirements.is_invalid_frame(frame):
                alert = "Empty Frame Alert"

                # ENCODE IMAGE
                encoded_img = ""
                try:
                    if frame is not None:
                        encoded_img = requirements.encode_image(frame)
                except Exception:
                    logger.exception(f"[{scn}] encode_image failed")
                    
                # SEND TO GUI
                if send_gui == 1:
                    timestamp = datetime.now().strftime("%d-%m-%y %H:%M:%S")
                    scn = scn.strip()
                    cam_cfg = config_out[scn]
                    site = cam_cfg.get("site_name", "").strip('"')
                    camera_name = cam_cfg.get("name", "").strip('"')
                    try:
                        payload_gui = {
                            "alert_type_name": alert,
                            "camera_scn": scn,
                            "camera_name": camera_name,
                            "site_name": site,
                            "created_at": timestamp,
                            "image_url": encoded_img
                        }
                        if debug: 
                            logger.info("called for sendingh data")
                        requirements.send_to_gui(payload_gui)
                        if debug:
                            logger.info("Done !")
                    except Exception as e:
                        logger.exception(f"[{scn}] send_to_gui failed: {e}")


                # send_invalid_frame_alert()
                continue

            # 2. ALERT FOR FRAME IS BLURRED..!
            blur_score_frame = requirements.get_blur_score(frame)
            if blur_score_frame < blur_score:
                alert = "Camera Obstruction Alert"

                # ENCODE IMAGE
                encoded_img = ""
                try:
                    if frame is not None:
                        encoded_img = requirements.encode_image(frame)
                except Exception:
                    logger.exception(f"[{scn}] encode_image failed")
                    
                # SEND TO GUI
                if send_gui == 0:
                    timestamp = datetime.now().strftime("%d-%m-%y %H:%M:%S")
                    scn = scn.strip()
                    cam_cfg = config_out[scn]
                    site = cam_cfg.get("site_name", "").strip('"')
                    camera_name = cam_cfg.get("name", "").strip('"')
                    try:
                        payload_gui = {
                            "alert_type_name": alert,
                            "camera_scn": scn,
                            "camera_name": camera_name,
                            "site_name": site,
                            "created_at": timestamp,
                            "image_url": encoded_img
                        }
                        if debug: 
                            logger.info("called for sendingh data")
                        requirements.send_to_gui(payload_gui)
                        if debug:
                            logger.info("Done !")
                    except Exception as e:
                        logger.exception(f"[{scn}] send_to_gui failed: {e}")

                # send_camera_obstruction_alert()
                # logger.info(f"Blurred Camera: {blur_score}")

            write_frame_seq(header_arr, buf, frame)

            if sleep_t:
                time.sleep(sleep_t)

    except Exception:
        logger.exception(traceback.format_exc())

    finally:

        try:
            if shm is not None:
                shm.close()
        except Exception:
            pass

        try:
            if header_shm is not None:
                header_shm.close()
        except Exception:
            pass

        logger.warning(f"[{scn}] Camera reader exited.")


def start_camera_readers( camera_scns, send_gui, debug, shm_map, header_map, frame_shape, max_attempts, stop_event, TARGET_FPS: int = 8,):
    threads = []
    config_out = ConfigParser()
    config_out.read("/var/pnayanam/pnayanam.ini")

    for scn in camera_scns:
        if config_out.has_section(scn):
            cam_conf = dict(config_out.items(scn))
        elif config_out.has_section("scn"):
            cam_conf = dict(config_out.items("scn"))
        else:
            cam_conf = {}

        stream_source = cam_conf.get("stream_url", "").strip()

        if not stream_source:
            logger.warning(f"[{scn}] No stream configured. Skipping.")
            continue

        # Decide which reader to use
        if os.path.isdir(stream_source):
            target = _camera_reader_thread_solarcam
            logger.info(f"[{scn}] Source: SOLAR CAMERA ({stream_source})")

        else:
            target = _camera_reader_thread_rtsp
            logger.info(f"[{scn}] Source: RTSP ({stream_source})")

        t = threading.Thread(
            target=target,
            args=( scn, debug, send_gui, stream_source, shm_map[scn], header_map[scn], frame_shape, max_attempts, stop_event, TARGET_FPS,),
            daemon=True,
        )

        t.start()
        threads.append(t)
        logger.info(f"[{scn}] Started {target.__name__}")
    return threads




 





