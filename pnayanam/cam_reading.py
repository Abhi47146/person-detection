import cv2
import time
import threading
from typing import Optional
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

blur_score = config_out.getint("blur_score", "blur_score", fallback=50)


def format_timestamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


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
    """
    seq = int(header_arr[0])
    header_arr[0] = seq + 1           # mark in-progress (odd)
    np.copyto(frame_view, frame)
    header_arr[0] = seq + 2           # mark complete (even)


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
                attempt = 0

            for _ in range(retry_delay * 10):
                if stop_event is not None and stop_event.is_set():
                    try:
                        cap.release()
                    except Exception:
                        pass
                    return None
                time.sleep(0.1)
            continue

        ret, frame = cap.read()
        if not ret or frame is None:
            # Rewind if local video file
            if not stream_url.startswith("rtsp://") and not stream_url.startswith("http://"):
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ret, frame = cap.read()
                if ret and frame is not None:
                    logger.info(f"Successfully connected and rewound test file for [{scn}]")
                    return cap

            logger.warning(f"[{scn}] Connected but no frames; closing and retrying")
            try:
                cap.release()
            except Exception:
                pass
            if max_attempts and attempt >= max_attempts:
                attempt = 0

            for _ in range(retry_delay * 10):
                if stop_event is not None and stop_event.is_set():
                    try:
                        cap.release()
                    except Exception:
                        pass
                    return None
                time.sleep(0.1)
            continue

        logger.info(f"Successfully connected to camera stream for [{scn}]")
        return cap


def _camera_reader_thread_rtsp(scn: str, debug, send_gui, rtsp: str, shm_name: str, header_name: str, frame_shape: tuple, max_attempts: int, stop_event, TARGET_FPS: int):
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

    cap = connect_to_cam(
        scn=scn,
        stream_url=rtsp,
        max_attempts=max_attempts,
        stop_event=stop_event
    )
    if cap is None or stop_event.is_set():
        return

    sleep_t = 1.0 / TARGET_FPS if TARGET_FPS else 0.0

    try:
        while not stop_event.is_set():
            if cap is None or not getattr(cap, "isOpened", lambda: False)():
                header_arr[0] = 0
                write_frame_seq(header_arr, buf, offline_frame)

                if send_gui == 1:
                    iso_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    scn_clean = scn.strip()
                    config_out.read("/var/pnayanam/pnayanam.ini")
                    cam_cfg = config_out[scn_clean] if scn_clean in config_out else {}
                    site = cam_cfg.get("site_name", cam_cfg.get("site", "")).strip('"')
                    camera_name = cam_cfg.get("name", "").strip('"')

                    try:
                        payload_gui = {
                            "alert_type_name": "Camera Offline Alert",
                            "camera_scn": scn_clean,
                            "camera_name": camera_name,
                            "site_name": site,
                            "created_at": iso_timestamp,
                            "image_url": ""
                        }
                        requirements.send_to_gui(payload_gui)
                    except Exception as e:
                        logger.exception(f"[{scn_clean}] send_to_gui failed {e}")

                send_camera_disconnect_alert()

                cap = connect_to_cam(scn=scn, stream_url=rtsp, max_attempts=max_attempts, stop_event=stop_event)
                if stop_event.is_set():
                    break
                continue

            ret, frame = cap.read()

            # Automatic looping for test files
            if not ret or frame is None:
                if not rtsp.startswith("rtsp://") and not rtsp.startswith("http://"):
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ret, frame = cap.read()
                    if not ret or frame is None:
                        time.sleep(0.01)
                        continue
                else:
                    header_arr[0] = 0
                    write_frame_seq(header_arr, buf, offline_frame)
                    try:
                        if cap:
                            cap.release()
                    except Exception:
                        pass
                    cap = None
                    continue

            frame = cv2.resize(frame, (W, H))

            if requirements.is_invalid_frame(frame):
                alert = "Empty Frame Alert"
                encoded_img = ""
                try:
                    if frame is not None:
                        encoded_img = requirements.encode_image(frame)
                except Exception:
                    logger.exception(f"{scn} encode_image failed")

                if send_gui == 1:
                    iso_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    scn_clean = scn.strip()
                    config_out.read("/var/pnayanam/pnayanam.ini")
                    cam_cfg = config_out[scn_clean] if scn_clean in config_out else {}
                    site = cam_cfg.get("site_name", cam_cfg.get("site", "")).strip('"')
                    camera_name = cam_cfg.get("name", "").strip('"')

                    formatted_image_url = f"data:image/jpeg;base64,{encoded_img}" if encoded_img else ""

                    try:
                        payload_gui = {
                            "alert_type_name": alert,
                            "camera_scn": scn_clean,
                            "camera_name": camera_name,
                            "site_name": site,
                            "created_at": iso_timestamp,
                            "image_url": formatted_image_url
                        }
                        requirements.send_to_gui(payload_gui)
                    except Exception as e:
                        logger.exception(f"[{scn}] send_to_gui failed: {e}")
                continue

            blur_score_frame = requirements.get_blur_score(frame)
            if blur_score_frame < blur_score:
                alert = "Camera Obstruction Alert"
                encoded_img = ""
                try:
                    if frame is not None:
                        encoded_img = requirements.encode_image(frame)
                except Exception:
                    logger.exception(f"[{scn}] encode_image failed")

                if send_gui == 1:
                    iso_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    scn_clean = scn.strip()
                    config_out.read("/var/pnayanam/pnayanam.ini")
                    cam_cfg = config_out[scn_clean] if scn_clean in config_out else {}
                    site = cam_cfg.get("site_name", cam_cfg.get("site", "")).strip('"')
                    camera_name = cam_cfg.get("name", "").strip('"')

                    formatted_image_url = f"data:image/jpeg;base64,{encoded_img}" if encoded_img else ""

                    try:
                        payload_gui = {
                            "alert_type_name": alert,
                            "camera_scn": scn_clean,
                            "camera_name": camera_name,
                            "site_name": site,
                            "created_at": iso_timestamp,
                            "image_url": formatted_image_url
                        }
                        requirements.send_to_gui(payload_gui)
                    except Exception as e:
                        logger.exception(f"[{scn}] send_to_gui failed: {e}")

            write_frame_seq(header_arr, buf, frame)

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


def _camera_reader_thread_solarcam(scn: str, debug, send_gui, frame_folder, shm_name: str, header_name: str, frame_shape: tuple, max_attempts: int, stop_event, TARGET_FPS: int):
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

    frame_path = os.path.join(frame_folder, "latest_frame.png")
    sleep_t = 1.0 / TARGET_FPS if TARGET_FPS else 0.125
    offline_frame = _make_offline_frame(H, W, C, "No frames")

    try:
        camera_status = "active"
        last_frame_mtime = None
        last_log_time = time.time()

        while not stop_event.is_set():
            if not os.path.exists(frame_path):
                if camera_status != "inactive":
                    camera_status = "inactive"
                    requirements.update_camera_status_api(scn, "inactive")
                    header_arr[0] = 0
                    write_frame_seq(header_arr, buf, offline_frame)

                time.sleep(sleep_t)
                continue

            current_mtime = os.path.getmtime(frame_path)

            if current_mtime == last_frame_mtime:
                # 45 second timeout prevents toggling inactive while Playwright cycles
                if time.time() - last_log_time >= 45:
                    last_log_time = time.time()

                    if camera_status != "inactive":
                        camera_status = "inactive"
                        requirements.update_camera_status_api(scn, "inactive")
                        header_arr[0] = 0
                        write_frame_seq(header_arr, buf, offline_frame)

                if sleep_t:
                    time.sleep(sleep_t)

                continue

            img = cv2.imread(frame_path)

            if img is None:
                if sleep_t:
                    time.sleep(sleep_t)
                continue

            if camera_status != "active":
                camera_status = "active"
                requirements.update_camera_status_api(scn, "active")

            frame = cv2.resize(img, (W, H))
            last_frame_mtime = current_mtime
            last_log_time = time.time()

            if requirements.is_invalid_frame(frame):
                alert = "Empty Frame Alert"
                encoded_img = ""
                try:
                    if frame is not None:
                        encoded_img = requirements.encode_image(frame)
                except Exception:
                    logger.exception(f"{scn} encode_image failed")

                if send_gui == 1:
                    iso_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    scn_clean = scn.strip()
                    config_out.read("/var/pnayanam/pnayanam.ini")
                    cam_cfg = config_out.get(scn_clean, {})
                    site = cam_cfg.get("site_name", cam_cfg.get("site", "")).strip('"')
                    camera_name = cam_cfg.get("name", "").strip('"')

                    formatted_image_url = f"data:image/jpeg;base64,{encoded_img}" if encoded_img else ""

                    try:
                        payload_gui = {
                            "alert_type_name": alert,
                            "camera_scn": scn_clean,
                            "camera_name": camera_name,
                            "site_name": site,
                            "created_at": iso_timestamp,
                            "image_url": formatted_image_url
                        }
                        requirements.send_to_gui(payload_gui)
                    except Exception as e:
                        logger.exception(f"[{scn}] send_to_gui failed: {e}")
                continue

            blur_score_frame = requirements.get_blur_score(frame)
            if blur_score_frame < blur_score:
                alert = "Camera Obstruction Alert"
                encoded_img = ""
                try:
                    if frame is not None:
                        encoded_img = requirements.encode_image(frame)
                except Exception:
                    logger.exception(f"[{scn}] encode_image failed")

                if send_gui == 1:
                    iso_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    scn_clean = scn.strip()
                    config_out.read("/var/pnayanam/pnayanam.ini")
                    cam_cfg = config_out.get(scn_clean, {})
                    site = cam_cfg.get("site_name", cam_cfg.get("site", "")).strip('"')
                    camera_name = cam_cfg.get("name", "").strip('"')

                    formatted_image_url = f"data:image/jpeg;base64,{encoded_img}" if encoded_img else ""

                    try:
                        payload_gui = {
                            "alert_type_name": alert,
                            "camera_scn": scn_clean,
                            "camera_name": camera_name,
                            "site_name": site,
                            "created_at": iso_timestamp,
                            "image_url": formatted_image_url
                        }
                        requirements.send_to_gui(payload_gui)
                    except Exception as e:
                        logger.exception(f"[{scn}] send_to_gui failed: {e}")

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


def start_camera_readers(camera_scns, send_gui, debug, shm_map, header_map, frame_shape, max_attempts, stop_event, TARGET_FPS: int = 8):
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

        if os.path.isdir(stream_source):
            target = _camera_reader_thread_solarcam
            logger.info(f"[{scn}] Source: SOLAR CAMERA ({stream_source})")
        else:
            target = _camera_reader_thread_rtsp
            logger.info(f"[{scn}] Source: RTSP / FILE ({stream_source})")

        t = threading.Thread(
            target=target,
            args=(scn, debug, send_gui, stream_source, shm_map[scn], header_map[scn], frame_shape, max_attempts, stop_event, TARGET_FPS),
            daemon=True,
        )

        t.start()
        threads.append(t)
        logger.info(f"[{scn}] Started {target.__name__}")
    return threads