from __future__ import annotations
import os
import sys
import time
import ast
import multiprocessing as mp
from datetime import datetime
from configparser import ConfigParser
from typing import Dict, Tuple
from multiprocessing import shared_memory, Event
import numpy as np
import threading
from ultralytics import YOLO
import cv2
import struct

try:
    import supervision as sv
except ImportError:
    sv = None

from pnayanam.logger import Logger
from pnayanam.whatsapp_alert import send_person_alert
from pnayanam.collect_subscribers import subscriber_monitor
from pnayanam.fetch_cam_details import camera_monitor, restart_event
from pnayanam.send_gpu_details import edge_health_monitor
from pnayanam.cam_reading import start_camera_readers
from pnayanam.pnayanam_utils import Requirements
from solar_cam.get_solar_frames import start_solar_camera_collection_thread

requirements = Requirements()
logger = Logger("person_detection")

HEADER_FMT = "<QIII"
HEADER_SIZE = struct.calcsize(HEADER_FMT)


def is_time_within_intervals(start_str: str, end_str: str) -> bool:
    """
    Checks if current time falls within any comma-separated start_time and end_time pairs.
    Example:
        start_str = "10:00, 13:00, 11:53"
        end_str   = "11:30, 16:00, 11:58"
    """
    if not start_str or not end_str:
        return True

    starts = [s.strip() for s in start_str.split(",")]
    ends = [e.strip() for e in end_str.split(",")]
    now = datetime.now().time()

    for s_val, e_val in zip(starts, ends):
        try:
            s_parse = s_val + ":00" if len(s_val) == 5 else s_val
            e_parse = e_val + ":00" if len(e_val) == 5 else e_val

            start = datetime.strptime(s_parse, "%H:%M:%S").time()
            end = datetime.strptime(e_parse, "%H:%M:%S").time()

            if start <= end:
                if start <= now <= end:
                    return True
            else:  # Handles overnight schedule windows (e.g., 22:00 to 06:00)
                if now >= start or now <= end:
                    return True
        except Exception:
            continue

    return False


def create_frame_and_header_shms(prefix: str, frame_shape: Tuple[int, int, int]) -> Tuple[str, str]:
    H, W, C = frame_shape
    frame_bytes = H * W * C

    for suffix in ["_frame", "_header"]:
        try:
            old = shared_memory.SharedMemory(name=f"{prefix}{suffix}")
            old.close()
            old.unlink()
        except FileNotFoundError:
            pass

    frame_shm = shared_memory.SharedMemory(name=f"{prefix}_frame", create=True, size=frame_bytes)
    header_shm = shared_memory.SharedMemory(name=f"{prefix}_header", create=True, size=8)

    os.chmod(f"/dev/shm/{prefix}_frame", 0o666)
    os.chmod(f"/dev/shm/{prefix}_header", 0o666)

    view = np.ndarray((H, W, C), dtype=np.uint8, buffer=frame_shm.buf)
    view[:] = 0

    hdr = np.ndarray((1,), dtype=np.uint64, buffer=header_shm.buf)
    hdr[0] = 0

    header_shm.close()
    frame_shm.close()

    return frame_shm.name, header_shm.name


def create_result_shm(stream_id: str, frame_shape: Tuple[int, int, int]) -> str:
    H, W, C = frame_shape
    frame_bytes = H * W * C
    shm_name = f"result_shm_{stream_id}"

    try:
        old_shm = shared_memory.SharedMemory(name=shm_name)
        old_shm.close()
        old_shm.unlink()
    except FileNotFoundError:
        pass

    result_shm = shared_memory.SharedMemory(name=shm_name, create=True, size=frame_bytes)
    os.chmod(f"/dev/shm/{shm_name}", 0o666)

    view = np.ndarray((H, W, C), dtype=np.uint8, buffer=result_shm.buf)
    view[:] = 0
    result_shm.close()

    return shm_name


