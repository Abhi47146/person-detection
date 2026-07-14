"""
Updated multi-camera inference pipeline using single-frame shared memory buffers per camera.

This file contains two modules combined for convenience:
1) multicam_inferencing_sharedmem: launcher - replaces your previous multicam_inferencing_v3.py
2) camera_processing_sharedmem: updated camera worker (based on camera_processing_v3.py) adjusted to read frames from shared memory instead of a manager.Queue

Key changes from your original code:
- Replaced per-camera manager.Queue frame_queues with per-camera SharedMemory single-frame buffers.
- Camera readers run as threads (I/O-bound, GIL released) and write latest frame into SharedMemory.
- Inference (YOLO) runs in a separate process, reads latest frames from SharedMemory, forms batches and runs model(frames).
- Detection results are sent through lightweight multiprocessing.Queue result_queues (small messages: (cam_id, detections, frame_id, ts)).
- Camera worker processes (camera_process_worker) now read frames directly from their SharedMemory buffer (no frame_queue argument). The rest of logic is preserved as much as possible.

USAGE:
- Edit CAM_PATHS and config-related sections as needed.
- Set yolo_model_path to your .pt model path (PyTorch/Ultralytics)
- Run: python3 multicam_sharedmem_version.py

Note: This is an integration template. Some helper modules (grpc_streamer, fog_detection, redis_event, autogen.*) were kept but not modified. Keep them on PYTHONPATH.
"""

# -----------------------------
# multicam_inferencing_sharedmem
# -----------------------------
from __future__ import annotations
import os
import sys
import time
import ast
import multiprocessing as mp
from datetime import datetime
from configparser import ConfigParser
from typing import Dict,Tuple
from multiprocessing import shared_memory, Event
import numpy as np
import threading
from ultralytics import YOLO
import supervision as sv
import cv2
import struct

from pnayanam.logger import Logger

from pnayanam.telegram_alert import send_person_alert, send_person_count_alert
from pnayanam.collect_subscribers import subscriber_monitor
from pnayanam.fetch_cam_details import camera_monitor, restart_event

from pnayanam.send_gpu_details import edge_health_monitor

from pnayanam.cam_reading import start_camera_readers

from pnayanam.pnayanam_utils import Requirements
requirements = Requirements()

logger = Logger("person_detection")

HEADER_FMT = "<QIII"   # seq:uint64, h:uint32, w:uint32, c:uint32
HEADER_SIZE = struct.calcsize(HEADER_FMT)

config_out = ConfigParser()
config_out.read("/var/pnayanam/pnayanam.ini")
scns = [scn.strip() for scn in config_out.get("camera_scn", "scns").split(",")]


def create_frame_and_header_shms(prefix: str, frame_shape: Tuple[int,int,int]) -> Tuple[str, str]:
    """Create frame shm (H*W*C bytes) and header shm (8 bytes) and initialize them."""
    H, W, C = frame_shape
    frame_bytes = H * W * C

    try:
        old = shared_memory.SharedMemory(name=f"{prefix}_frame")
        old.close()
        old.unlink()
    except FileNotFoundError:
        pass

    try:
        old = shared_memory.SharedMemory(name=f"{prefix}_header")
        old.close()
        old.unlink()
    except FileNotFoundError:
        pass
    

    frame_shm = shared_memory.SharedMemory(name=f"{prefix}_frame",create=True,size=frame_bytes)
    header_shm = shared_memory.SharedMemory(name=f"{prefix}_header",create=True,size=8)

    os.chmod(f"/dev/shm/{prefix}_frame", 0o666)
    os.chmod(f"/dev/shm/{prefix}_header", 0o666)

    # zero frame
    view = np.ndarray((H, W, C), dtype=np.uint8, buffer=frame_shm.buf)
    view[:] = 0
    # init header (uint64)
    hdr = np.ndarray((1,), dtype=np.uint64, buffer=header_shm.buf)
    hdr[0] = 0
    # close local handles
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
        logger.info(f"[OLD RESULT SHM REMOVED] {shm_name}")
    except FileNotFoundError:
        pass

    result_shm = shared_memory.SharedMemory(name=shm_name,create=True,size=frame_bytes)

    os.chmod(f"/dev/shm/{shm_name}", 0o666)

    view = np.ndarray((H, W, C),dtype=np.uint8,buffer=result_shm.buf)
    view[:] = 0
    result_shm.close()
    logger.info(f"[RESULT SHM READY] {shm_name}")

    return shm_name

