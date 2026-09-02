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

    def send_to_gui(self, payload):
        try:
            if "site_name" not in payload or not payload["site_name"]:
                payload["site_name"] = "N/A"

            url = f"http://{self.gui_ip}:{self.gui_port}/api/alerts"
            response = requests.post(
                url,
                json=payload,
                timeout=(5, 20)
            )
            response.raise_for_status()
            if response.status_code in (200, 201):
                self.logger.info(f"Live alert successfully dispatched to GUI at {self.gui_ip}")
        except requests.exceptions.HTTPError as e:
            self.logger.error(f"HTTP Error sending payload to GUI: {e} | Response: {e.response.text}")
        except Exception as e:
            self.logger.error(f"ERROR SENDING PAYLOAD TO GUI: {e}")

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
    def get_subscribers(self, target_site_name=None, target_site_id=None):
        """
        Fetches primary subscriber phone numbers from the API.
        Filters strictly by site name or site ID if provided.
        """
        recipients_url = f"http://{self.gui_ip}:{self.gui_port}/api/site-recipients"
        sites_url = f"http://{self.gui_ip}:{self.gui_port}/api/sites/active"
        subscribers = set()

        try:
            # 1. Resolve site_id if target_site_name is provided
            resolved_site_id = target_site_id

            if target_site_name and not resolved_site_id:
                try:
                    s_resp = requests.get(sites_url, timeout=3)
                    if s_resp.status_code == 200:
                        sites_data = s_resp.json()
                        target_clean = str(target_site_name).strip().strip('"').lower()
                        for s in sites_data:
                            if str(s.get("name", "")).strip().lower() == target_clean:
                                resolved_site_id = s.get("id")
                                break
                except Exception as e:
                    self.logger.warning(f"Could not resolve site_id for site name '{target_site_name}': {e}")

            # 2. Fetch recipients and filter by primary status & resolved site_id
            response = requests.get(recipients_url, timeout=5)
            response.raise_for_status()
            recipients = response.json()

            for item in recipients:
                if not item.get("is_primary"):
                    continue

                # Filter by site_id if resolved
                if resolved_site_id and item.get("site_id") != resolved_site_id:
                    continue

                phone = item.get("phone")
                if phone:
                    formatted_phone = phone if phone.startswith("+") else f"+91{phone}"
                    subscribers.add(formatted_phone)

            sub_list = list(subscribers)
            self.logger.info(
                f"Loaded dynamic primary subscribers for site '{target_site_name or resolved_site_id}': {sub_list}"
            )
            return sub_list

        except Exception as e:
            self.logger.error(f"Failed to fetch dynamic subscribers from API: {e}. Falling back to config file.")
            
            # Fallback to config file
            fallback_subs = [
                chat_id.strip()
                for chat_id in self.config_in.get("subscribers", "chat_ids", fallback="").split(",")
                if chat_id.strip()
            ]
            return list(set(fallback_subs))