def inference_worker_sharedmem(
    shm_map, header_map, shapes, yolo_result_queues, ocr_result_queues,
    pole_enabled, pole_polygon, debug, yolo_device, yolo_thresh, ocr_thresh, 
    yolo_model_path, batch_size, is_saving_images, image_storage_dir, 
    retention_hours, is_logging_data, log_file, db_enabled, lookup_path, 
    send_gui, tis_enabled, tis_ip, tis_port, result_shm_map, is_displaying_video, 
    batch_timeout=0.45, stop_event: Event = None
): 
    logger.info(f"Inference worker starting. model={yolo_model_path} device={yolo_device} batch_size={batch_size}")

    try:
        model = YOLO(yolo_model_path)
        if yolo_device and str(yolo_device).lower() == "cuda":
            model.to("cuda")
    except Exception:
        logger.exception("Failed to load YOLO model")
        return

    trackers = {}
    if sv is not None:
        trackers = {
            scn: sv.ByteTrack(
                track_activation_threshold=0.3,
                lost_track_buffer=30,
                minimum_matching_threshold=0.9,
                frame_rate=8
            )
            for scn in shm_map.keys()
        }

    frame_shms, header_shms, frame_views, header_bufs = {}, {}, {}, {}

    for scn, fname in shm_map.items():
        try:
            fshm = shared_memory.SharedMemory(name=fname)
            hshm = shared_memory.SharedMemory(name=header_map[scn])
            frame_shms[scn] = fshm
            header_shms[scn] = hshm
            H, W, C = shapes[scn]
            frame_views[scn] = (H, W, C, np.ndarray((H, W, C), dtype=np.uint8, buffer=fshm.buf))
            header_bufs[scn] = hshm.buf
        except Exception:
            logger.exception(f"[{scn}] SHM attach failed")

    result_shms, result_buffers = {}, {}
    for scn in shm_map.keys():
        try:
            shm_name = result_shm_map[scn]
            rshm = shared_memory.SharedMemory(name=shm_name)
            result_shms[scn] = rshm
            result_buffers[scn] = np.ndarray((640, 640, 3), dtype=np.uint8, buffer=rshm.buf)
        except Exception:
            logger.exception(f"[{scn}] Result SHM attach failed")

    last_processed_seq: Dict[str, int] = {scn: 0 for scn in shm_map.keys()}
    config_out = ConfigParser()

    try:
        frame_map = {}
        last_cleanup_time = 0
        cleanup_interval = 600
        sleep_t = 0.060

        while True:
            loop_start = time.time()

            if stop_event is not None and stop_event.is_set():
                break

            now = time.time()
            if now - last_cleanup_time > cleanup_interval:
                try:
                    requirements.cleanup_old_images(image_storage_dir, retention_hours)
                    last_cleanup_time = now
                except Exception:
                    logger.exception("cleanup_old_images failed")

            # Refresh INI config dynamically to read active is_process flags
            config_out.read("/var/pnayanam/pnayanam.ini")

            batch_frames = []
            batch_scns = []
            
            for scn in list(shm_map.keys()):
                if len(batch_frames) >= batch_size:
                    break
                if scn not in frame_views:
                    continue

                scn_clean = scn.strip()
                cam_cfg = config_out[scn_clean] if scn_clean in config_out else {}

                # Condition 1: Check if camera processing is enabled
                is_process_val = cam_cfg.get("is_process", "False").lower() in ("true", "1")

                # Condition 2: Check if current time falls within active time intervals
                start_time_cfg = cam_cfg.get("start_time", "00:00")
                end_time_cfg = cam_cfg.get("end_time", "23:59")
                in_time_window = is_time_within_intervals(start_time_cfg, end_time_cfg)

                # Skip YOLO inference if either condition is False, and reset last sequence counter
                if not (is_process_val and in_time_window):
                    last_processed_seq[scn] = 0
                    continue

                H, W, C, view = frame_views[scn]
                hbuf = header_bufs[scn]

                seq1 = struct.unpack_from("<Q", hbuf, 0)[0]

                # Skip if offline (0), writing frame (odd), or frame has not changed
                if seq1 == 0 or (seq1 & 1) or (seq1 == last_processed_seq[scn]):
                    continue

                try:
                    frame = view.copy()
                except Exception:
                    continue

                seq2 = struct.unpack_from("<Q", hbuf, 0)[0]
                if seq1 != seq2 or (seq2 & 1):
                    continue

                last_processed_seq[scn] = seq1
                batch_frames.append(frame)
                batch_scns.append(scn)

            if not batch_frames:
                time.sleep(0.01)
                continue

            try:
                results = model(batch_frames, conf=yolo_thresh, iou=0.45, imgsz=640, verbose=debug)
            except Exception:
                logger.exception("YOLO Inference failed")
                continue

            for frame, res_obj, scn in zip(batch_frames, results, batch_scns):
                frame_map[scn] = frame
                current_ids = set()

                if sv is None or scn not in trackers:
                    continue

                try:
                    detections = sv.Detections.from_ultralytics(res_obj)
                    tracked_detections = trackers[scn].update_with_detections(detections)
                except Exception:
                    logger.exception(f"Tracking error for {scn}")
                    continue

                if tracked_detections is None:
                    continue

                person_count = 0

                for i in range(len(tracked_detections.xyxy)):
                    class_name = detections.data.get("class_name", [None] * len(tracked_detections.xyxy))[i]
                    if class_name != "person": 
                        continue

                    person_count += 1
                    x1, y1, x2, y2 = map(int, tracked_detections.xyxy[i])
                    bbox = (x1, y1, x2, y2)

                    if tracked_detections.tracker_id is None or tracked_detections.tracker_id[i] is None: 
                        continue

                    tracker_id = int(tracked_detections.tracker_id[i])
                    current_ids.add(tracker_id)

                    x1, y1 = max(0, x1), max(0, y1)
                    x2, y2 = min(frame.shape[1], x2), min(frame.shape[0], y2)

                    if x2 <= x1 or y2 <= y1:
                        continue

                    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                    label = f"Person ID:{tracker_id}"
                    cv2.putText(frame, label, (x1, max(y1 - 10, 15)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

                    if requirements.is_new_tracker(tracker_id):
                        scn_clean = scn.strip()
                        cam_cfg = config_out[scn_clean] if scn_clean in config_out else {}
                        site = cam_cfg.get("site_name", cam_cfg.get("site", "")).strip('"')
                        camera_name = cam_cfg.get("name", "").strip('"')

                        requirements.add_tracker(tracker_id)

                        frame_image_path = None
                        if is_saving_images == 1:
                            try:
                                frame_image_path = requirements.save_images(frame, image_storage_dir, str(scn), bbox)
                            except Exception:
                                logger.exception(f"[{scn}] Image save failed")

                        encoded_img = ""
                        try:
                            if frame_image_path:
                                encoded_img = requirements.encode_image(frame_image_path)
                        except Exception:
                            logger.exception(f"[{scn}] Base64 encoding failed")

                        send_person_alert(frame, person_count, scn, camera_name, site)

                        if send_gui == 1:
                            try:
                                iso_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                formatted_image_url = f"data:image/jpeg;base64,{encoded_img}" if encoded_img else ""
                                
                                payload_gui = {
                                    "alert_type_name": "Human Detection",
                                    "camera_scn": scn,
                                    "camera_name": camera_name,
                                    "site_name": site,
                                    "created_at": iso_timestamp,
                                    "image_url": formatted_image_url,
                                }
                                requirements.send_to_gui(payload_gui)
                            except Exception as e:
                                logger.exception(f"[{scn}] send_to_gui failed: {e}")

                display_frame = frame_map.get(scn, frame)
                try:
                    result_buffers[scn][:] = cv2.resize(display_frame, (640, 640))
                except Exception:
                    pass

                if is_displaying_video:
                    cv2.imshow(f"Detection Stream - {scn}", cv2.resize(display_frame, (1280, 720)))
                    cv2.waitKey(1)

            elapsed = time.time() - loop_start
            remaining = sleep_t - elapsed
            if remaining > 0:
                time.sleep(remaining)

    except KeyboardInterrupt:
        logger.info("Inference worker stopped via KeyboardInterrupt")
    finally:
        cv2.destroyAllWindows()
        for shm in list(frame_shms.values()) + list(header_shms.values()) + list(result_shms.values()):
            try:
                shm.close()
            except Exception:
                pass
        logger.info("Inference worker exiting")


def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))

    config_in = ConfigParser()
    config_out = ConfigParser()
    
    config_path = os.path.join(base_dir, "resources", "config.ini")
    config_in.read(config_path)
    config_out.read('/var/pnayanam/pnayanam.ini')

    send_gui = config_out.getint("GUI", "send_gui", fallback=1)
    tis_enabled = config_out.getboolean('tis', 'enable_tis_server', fallback=False)
    tis_ip = config_out.get('tis', 'tis_ip', fallback="127.0.0.1")
    tis_port = config_out.getint('tis', 'tis_port', fallback=8080)

    is_saving_images = config_in.getint('images_directory', 'is_saving_images', fallback=1)
    image_storage_dir = os.path.abspath(os.path.join(base_dir, config_in.get('images_directory', 'image_storage_dir', fallback="alert_images")))

    retention_hours = config_in.getint('Retention_Hours', 'retention_hours', fallback=48)
    is_logging_data = config_in.getint('logging_directory', 'is_logging_data', fallback=1)
    log_file = os.path.abspath(os.path.join(base_dir, config_in.get('logging_directory', 'log_file', fallback="person_detection.log")))

    yolo_thresh = config_in.getfloat('model_Confidence_thresholds', 'yolo_thresh', fallback=0.45)
    ocr_thresh = config_in.getfloat('model_Confidence_thresholds', 'ocr_thresh', fallback=0.50)

    coordinates_str = config_in.get('pole_polygon', 'coordinates_pole', fallback="[]")
    pole_enabled = config_in.getboolean('pole_polygon', 'pole_enable', fallback=False)   
    pole_polygon = ast.literal_eval(coordinates_str)

    is_displaying_video = config_in.getboolean('video_results', 'is_displaying_video', fallback=False)

    db_enabled = config_in.getint("DB_lookup_Enabled", "lookup_enabled", fallback=0)
    lookup_path = os.path.abspath(os.path.join(base_dir, config_in.get("DB_lookup_Enabled", "lookup_path", fallback="users.db")))

    debug = config_in.getboolean('logs_debug', 'debug', fallback=False)

    yolo_model_path = os.path.join(base_dir, config_in.get("YOLO_Model", "yolo_model_path", fallback="yolov8n.pt"))
    yolo_device = config_in.get("yolo_model_device", "yolo_device", fallback="cpu")

    camera_scns = [s.strip() for s in config_out.get("camera_scn", "scns").split(",") if s.strip()]

    batch_size = config_in.getint("runtime", "batch_size", fallback=4)
    batch_timeout = config_in.getfloat("runtime", "batch_timeout", fallback=0.05)
    queue_maxsize = config_in.getint("runtime", "queue_maxsize", fallback=20)
    supervisor_respawn = config_in.getboolean("runtime", "supervisor_respawn", fallback=True)

    FRAME_H = config_in.getint("shared_memory", "FRAME_H", fallback=640)
    FRAME_W = config_in.getint("shared_memory", "FRAME_w", fallback=640)
    FRAME_SHAPE = (FRAME_H, FRAME_W, 3)

    shm_map: Dict[str, str] = {}
    shapes: Dict[str, tuple] = {}
    header_map: Dict[str, str] = {}

    for scn in camera_scns:
        frame_shm_name, header_shm_name = create_frame_and_header_shms(scn + "_shm", FRAME_SHAPE)
        shm_map[scn] = frame_shm_name
        header_map[scn] = header_shm_name
        shapes[scn] = FRAME_SHAPE

    result_shm_map = {scn: create_result_shm(scn, (640, 640, 3)) for scn in camera_scns}
    stop_event = mp.Event()
    processes: Dict[str, mp.Process] = {}

    yolo_result_queues = {scn: mp.Queue(maxsize=queue_maxsize) for scn in camera_scns}
    ocr_result_queues = {scn: mp.Queue(maxsize=queue_maxsize) for scn in camera_scns}

    camera_stop_event = threading.Event()

    camera_threads = start_camera_readers(
        camera_scns, send_gui, debug, shm_map, header_map, FRAME_SHAPE, 
        5, camera_stop_event, TARGET_FPS=8
    )

    inf_proc = mp.Process(
        target=inference_worker_sharedmem, 
        args=(
            shm_map, header_map, shapes, yolo_result_queues, ocr_result_queues, 
            pole_enabled, pole_polygon, debug, yolo_device, yolo_thresh, ocr_thresh, 
            yolo_model_path, batch_size, is_saving_images, image_storage_dir, 
            retention_hours, is_logging_data, log_file, db_enabled, lookup_path, 
            send_gui, tis_enabled, tis_ip, tis_port, result_shm_map, is_displaying_video,
            batch_timeout, stop_event
        ), 
        daemon=True
    )
    inf_proc.start()
    processes["inference"] = inf_proc

    try:
        while True:
            time.sleep(1)
            if restart_event.is_set():
                logger.info("Restart triggered by config update...")
                restart_event.clear()
                camera_stop_event.set()

                for t in camera_threads:
                    t.join(timeout=2)

                stop_event.set()
                for proc in processes.values():
                    if proc.is_alive():
                        proc.terminate()

                time.sleep(0.5)
                os.execv(sys.executable, [sys.executable] + sys.argv)
                sys.exit(0)

            for name, proc in list(processes.items()):
                if not proc.is_alive() and supervisor_respawn:
                    logger.warning(f"Respawning dead process: {name}")
                    newp = mp.Process(
                        target=inference_worker_sharedmem, 
                        args=(
                            shm_map, header_map, shapes, yolo_result_queues, ocr_result_queues, 
                            pole_enabled, pole_polygon, debug, yolo_device, yolo_thresh, ocr_thresh, 
                            yolo_model_path, batch_size, is_saving_images, image_storage_dir, 
                            retention_hours, is_logging_data, log_file, db_enabled, lookup_path, 
                            send_gui, tis_enabled, tis_ip, tis_port, result_shm_map, is_displaying_video,
                            batch_timeout, stop_event
                        ), 
                        daemon=True
                    )
                    newp.start()
                    processes[name] = newp

    except KeyboardInterrupt:
        logger.info("Pipeline stopping...")
        stop_event.set()
    finally:
        stop_event.set()
        camera_stop_event.set()
        for t in camera_threads:
            try:
                t.join(timeout=1)
            except Exception:
                pass
        for p in processes.values():
            try:
                p.terminate()
            except Exception:
                pass


def start_subscriber_thread():
    t = threading.Thread(target=subscriber_monitor, daemon=True)
    t.start()
    logger.info("SUBSCRIBER THREAD ACTIVATED")


def start_gpu_resources_thread():
    t = threading.Thread(target=edge_health_monitor, daemon=True)
    t.start()
    logger.info("GPU MONITOR THREAD ACTIVATED")


def start_camDetails_thread():
    t = threading.Thread(target=camera_monitor, daemon=True)
    t.start()
    logger.info("CAMERA DETAILS THREAD ACTIVATED")


if __name__ == "__main__":
    start_camDetails_thread()
    start_subscriber_thread()
    start_gpu_resources_thread()
    start_solar_camera_collection_thread()
    main()