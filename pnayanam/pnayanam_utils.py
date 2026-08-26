import base64
import cv2
import numpy as np
import os
import re
import socket
import shutil
import json
import supervision as sv
import time
import sqlite3
from configparser import ConfigParser
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from fastapi import HTTPException
from pnayanam.grafana import Grafana
from shapely.geometry import Polygon, box
import requests

class Requirements:
    def __init__(self, base_dir="alert_images"):

        from pnayanam.logger import Logger
        self.logger = Logger("pnayanam_utils")

        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        
        self.TCP_READ_TIMEOUT = int(os.environ.get('IA_TCP_READ_TIMEOUT', 20))

        self.logged_vehicles = set()
        self.line_color = (255, 0, 0)      
        self.text_color = (0, 0, 0)      
        self.bg_color = (255, 255, 255)   
        self.thickness = 2
        self.font = cv2.FONT_HERSHEY_SIMPLEX
        self.font_scale = 0.9
        self.font_thickness = 2

        self.base_dir = base_dir
        self.alerted_tracker_ids = set()
        self.last_cleanup_time = datetime.now()

        self.config_in = ConfigParser()
        self.config_out = ConfigParser()

        self.config_path = os.path.join(
            os.path.dirname(__file__),
            "resources",
            "config.ini"
        )
        self.config_in.read(self.config_path)

        self.config_out.read('/var/pnayanam/pnayanam.ini')

        self.enable_tis_server = self.config_out.getint('tis', 'enable_tis_server')
        self.tis_ip = self.config_out.get('tis', 'tis_ip')
        self.tis_port = self.config_out.getint('tis', 'tis_port')
        self.retry_interval = self.config_out.getint('tis', 'retry_interval')
        self.client = None

        self.send_to_grafana = self.config_in.getint('GRAFANA', 'send_to_grafana')

        self.db_enabled = self.config_in.getint("DB_lookup_Enabled", "lookup_enabled")
        self.lookup_path = os.path.abspath(os.path.join(self.base_dir, self.config_in.get("DB_lookup_Enabled", "lookup_path")))

        self.DB_PATH = os.path.join(os.path.dirname(__file__), "users.db")

        self.protocol = self.config_out.get(
            "gui_ip_address",
            "protocol",
            fallback="http"
        )

        self.gui_ip = self.config_out.get(
            "gui_ip_address",
            "ip_address",
            fallback="127.0.0.1"
        )

        self.gui_port = self.config_out.get(
            "gui_ip_address",
            "port",
            fallback="8000"
        )

        self.alerts_url = (
            f"{self.protocol}://{self.gui_ip}:{self.gui_port}/api/alerts"
        )

        self.debug = self.config_in.getboolean('logs_debug','debug')

        # self.send_to_grafana = self.config.getint('GRAFANA', 'send_to_grafana')
        
        if self.send_to_grafana == 1:
            self.grafana = Grafana(configfile="config_in.ini", logger=self.logger)
            self.logger.info("Initialized Grafana for alerting")
        else:
            self.logger.info("Grafana alerting is disabled (grafana=0 in config_in.ini)")

    def load_database(self):
        try:
            with open(self.database_file, 'r') as file:
                return [line.strip() for line in file if line.strip()]
        except FileNotFoundError:
            self.logger.error(f"Database file not found: {self.database_file}")
            if self.send_to_grafana == 1:
                self.send_grafana_alert(scn="requirements", alert_type="data_mismatch", severity="high",
                                    desc=f"Database file not found: {self.database_file}")
            return []

    def ensure_directory_exists(self, directory: str):
        try:
            directory = os.path.abspath(os.path.join(self.base_dir, directory)) if directory.startswith('i-phalakam/') else directory
            os.makedirs(directory, exist_ok=True)
        except Exception as e:
            self.logger.error(f"Failed to create directory {directory}: {e}")
            if self.send_to_grafana == 1:
                self.send_grafana_alert(scn="requirements", alert_type="data_mismatch", severity="high",
                                    device=directory, desc=f"Failed to create directory: {e}")
            raise
    
    def clean_text(self, text):
        if not isinstance(text, str):
            self.logger.warning(f"Non-string input to clean_text: {text} ({type(text)})")
            if self.send_to_grafana == 1:
                self.send_grafana_alert(scn="requirements", alert_type="data_mismatch", severity="medium",
                                    desc=f"Non-string input to clean_text: {type(text)}")
            return ""
        # reg_num = re.sub(r'[^a-zA-Z0-9↑]', '', text)
        reg_num = re.sub(r'[^a-zA-Z0-9↑]', '', text)
        reg_num = str(reg_num).strip()
        return reg_num.upper()
    
    def reconnect_to_camera(self, source_path, logger):
        retry_count = 0
        max_retries = 5
        source_path = os.path.abspath(os.path.join(self.base_dir, source_path)) if not source_path.startswith('rtsp://') else source_path
        while retry_count < max_retries:
            try:
                video_capture = cv2.VideoCapture(source_path)
                if video_capture.isOpened():
                    self.logger.debug(f"Successfully reconnected to camera {source_path}")
                    return video_capture
                else:
                    self.logger.error(f"Failed to reconnect to camera {source_path}. Retrying...")
                    if self.send_to_grafana == 1:
                        self.send_grafana_alert(scn="requirements", alert_type="device_offline", severity="high",
                                            device=source_path, desc="Failed to reconnect to camera")
            except Exception as e:
                self.logger.error(f"Error reconnecting to camera {source_path}: {e}")
                if self.send_to_grafana == 1:
                    self.send_grafana_alert(scn="requirements", alert_type="device_offline", severity="high",
                                        device=source_path, desc=f"Error reconnecting to camera: {e}")
            retry_count += 1
            time.sleep(5)
        self.logger.error(f"Max retries reached for camera {source_path}")
        if self.send_to_grafana == 1:
            self.send_grafana_alert(scn="requirements", alert_type="device_offline", severity="high",
                                device=source_path, desc="Max retries reached for camera reconnection")
        raise Exception(f"Failed to reconnect to camera {source_path} after {max_retries} attempts")

    def is_similar_plate(self, plate1: str, plate2: str, threshold: float = 0.8) -> bool:
        if not isinstance(plate1, str) or not isinstance(plate2, str):
            self.logger.warning(f"Non-string plate input to is_similar_plate: plate1={plate1} ({type(plate1)}), plate2={plate2} ({type(plate2)})")
            if self.send_to_grafana == 1:
                self.send_grafana_alert(scn="requirements", alert_type="data_mismatch", severity="medium",
                                    desc="Non-string plate input to is_similar_plate")
            return False
        plate1 = str(plate1)
        plate2 = str(plate2)
        similarity = SequenceMatcher(None, plate1, plate2).ratio()
        return similarity >= threshold

    def process_detections(self, frame: np.ndarray, detections: sv.Detections, index: int) -> np.ndarray:
        if index < len(detections.xyxy):
            x1, y1, x2, y2 = map(int, detections.xyxy[index])
            h, w, _ = frame.shape
            x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
            cropped_plate = frame[y1:y2, x1:x2]
            if cropped_plate.size > 0:
                return cropped_plate
            self.logger.warning("Invalid cropped plate region")
            if self.send_to_grafana == 1:
                self.send_grafana_alert(scn="requirements", alert_type="data_mismatch", severity="medium",
                                    desc="Invalid cropped plate region")
        return None

    # def encode_image(self, plate_img_path):
    #     # Check if file exists
    #     if not os.path.exists(plate_img_path):
    #         self.logger.info(f"[DEBUG] File does not exist: {plate_img_path}")
    #         return None

    #     # Read the image
    #     img = cv2.imread(plate_img_path)
    #     if img is None:
    #         self.logger.info(f"[DEBUG] Failed to load image: {plate_img_path}")
    #         return None

    #     # Encode image to PNG bytes
    #     _, buffer = cv2.imencode(".png", img)
    #     img_b64 = base64.b64encode(buffer).decode("utf-8")

    #     return img_b64

    def encode_image(self, image):
        if isinstance(image, str):
            if not os.path.exists(image):
                return ""

            with open(image, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")

        elif isinstance(image, np.ndarray):
            success, buffer = cv2.imencode(".jpg", image)

            if not success:
                return ""

            return base64.b64encode(buffer).decode("utf-8")

        return ""

    def is_crossing_line(self, bbox, line_start, line_end):
        x1, y1, x2, y2 = bbox
        bbox_center_y = (y1 + y2) // 2
        res = bbox_center_y > line_start # for ORR. CEPL
        # res = bbox_center_y < line_start # for MLFF
        return res

    def log_entry_once(self, log_file: str, timestamp: str, anpr_camera_scn: str, 
                   lane_num: int, reg_num: str, tis_enabled, tracker_id, encoded_img, debug,
                   vehicle_image_path, plate_image_path):  # use this tracker_id for storing unique logs
        
        try:
            log_file = os.path.abspath(os.path.join(self.base_dir, log_file)) if log_file.startswith('i-phalakam/') else log_file
            self.ensure_directory_exists(os.path.dirname(log_file))
        except Exception as e:
            if self.send_to_grafana == 1:
                self.send_grafana_alert(scn=anpr_camera_scn, alert_type="data_mismatch", severity="high",
                                    device=log_file, desc=f"Failed to create log directory: {e}")
            raise

        # Check if the vehicle is already logged
        if reg_num in self.logged_vehicles:
            return  # Skip logging if the vehicle is already logged

        # If the log file does not exist, create it with a header
        if not os.path.exists(log_file):
            try:
                with open(log_file, "w") as log_file_handle:
                    log_file_handle.write(
                        # "record_scn,tracker_id,timestamp,anpr_camera_scn,lane_num,reg_num\n"
                        "record_scn,timestamp,anpr_camera_scn,lane_num,reg_num\n"
                    )
                if debug:
                    self.logger.warning(f"Log file created: {log_file}")
                record_scn = 1
            except Exception as e:
                self.logger.error(f"Error creating log file {log_file}: {e}")
                raise
        else:
            # Read the last record_scn from the file
            try:
                with open(log_file, "r") as f:
                    lines = f.readlines()
                    if len(lines) <= 1:
                        record_scn = 1
                    else:
                        last_line = lines[-1]
                        last_record_scn = last_line.split(",")[0]
                        last_record_scn = last_line.split(",")[1]
                        try:
                            record_scn = int(last_record_scn) + 1
                        except ValueError:
                            record_scn = 1
            except Exception as e:
                self.logger.error(f"Error reading log file for record_scn: {e}")

        # Write the entry to the log file
        try:
            with open(log_file, "a") as log_file_handle:
                # STORE IN LOG FILE
                log_file_handle.write(
                    # f"{record_scn},{tracker_id},{timestamp},{anpr_camera_scn},{lane_num},{reg_num}\n"
                    f"{record_scn},{timestamp},{anpr_camera_scn},{lane_num},{reg_num}\n"
                )
        except Exception as e:
            self.logger.error(f"Error writing to log file {log_file}: {e}")
            raise

        self.logged_vehicles.add(reg_num)

        if debug:
            # self.logger.warning(f"Logged entry: {record_scn}, {tracker_id}, {timestamp}, {anpr_camera_scn}, {lane_num}, {reg_num}, {encoded_img}")
            self.logger.warning(f"Logged entry: {record_scn}, {tracker_id}, {timestamp}, {anpr_camera_scn}, {lane_num}, {reg_num}")

    def send_to_gui(self, payload):
        try:
            response = requests.post(
                f"http://{self.gui_ip}:8000/api/alerts",
                json=payload,
                timeout=(5, 20)
            )
            response.raise_for_status()
            if self.debug:
                # self.logger.info(f"GUI data sent successfully to: {self.gui_ip} with PAYLOAD: {payload}")
                self.logger.info(f"GUI data sent successfully to: {self.gui_ip} with PAYLOAD: ")
            if response.status_code == 200:
                self.logger.info("Sent live data to GUI")
        except Exception as e:
            # self.logger.error(f"ERROR SENDING PAYLOAD TO GUI..! {payload}: {e}")
            self.logger.error(f"ERROR SENDING PAYLOAD TO GUI..!: {e}")

    def update_camera_status_api(self, scn, status):
        """Updates camera status by camera_scn via PATCH endpoint."""
        try:
            url = f"http://{self.gui_ip}:8000/api/cameras/status/{scn}"
            payload = {"status": status}
            response = requests.patch(url, json=payload, timeout=3)

            if response.status_code == 200:
                self.logger.info(f"[{scn}] Camera status successfully updated to '{status}'.")
            else:
                self.logger.warning(
                    f"[{scn}] Failed status update. Code: {response.status_code}, Response: {response.text}"
                )
        except Exception as e:
            self.logger.error(f"[{scn}] Exception occurred while updating status: {e}")

    def send_to_tis(self, data, tis_ip, tis_port):
        for _ in range(5):
            try:
                if self.client is None:
                    self.client = socket.create_connection((tis_ip, tis_port), timeout=5)

                self.logger.info(f"About to Send data to TIS:{data}")

                self.client.sendall((json.dumps(data) + '\n').encode("utf-8"))
                self.logger.info(f"Sent to TIS: {data}")
                return

            except Exception as e:
                self.logger.error(f"TIS error: {e}")
                try:
                    if self.client:
                        self.client.close()
                except Exception as e:
                    pass

                self.client = None
                time.sleep(self.retry_interval)

        self.logger.error("TIS send failed after retries")

    def extract_plate_region(self, frame, bbox):
        """Safely extract number plate crop"""
        try:
            if frame is None or frame.size == 0:
                return None
            h, w = frame.shape[:2]
            x1, y1, x2, y2 = bbox
            x1 = max(0, int(x1))
            y1 = max(0, int(y1))
            x2 = min(w, int(x2))
            y2 = min(h, int(y2))
            # Critical validation
            if x2 <= x1 or y2 <= y1:
                self.logger.warning(f"Invalid bbox: {bbox}")
                return None
            plate_region = frame[y1:y2, x1:x2]
            if plate_region is None or plate_region.size == 0:
                self.logger.warning("Empty plate region extracted")
                return None
            return plate_region
        except Exception as e:
            self.logger.error(f"Error extracting plate region: {e}")
            return None

    def pole_plate_intersection(self, bounding_box, pole_polygon, debug):
        try:
            plate_box = box(*bounding_box)
            intersects = plate_box.intersects(Polygon(pole_polygon))
            if intersects:
                if self.send_to_grafana == 1:
                    self.send_grafana_alert(scn="requirements", alert_type="data_mismatch", severity="medium",
                                        desc="Plate obstructed by pole")
            # if debug:
            #     self.logger.info(f"Bounding box: {bounding_box}, Pole polygon: {pole_polygon}, Intersects: {intersects}")
            return intersects
        except Exception as e:
            self.logger.error(f"Error checking pole-plate intersection: {e}")
            if self.send_to_grafana == 1:
                self.send_grafana_alert(scn="requirements", alert_type="data_mismatch", severity="medium",
                                    desc=f"Error checking pole-plate intersection: {e}")
            return False
        
    def draw_imaginary_line(self, frame, line_start, line_end):
        try:
            cv2.line(frame, line_start, line_end, self.line_color, self.thickness)
        except Exception as e:
            self.logger.error(f"Error drawing imaginary line: {e}")
            if self.send_to_grafana == 1:
                self.send_grafana_alert(scn="requirements", alert_type="data_mismatch", severity="medium",
                                    desc=f"Error drawing imaginary line: {e}")

    def draw_imaginary_pole(self, frame, pole_polygon):
        try:
            pts = np.array(pole_polygon, np.int32).reshape((-1, 1, 2))
            cv2.polylines(frame, [pts], isClosed=True, color=self.line_color, thickness=self.thickness)
        except Exception as e:
            self.logger.error(f"Error drawing imaginary pole: {e}")
            if self.send_to_grafana == 1:
                self.send_grafana_alert(scn="requirements", alert_type="data_mismatch", severity="medium",
                                    desc=f"Error drawing imaginary pole: {e}")

    # DISPLAYING PLATE TEXT FOR VIDEO PROCESSING
    def displaying_plate(self, best_plate, best_conf, thresh_conf, bounding_box, pole_polygon, 
                         debug, frame, tracker_id):
        try:
            if best_plate and best_conf >= thresh_conf and not self.pole_plate_intersection(bounding_box, pole_polygon, debug):
                if len(best_plate) <= 10:
                    plate_to_display = best_plate 
                else:
                    plate_to_display = best_plate[:10]

                text = f"#{tracker_id}: {plate_to_display}, {best_conf:.2f}"

                (text_width, text_height), baseline = cv2.getTextSize(text, self.font, self.font_scale, self.font_thickness)

                text_x = int(bounding_box[0])
                text_y = int(bounding_box[1]) - 10

                cv2.rectangle(
                    frame,
                    (text_x, text_y - text_height - baseline),
                    (text_x + text_width, text_y + baseline),
                    self.bg_color,  
                    # cv2.FILLED
                    -1
                )
                cv2.putText(
                    frame,
                    text,
                    (text_x, text_y),
                    self.font,
                    self.font_scale,
                    self.text_color,  
                    self.font_thickness
                )
        except Exception as e:
            self.logger.error(f"Error displaying plate: {e}")

    # DISPLAYING VEHICLE BBOX WITH TRACKERID..!
    def displaying_vehicle(self, class_name, bounding_box, frame, tracker_id):
        try:
            if class_name != "Vehicle":
                return

            text = f"#{tracker_id}: {class_name}"

            # get text size
            (text_width, text_height), baseline = cv2.getTextSize(
                text,
                self.font,
                self.font_scale,
                self.font_thickness
            )

            x1, y1, x2, y2 = bounding_box

            # top-right inside bbox
            text_x = int(x2 - text_width - 5)
            text_y = int(y1 + text_height + 5)

            # prevent overflow
            if text_x < 0:
                text_x = 0

            # background rectangle
            cv2.rectangle(
                frame,
                (text_x, text_y - text_height - baseline),
                (text_x + text_width, text_y + baseline),
                self.bg_color,
                -1
            )

            # put text
            cv2.putText(
                frame,
                text,
                (text_x, text_y),
                self.font,
                self.font_scale,
                self.text_color,
                self.font_thickness
            )

        except Exception as e:
            self.logger.error(f"Error displaying vehicle tracker: {e}")

    def cleanup_old_images(self, image_storage_dir: str, retention_hours: int):
        try:
            image_storage_dir = os.path.abspath(os.path.join(self.base_dir, image_storage_dir)) if image_storage_dir.startswith('i-phalakam/') else image_storage_dir
            now = time.time()
            retention_seconds = retention_hours * 3600

            for root, dirs, files in os.walk(image_storage_dir):
                for filename in files:
                    file_path = os.path.join(root, filename)
                    
                    if not os.path.isfile(file_path):
                        continue  # Skip if it's not a regular file

                    file_age = now - os.path.getmtime(file_path)
                    
                    if file_age <= retention_seconds:
                        continue  # Skip if file is not old enough to delete

                    os.remove(file_path)
                    if self.debug:
                        self.logger.warning(f"Deleted old file: {file_path}")

        except Exception as e:
            self.logger.error(f"Error cleaning up old images: {e}")
            if self.send_to_grafana == 1:
                self.send_grafana_alert(scn="requirements", alert_type="data_mismatch", severity="high",
                                    device=image_storage_dir, desc=f"Error cleaning up old images: {e}")

    def save_images(self, frame: np.ndarray, image_storage_dir: str, scn: str, bbox):
        """Save full frame and cropped person image"""

        try:
            now = datetime.now()
            year = now.strftime("%Y")
            month = now.strftime("%m")
            day = now.strftime("%d")

            base_dir = os.path.join(image_storage_dir, year, month, day, str(scn))
            self.ensure_directory_exists(base_dir)

            # safer timestamp with microseconds to avoid filename clashes
            timestamp_str = now.strftime("%d-%m-%Y_%H-%M-%S-%f")

            frame_image_path = os.path.join(base_dir, f"frame_{timestamp_str}.png")
            # person_image_path = os.path.join(base_dir, f"person_{timestamp_str}.png")

            # ---------------- Save full frame ----------------
            if frame is not None and frame.size > 0:
                cv2.imwrite(frame_image_path, frame)
                if self.debug:
                    self.logger.info(f"Saved frame image: {frame_image_path}")
            else:
                self.logger.warning("Invalid frame, not saving frame image")
                frame_image_path = None

            return frame_image_path

        except Exception as e:
            self.logger.error(f"Error saving frame/person images: {e}")
            if hasattr(self, 'send_to_grafana') and self.send_to_grafana == 1:
                self.send_grafana_alert(
                    scn=scn,
                    alert_type="data_mismatch",
                    severity="high",
                    desc=f"Error saving frame/person images: {e}"
                )
            return None

    def send_grafana_alert(self, scn: str, alert_type: str, severity: str = "low", device: str = "", desc: str = "", req_payload: dict = None):
        if not self.send_to_grafana:
            self.logger.info(f"Grafana alerting disabled, skipping alert: type={alert_type}, scn={scn}")
            return None
        self.logger.info(f"Sending Grafana alert: type={alert_type}, scn={scn}, severity={severity}")
        
        try:
            status_code = self.grafana.send_alert(scn, alert_type, severity, device, desc, req_payload)
            return status_code
        except HTTPException as e:
            self.logger.error(f"Failed to send Grafana alert: {e.detail}")
            raise
        except Exception as e:
            self.logger.error(f"Unexpected error sending Grafana alert: {e}")
            raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")

    def similarity_score(self, str1, str2):
        score = SequenceMatcher(None, str1, str2).ratio()
        return score

    def find_best_match(self, detected_text):
        if not self.candidates or not detected_text:
            return None, 0.0

        detected_text = detected_text.strip().upper()
        detected_last4 = detected_text[-4:]
        best_match = None
        highest_score = 0.0

        # First-level: match last 4 characters
        last4_matches = [
            candidate for candidate in self.candidates
            if candidate[-4:] == detected_last4
        ]

        # Case 1: Exactly one match
        if len(last4_matches) == 1:
            return last4_matches[0], 1.0

        # Case 2: Multiple last-4 matches — use similarity
        if last4_matches:
            for candidate in last4_matches:
                score = self.similarity_score(detected_text, candidate)
                if score > highest_score:
                    highest_score = score
                    best_match = candidate
            return best_match, highest_score

        # Case 3: No last-4 match — full DB similarity search
        for candidate in self.candidates:
            score = self.similarity_score(detected_text, candidate)
            if score > highest_score:
                highest_score = score
                best_match = candidate

        return best_match

    def is_inside(self, vehicle_bbox, plate_bbox):
            """Check if plate_bbox is inside vehicle_bbox."""
            vx1, vy1, vx2, vy2 = vehicle_bbox
            px1, py1, px2, py2 = plate_bbox
            return px1 >= vx1 and py1 >= vy1 and px2 <= vx2 and py2 <= vy2

    def associate_plates_to_vehicles(self, detections):
        """Associate number plates to vehicles based on bounding box inclusion."""
        associations = {}  # vehicle_id -> plate_bbox
        vehicle_bboxes = []
        plate_bboxes = []

        for i in range(len(detections.xyxy)):
            bbox = detections.xyxy[i].tolist()
            track_id = detections.tracker_id[i] if detections.tracker_id is not None and len(detections.tracker_id) > 0 else None
            conf = detections.confidence[i] if detections.confidence is not None and len(detections.confidence) > 0 else 0.0
            class_name = detections.data['class_name'][i] if detections.data and 'class_name' in detections.data else None

            if class_name == 'Vehicle':
                vehicle_bboxes.append(bbox + [track_id, conf])
            elif class_name == 'Number Plate':
                plate_bboxes.append(bbox + [track_id, conf])

        for v_bbox in vehicle_bboxes:
            for p_bbox in plate_bboxes:
                if self.is_inside(v_bbox[:4], p_bbox[:4]):
                    associations[v_bbox[4]] = p_bbox
                    plate_bboxes.remove(p_bbox)
                    break

        return associations

    def clear_past_entrie(self, rows=30):
        conn = sqlite3.connect(self.DB_PATH)
        cursor = conn.cursor()

        cursor.execute(f"""
            DELETE FROM vehicle_logs
            WHERE id NOT IN (
                SELECT id FROM vehicle_logs
                ORDER BY id DESC
                LIMIT {rows}
            )
        """)

        conn.commit()
        conn.close()

    # PNAYANAM ALERTS

    # 1. FINDING PERSON COUNT ALERT
    '''
    Already writen in telegram_alert.py file..1 
    '''

    # 2. FINDING CAMERA DISCONNECTION ALERT
    def is_camera_disconnected(self, ret):
        return not ret

    # 3. FINDING EMPTY/CORRUPTED FRAMES ALERT
    def is_invalid_frame(self, frame):
        if frame is None: 
            return True
        if not isinstance(frame, np.ndarray): 
            return True
        if frame.size == 0: 
            return True
        if len(frame.shape) != 3: 
            return True

        h, w, c = frame.shape
        if h <= 0 or w <= 0: 
            return True
        if c != 3: 
            return True
        if frame.dtype != np.uint8: 
            return True

        return False

    # 4. FINDING CAMERA OBSTRUCTION/OCCLUSION ALERT
    def get_blur_score(self, frame):
        """Calculate blur score."""
        gray = cv2.cvtColor( frame, cv2.COLOR_BGR2GRAY )
        return cv2.Laplacian( gray, cv2.CV_64F ).var()

    ''' ================================================== '''

    def get_day_folder(self):
        today = datetime.now().strftime("%Y-%m-%d")
        day_folder = os.path.join(self.base_dir, today)
        os.makedirs(day_folder, exist_ok=True)
        return day_folder

    def save_alert_frame(self, frame, tracker_id):
        day_folder = self.get_day_folder()
        timestamp = datetime.now().strftime("%H-%M-%S")

        image_path = os.path.join(day_folder,f"tracker_{tracker_id}_{timestamp}.jpg")

        cv2.imwrite(image_path, frame)
        self.logger.info(f"Saved Alert frame at {image_path}")
        return image_path

    def is_new_tracker(self, tracker_id):
        return tracker_id not in self.alerted_tracker_ids

    def add_tracker(self, tracker_id):
        self.alerted_tracker_ids.add(tracker_id)

    def clear_tracker_ids_if_needed(self):
        current_time = datetime.now()

        if (current_time - self.last_cleanup_time) >= timedelta(days=2):
            self.alerted_tracker_ids.clear()
            self.last_cleanup_time = current_time

            self.logger.info("Tracker ID cache cleared.")

    def delete_old_image_folders(self):
        current_time = datetime.now()

        if not os.path.exists(self.base_dir):
            return

        for folder_name in os.listdir(self.base_dir):
            folder_path = os.path.join(self.base_dir, folder_name)
            try:
                folder_date = datetime.strptime(folder_name, "%Y-%m-%d")
                if (current_time - folder_date) > timedelta(days=2):
                    shutil.rmtree(folder_path)

                    self.logger.info(f"[INFO] Deleted {folder_path}")

            except ValueError:
                continue

    # def get_subscribers(self):

    #     subscribers = [
    #         chat_id.strip()
    #         for chat_id in self.config_in.get("subscribers", "chat_ids").split(",")
    #         if chat_id.strip()
    #     ]

    #     self.logger.info(f"Loaded subscribers: {subscribers}")

    #     return subscribers
    #     # return [1980972331,8666481737]
    def get_subscribers(self, target_site_id=None):
        """Fetches primary subscriber phone numbers from the API.

        If target_site_id is provided, returns the primary recipient for that specific
        site. Otherwise, returns all primary recipients across all sites.
        """
        api_url = f"http://{self.gui_ip}:8000/api/site-recipients"
        subscribers = []

        try:
            response = requests.get(api_url, timeout=5)
            response.raise_for_status()
            data = response.json()

            for item in data:
                # Check if user is a primary recipient
                if item.get("is_primary"):
                    # If target_site_id is specified, filter by site_id as well
                    if (
                        target_site_id is None
                        or item.get("site_id") == target_site_id
                    ):
                        phone = item.get("phone")
                        if phone:
                            # Prepend country code if missing
                            formatted_phone = (
                                phone if phone.startswith("+") else f"+91{phone}"
                            )
                            subscribers.append(formatted_phone)

            self.logger.info(
                f"Loaded dynamic primary subscribers for site '{target_site_id}': {subscribers}"
            )

        except requests.RequestException as e:
            self.logger.error(
                f"Failed to fetch subscribers from API: {e}. Falling back to config."
            )
            # Fallback to config file in case API is down
            subscribers = [
                chat_id.strip()
                for chat_id in self.config_in.get(
                    "subscribers", "chat_ids", fallback=""
                ).split(",")
                if chat_id.strip()
            ]

        return subscribers