def inference_worker_sharedmem(shm_map, header_map, shapes, yolo_result_queues, ocr_result_queues,
        pole_enabled, pole_polygon, debug, yolo_device, yolo_thresh, ocr_thresh, yolo_model_path, batch_size,
        is_saving_images, image_storage_dir, retention_hours, is_logging_data, log_file, db_enabled, lookup_path, 
        send_gui, tis_enabled, tis_ip, tis_port, result_shm_map, is_displaying_video, 
        batch_timeout=0.45, stop_event: Event = None, ): 
    """
    Optimized minimal inference worker:
    - attach to frame SHM and header SHM per camera
    - do a minimal seq check (odd => skip), copy whole frame buffer with .copy()
    - batch frames and call model(batch_frames)
    - send detections to result_queues
    """
    logger.info(
        f"Inference worker starting. "
        f"model={yolo_model_path} "
        f"device={yolo_device} "
        f"batch_size={batch_size}"
    )

    # load model once
    try:
        model = YOLO(yolo_model_path)
        if yolo_device and str(yolo_device).lower() == "cuda":
            model.to("cuda")
    except Exception:
        logger.exception("Failed to load model")
        return

    trackers = {
        scn: sv.ByteTrack(
            track_activation_threshold=0.3,
            lost_track_buffer=30,
            minimum_matching_threshold=0.9,
            frame_rate=8
        )
        for scn in shm_map.keys()
    }

    # Attach to SHMs
    frame_shms = {}
    header_shms = {}
    frame_views = {}
    header_bufs = {}

    for scn, fname in shm_map.items():
        try:
            # kind of safe step to recreate the memory if it is  lost and crashed.
            fshm = shared_memory.SharedMemory(name=fname)
            hshm = shared_memory.SharedMemory(name=header_map[scn])
            frame_shms[scn] = fshm
            header_shms[scn] = hshm
            H, W, C = shapes[scn]
            # view as (H,W,C) and use .copy() to read
            frame_views[scn] = (H, W, C, np.ndarray((H, W, C), dtype=np.uint8, buffer=fshm.buf))
            header_bufs[scn] = hshm.buf
        except Exception:
            logger.exception(f"[{scn}] SHM attach failed")

    result_shms = {}
    result_buffers = {}

    for scn in shm_map.keys():
        try:
            shm_name = result_shm_map[scn]
            rshm = shared_memory.SharedMemory(name=shm_name)
            result_shms[scn] = rshm

            result_buffers[scn] = np.ndarray((640, 640, 3),dtype=np.uint8,buffer=rshm.buf)
        except Exception:
            logger.exception(f"[{scn}] Result SHM attach failed")

    try:

        frame_map = {}

        last_cleanup_time = 0
        cleanup_interval = 600

        sleep_t = 0.060      # 60 ms

        last_count_alert_time = 0
        
        while True:
            loop_start = time.time()

            if stop_event is not None and stop_event.is_set():
                break

            # PERIODIC IMAGE CLEANUP
            now = time.time()
            if now - last_cleanup_time > cleanup_interval:
                try:
                    requirements.cleanup_old_images(image_storage_dir,retention_hours)
                    last_cleanup_time = now
                except Exception:
                    logger.exception("cleanup_old_images failed")

            
            # 1. THIS LOOP MAKES BATCHES OF FRAMES FOR BATCH INFERENCING!
            
            batch_frames = []
            batch_scns = []
            # round-robin collect frames
            a=time.time()
            for scn in list(shm_map.keys()):
                if len(batch_frames) >= batch_size:
                    break
                if scn not in frame_views:
                    continue

                H, W, C, view = frame_views[scn]
                hbuf = header_bufs[scn]

                # minimal seq-check: read seq, if odd skip, copy frame, re-read seq and accept only if unchanged
                seq1 = struct.unpack_from("<Q", hbuf, 0)[0]
                if seq1 & 1:
                    continue
                # copy whole frame (fast C-level memcpy)
                try:
                    frame = view.copy()
                except Exception:
                    # fallback: skip camera
                    continue
                seq2 = struct.unpack_from("<Q", hbuf, 0)[0]
                if seq1 != seq2 or (seq2 & 1):
                    # unstable - skip
                    continue

                batch_frames.append(frame)
                batch_scns.append(scn)

            if not batch_frames:
                # small sleep to avoid tight spin
                #time.sleep(0.003)
                continue
            b=time.time()
            if debug:
                logger.info(
    f"Total time taken for reading {len(batch_frames)} frames from SHM: {(b - a) * 1000:.2f} ms"
)
            
            # 2. RUNNING YOLO BATCH INFERENCE AND FETCHING DETCETINO RESULTS!
            
            try:
                t1=time.time()
                # results = model(batch_frames)
                results = model(
                    batch_frames,
                    # device=0 if yolo_device == "cuda" else "cpu",
                    conf=yolo_thresh,
                    iou=0.45,
                    imgsz=640,  
                    half=True,
                    verbose=True
                    # verbose=False,
                )
                t2=time.time()
                if debug:
                    logger.info(
    f"Total time taken by YOLO model for {len(batch_frames)} frames: {(t2 - t1) * 1000:.2f} ms"
)
            except Exception:
                logger.exception("Model inference failed")
                for scn in batch_scns:
                    try:
                        yolo_result_queues[scn].put_nowait((None, None, None))
                    except Exception:
                        pass
                continue

                
                # MAINTAIN 60ms INTERVAL
                

                elapsed = time.time() - loop_start
                remaining = sleep_t - elapsed
                if remaining > 0:
                    time.sleep(remaining)

            
            # 3. DETECTIONS LOOP
            
            # result_queues[scn].put_nowait((frame, detections, time.time()))
            for frame, res_obj, scn in zip(batch_frames, results, batch_scns):

                frame_map[scn] = frame
                current_ids = set()

                try:
                    detections = (sv.Detections.from_ultralytics(res_obj) if 'sv' in globals() and sv is not None else None)
                    tracked_detections = trackers[scn].update_with_detections(detections)
                except Exception:
                    logger.exception(f"Conversion to Detections failed for {scn}")
                    detections = None

                if tracked_detections is None:
                    continue

                
                # 3.1 ITERATING DETECTIONS
                
                person_count = 0

                for i in range(len(tracked_detections.xyxy)):
                    person_count += 1

                    x1, y1, x2, y2 = map(int, tracked_detections.xyxy[i])
                    bbox = (x1, y1, x2, y2)

                    class_name = detections.data.get("class_name",[None]*len(tracked_detections.xyxy))[i]
                    if class_name != "person": 
                        continue

                    if tracked_detections.tracker_id is None: 
                        continue
                    if tracked_detections.tracker_id[i] is None: 
                        continue

                    tracker_id = int(tracked_detections.tracker_id[i])
                    current_ids.add(tracker_id)

                    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)

                    # safe clipping
                    x1 = max(0, x1)
                    y1 = max(0, y1)
                    x2 = min(frame.shape[1], x2)
                    y2 = min(frame.shape[0], y2)

                    if x2 <= x1 or y2 <= y1:
                        continue

                    # draw bbox
                    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                    # label text
                    label = f"{class_name} ID:{tracker_id}"
                    # place text slightly above bbox, if space not available put inside bbox
                    text_y = y1 - 10 if y1 - 10 > 10 else y1 + 20
                    cv2.putText( frame, label, (x1, text_y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2, cv2.LINE_AA)

                    if requirements.is_new_tracker(tracker_id):
                        scn = scn.strip()
                        cam_cfg = config_out[scn]
                        site = cam_cfg.get("site_name", "").strip('"')
                        camera_name = cam_cfg.get("name", "").strip('"')

                        a = time.time()
                        send_person_alert(frame, person_count, scn, camera_name, site)
                        alert = "Human Detection"
                        b = time.time()
                        logger.info(
                            f"Time taken to send the alert {(b-a)*1000:.2f} MilliSeconds "
                            f"for image: {frame.shape}..!"
                        )
                        requirements.add_tracker(tracker_id)

                        
                        # SAVE IMAGES
                        
                        frame_image_path = None
                        # person_image_path = None
                        if is_saving_images == 1:
                            try:
                                # vehicle_image_path, plate_image_path = requirements.save_images(
                                frame_image_path = requirements.save_images(
                                    frame,
                                    image_storage_dir,
                                    str(scn),
                                    bbox
                                )
                            except Exception:
                                logger.exception(f"[{scn}] save_images failed")

                        # ENCODE IMAGE!
                        encoded_img = ""
                        try:
                            if frame_image_path:
                                encoded_img = requirements.encode_image(frame_image_path)
                        except Exception:
                            logger.exception(f"[{scn}] encode_image failed")

                        # SEND GUI!
                        timestamp = datetime.now().strftime("%d-%m-%y %H:%M:%S")
                        if debug:
                            logger.info(f"send_to_gui flag:{send_gui}")
                        # lane_no = int(scn[-2:])
                        if send_gui == 1:
                            try:
                                payload_gui = {
                                    "alert_type_name": alert,
                                    "camera_scn": scn,
                                    "camera_name": camera_name,
                                    "site_name": site,
                                    "timestamp": timestamp,
                                    "image_url": encoded_img,
                                    # "image_url": "",
                                }
                                if debug: 
                                    logger.info("called for sendingh data")
                                requirements.send_to_gui(payload_gui)
                                if debug:
                                    logger.info("Done !")
                            except Exception as e:
                                logger.exception(f"[{scn}] send_to_gui failed: {e}")

                current_time = time.time()
                if (current_time-last_count_alert_time>=30) and (person_count>0):
                    send_person_count_alert(person_count)
                    last_count_alert_time = current_time

                
                # SEND TIS
                
                # SEDNING THE DATA TO THE TIS!
                # timestamp_tis = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
                # timestamp_ID = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
                # # rand_id = random.randint(1000, 9999)
                # messageid = f"{timestamp_ID}_{tracker_id}"

                # if debug:
                #     logger.info("send_to_tis flag:{send_tis}")
                # if tis_enabled == 1:
                #     try:
                #         payload_tis = {
                #                         "data": {
                #                             "messageid": messageid, 
                #                             "metadata": {
                #                                 "number_plate": plate_to_log,
                #                                 "image": "",
                #                                 "frame": "",
                #                                 "vehicle": "",
                #                                 "auth": False,
                #                                 "vtype": 1, 
                #                                 "source_id": 1,
                #                                 "camera_id": 1, 
                #                                 "hashed_number_plate": "",
                #                                 "is_entry": True,
                #                                 "client": 1, 
                #                                 "client_name": "Calicut Expressway Privated Limited",
                #                                 "node": 1,
                #                                 "node_name": "Northbound", 
                #                                 "site": 1, 
                #                                 "site_name": "Olavanna toll", 
                #                                 "timestamp": timestamp_tis,
                #                                 "speed": -1,
                #                                 "radar_speed": -1,
                #                                 "color": -1,
                #                                 "brand": -1,
                #                                 "class": -1,
                #                                 "category": -1,
                #                                 "direction": 0,
                #                                 "lane_id": 20185,
                #                                 "visibility": -1,
                #                                 "uri": "",
                #                                 "bbox": [[ 628, 392, 140, 37]],
                #                                 "violations": []
                #                             }
                #                         },
                #                         "type": "event"
                #                     }
                #         requirements.send_to_tis(payload_tis, tis_ip, tis_port)
                #         if debug:
                #             logger.info(f"Sent this data to TIS: {payload_tis}")
                #     except Exception as e:
                #         logger.info(e)

                
                # LOG ENTRY
                
                # if is_logging_data == 1:
                #     try:
                #         requirements.log_entry_once(
                #             log_file,
                #             timestamp,
                #             anpr_camera_scn,
                #             q_scn,
                #             plate_to_log,
                #             tis_enabled,
                #             tracker_id,
                #             encoded_img,
                #             debug,
                #             vehicle_image_path,
                #             plate_image_path
                #         )
                #     except Exception:
                #         logger.exception(f"[{q_scn}] log_entry_once failed")

                
                # DISPLAY FRAME! AND WRITE TO RESULT SHM!
                display_frame = frame_map.get(scn, frame)

                # WRITE TO RESULT SHM!
                annot_frame = cv2.resize(display_frame,(640, 640))
                try:
                    # if debug:
                        # logger.debug("Sending result frame to shm")
                    result_buffers[scn][:] = annot_frame
                    # if debug:
                        # logger.debug("Sent result frame to shm")
                except Exception:
                    logger.exception(f"[{scn}] Failed writing result SHM")

                
                # DISPLAY FRAME!
                if is_displaying_video is True:
                    shfr = cv2.resize(display_frame, (1280, 720))
                    cv2.imshow(f"Plate Crop - {scn}", shfr)
                    cv2.waitKey(1)

    except KeyboardInterrupt:
        logger.info("Inference worker KeyboardInterrupt")
    finally:
        cv2.destroyAllWindows()
        # close shms
        for shm in frame_shms.values():
            try:   
                shm.close()
            except Exception: 
                pass
        for shm in header_shms.values():
            try: 
                shm.close()
            except Exception: 
                pass
        for shm in result_shms.values():
            try:
                shm.close()
            except Exception:
                pass
        logger.info("Inference worker exiting")

def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))

    config_in = ConfigParser()
    config_out = ConfigParser()
    
    config_path = os.path.join(
        os.path.dirname(__file__),
        "resources",
        "config.ini"
    )
    config_in.read(config_path)
    config_out.read('/var/pnayanam/pnayanam.ini')
    logger.info("Loading config from: {config_path}")

    send_gui = config_out.getint("GUI", "send_gui")
    tis_enabled = config_out.getboolean('tis', 'enable_tis_server')
    tis_ip = config_out.get('tis', 'tis_ip')
    tis_port = config_out.getint('tis', 'tis_port')

    is_saving_images = config_in.getint('images_directory', 'is_saving_images')
    image_storage_dir = config_in.get('images_directory', 'image_storage_dir')
    image_storage_dir = os.path.abspath(os.path.join(base_dir, image_storage_dir))

    retention_hours = config_in.getint('Retention_Hours', 'retention_hours')
    is_logging_data = config_in.getint('logging_directory', 'is_logging_data')
    log_file = config_in.get('logging_directory', 'log_file')
    log_file = os.path.abspath(os.path.join(base_dir, log_file))

    yolo_thresh = config_in.getfloat('model_Confidence_thresholds', 'yolo_thresh')
    ocr_thresh = config_in.getfloat('model_Confidence_thresholds', 'ocr_thresh')

    coordinates_str = config_in.get('pole_polygon', 'coordinates_pole')
    pole_enabled = config_in.getboolean('pole_polygon', 'pole_enable')   
    coordinates = ast.literal_eval(coordinates_str)
    pole_polygon = coordinates

    is_displaying_video = config_in.getboolean('video_results', 'is_displaying_video')

    db_enabled = config_in.getint("DB_lookup_Enabled", "lookup_enabled")
    lookup_path = config_in.get("DB_lookup_Enabled", "lookup_path")
    lookup_path = os.path.abspath(os.path.join(base_dir, lookup_path))

    debug = config_in.getboolean('logs_debug','debug')

    yolo_model_path = config_in.get("YOLO_Model", "yolo_model_path")
    yolo_model_path = os.path.join(os.path.dirname(__file__), yolo_model_path)
    yolo_device = config_in.get("yolo_model_device", "yolo_device")

    camera_scns = [s.strip() for s in config_out.get("camera_scn", "scns").split(",") if s.strip()]

    # runtime params
    batch_size = config_in.getint("runtime", "batch_size", fallback=4)
    batch_timeout = config_in.getfloat("runtime", "batch_timeout", fallback=0.05)
    queue_maxsize = config_in.getint("runtime", "queue_maxsize", fallback=20)
    supervisor_respawn = config_in.getboolean("runtime", "supervisor_respawn", fallback=True)

    # FRAME specifics (as per user)
    FRAME_H = config_in.getint("shared_memory", "FRAME_H")
    FRAME_W = config_in.getint("shared_memory", "FRAME_w")
    FRAME_C = 3
    FRAME_SHAPE = (FRAME_H, FRAME_W, FRAME_C)
    FRAME_BYTES = FRAME_H * FRAME_W * FRAME_C
    logger.info(f"Frame Bytes used: {FRAME_BYTES}")

    
    # CREATE SHARED MEMORY PER CAMERA
    shm_map: Dict[str, str] = {}
    shapes: Dict[str, tuple] = {}
    header_map: Dict[str, str] = {}

    for scn in camera_scns:
        frame_shm_name, header_shm_name = create_frame_and_header_shms(scn + "_shm", FRAME_SHAPE)
        shm_map[scn] = frame_shm_name
        header_map[scn] = header_shm_name

        shapes[scn] = FRAME_SHAPE

    reconnecting_max_attempts=5

    # CREATE RESULT DISPLAY SHMS
    result_shm_map = {}

    for scn in camera_scns:
        shm_name = create_result_shm(stream_id=scn,frame_shape=(640, 640, 3))
        result_shm_map[scn] = shm_name
        
    stop_event = mp.Event()
    processes: Dict[str, mp.Process] = {}

    yolo_result_queues: Dict[str, mp.Queue] = {scn: mp.Queue(maxsize=queue_maxsize) for scn in camera_scns}
    ocr_result_queues: Dict[str, mp.Queue] = {scn: mp.Queue(maxsize=queue_maxsize) for scn in camera_scns}

    camera_stop_event = threading.Event()

    camera_threads = start_camera_readers(camera_scns, send_gui, debug, shm_map, header_map, FRAME_SHAPE, reconnecting_max_attempts, camera_stop_event, TARGET_FPS=8)

    # start inference process
    inf_proc = mp.Process(target=inference_worker_sharedmem, 
                          args=(shm_map, header_map, shapes, yolo_result_queues, ocr_result_queues, 
                                pole_enabled, pole_polygon, debug, yolo_device, yolo_thresh, ocr_thresh, yolo_model_path, batch_size, 
                                is_saving_images, image_storage_dir, retention_hours, is_logging_data, log_file, db_enabled, lookup_path, 
                                send_gui, tis_enabled, tis_ip, tis_port, result_shm_map, is_displaying_video,
                                batch_timeout, stop_event), 
                          daemon=True)
    inf_proc.start()
    logger.info(f"Started inference process pid={inf_proc.pid}")
    processes["inference"]=inf_proc

    try:
        logger.info("Pipeline started. Press Ctrl+C to stop.")
        while True:
            time.sleep(1)
            if restart_event.is_set():
                logger.info("Restarting application...")

                restart_event.clear()

                logger.info("Stopping camera reader threads...")
                # 1. Stop camera reader threads
                camera_stop_event.set()
                for t in camera_threads:
                    t.join(timeout=2)
                logger.info("All camera reader threads stopped.")

                # 2. Stop inference
                stop_event.set()

                # Give inference a chance to exit gracefully
                for proc in processes.values():
                    proc.join(timeout=3)

                # Force kill only if still alive
                for proc in processes.values():
                    if proc.is_alive():
                        proc.terminate()
                        proc.join()

                # 3. Unlink SHMs
                # Cleanup frame SHM
                for shm_name in shm_map.values():
                    try:
                        shm = shared_memory.SharedMemory(name=shm_name)
                        shm.close()
                        shm.unlink()
                    except Exception:
                        pass
                # Cleanup header SHM
                for shm_name in header_map.values():
                    try:
                        shm = shared_memory.SharedMemory(name=shm_name)
                        shm.close()
                        shm.unlink()
                    except Exception:
                        pass
                # Cleanup result SHM
                for shm_name in result_shm_map.values():
                    try:
                        shm = shared_memory.SharedMemory(name=shm_name)
                        shm.close()
                        shm.unlink()
                    except Exception:
                        pass

                # 4. Restart
                time.sleep(0.5)
                os.execv(sys.executable, [sys.executable] + sys.argv)
                sys.exit(0)

            for name, proc in list(processes.items()):
                if not proc.is_alive():
                    logger.warning(f"ProcessID Died for {name}")
                    if supervisor_respawn:
                        # respawn camera processes only (inference can also be respawned)
                        if name == "inference":
                            logger.info("Respawning inference worker")
                            newp = mp.Process(target=inference_worker_sharedmem, 
                                              args=(shm_map, header_map, shapes, yolo_result_queues, ocr_result_queues, 
                                                    pole_enabled, pole_polygon, debug, yolo_device, yolo_thresh, ocr_thresh, yolo_model_path, batch_size, 
                                                    is_saving_images, image_storage_dir, retention_hours, is_logging_data, log_file, db_enabled, lookup_path, 
                                                    send_gui, tis_enabled, tis_ip, tis_port, result_shm_map, is_displaying_video,
                                                    batch_timeout, stop_event), 
                                              daemon=True)
                            newp.start()
                            processes["inference"] = newp
                        else:
                            logger.info(f"Unknown process type for respawn: {name}")
                    else:
                        logger.warning(f"Supervisor respawn disabled. Not respawning {name}")

    except KeyboardInterrupt:
        logger.info("Stopping pipeline...")
        stop_event.set()
    finally:
        logger.info("\nStopping all processes...")

        # SIGNAL STOP
        stop_event.set()

        camera_stop_event.set()
        for t in camera_threads:
            try:
                t.join(timeout=2)
            except Exception:
                pass

        # TERMINATE CHILD PROCESSES
        for name, p in processes.items():
            try:
                if p.is_alive():
                    logger.info(f"Terminating: {name}")
                    p.terminate()
            except Exception:
                pass

        # JOIN CHILD PROCESSES
        for name, p in processes.items():
            try:
                p.join(timeout=3)
                logger.info(f"Joined: {name}")
            except Exception:
                pass

        # DESTROY OPENCV WINDOWS
        try:
            cv2.destroyAllWindows()
        except Exception:
            pass

        # CLEAN FRAME SHMS
        for name in shm_map.values():
            try:
                shm = shared_memory.SharedMemory(name=name)
                shm.close()
                shm.unlink()
            except Exception:
                pass

        # CLEAN HEADER SHMS
        for name in header_map.values():
            try:
                shm = shared_memory.SharedMemory(name=name)
                shm.close()
                shm.unlink()
            except Exception:
                pass

        # CLEAN RESULT SHMS
        for shm_name in result_shm_map.values():
            try:
                shm = shared_memory.SharedMemory(name=shm_name)
                shm.close()
                shm.unlink()
            except Exception:
                pass

        logger.info("Cleaned up. Exiting.")

def start_subscriber_thread():
    subscriber_thread = threading.Thread(
        target=subscriber_monitor,
        daemon=True
    )

    subscriber_thread.start()
    logger.info("SUBSCRIBER THREAD IS ACTIVATED")

def start_gpu_resources_thread():
    gpu_thread = threading.Thread(
        target=edge_health_monitor,
        daemon=True
    )

    gpu_thread.start()
    logger.info("GPU RESOURCE MONITOR THREAD IS ACTIVATED")

def start_camDetails_thread():
    camera_thread = threading.Thread(
        target=camera_monitor,
        daemon=True
    )

    camera_thread.start()
    logger.info("CAMERA DETAILS THREAD IS ACTIVATED")