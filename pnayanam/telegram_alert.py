import requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import cv2
import configparser
from importlib.resources import files

from pnayanam.logger import Logger

from pnayanam.pnayanam_utils import Requirements
requirements = Requirements()

# CONFIG_FILE = "pnayanam/resources/config.ini"
import os

CONFIG_FILE = os.path.join(
    os.path.dirname(__file__),
    "resources",
    "config.ini"
)
config = configparser.ConfigParser()
config.read(CONFIG_FILE)

logger = Logger("telegram_alerts")

BOT_TOKEN = config.get("bot_token", "token")

telegram_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"

# IMAGE ALERT

def send_image_to_user(chat_id, image_bytes, caption):
    try:
        response = requests.post(
            telegram_url,
            data={"chat_id": chat_id,"caption": caption},
            files={"photo": ("alert.jpg", image_bytes, "image/jpeg")},
            timeout=10
        )

        if response.status_code != 200:
            logger.info(f"{response.text}")

        logger.info(f"Alert sent to {chat_id}: {response.status_code}")

    except Exception as e:
        logger.info(f"Failed for {chat_id}: {e}")

def send_person_alert(image, person_count, scn, camera_name, site):
    subscribers = requirements.get_subscribers()
    if not subscribers:
        return

    caption = (
        f"HUMAN DETCETION ALERT!\n\n"
        f"Total persons detceted: {person_count}\n"
        f"Time : {datetime.now().strftime('%d-%m-%Y %H:%M:%S')}\n\n"
        f"Camera SCN: {scn}\n"
        f"Camera Name: {camera_name}\n"
        f"Site Name: {site}\n"
    )

    success, buffer = cv2.imencode(".jpg", image)
    if not success:
        return

    image_bytes = buffer.tobytes()
    logger.info(f"The subscribers are: {subscribers}")
    with ThreadPoolExecutor(max_workers=len(subscribers)) as executor:
        for chat_id in subscribers:
            executor.submit(
                send_image_to_user,
                chat_id,
                image_bytes,
                caption
            )
    return caption

# TEXT ALERT

def send_text_to_user(chat_id, caption):
    try:
        response = requests.post(
            telegram_url,
            data={"chat_id": chat_id,"text": caption},
            timeout=10
        )
        logger.info(f"Alert sent to {chat_id}: {response.status_code}")
    
    except Exception as e:
        logger.info(f"Failed for {chat_id}: {e}")

def send_text_alert(caption):
    subscribers = requirements.get_subscribers()
    
    if not subscribers:
        return

    with ThreadPoolExecutor(max_workers=len(subscribers)) as executor:
        for chat_id in subscribers:
            executor.submit(
                send_text_to_user,
                chat_id,
                caption
            )

# ALERT TYPES

# SENDING PERSON COUNT/OCCUPANCY ALERT
def send_person_count_alert(person_count):
    caption = (
        f"PERSON OCCUPANCY ALERT\n\n"
        f"Number Of Persons : {person_count}\n"
        f"Time : {datetime.now().strftime('%d-%m-%Y %H:%M:%S')}"
    )

    send_text_alert(caption)

# SENDING CAMERA DISCONNECTION ALERT
def send_camera_disconnect_alert():
    caption = (
        f"CAMERA OFFLINE/DISCONNECTION ALERT\n\n"
        f"Camera Connection Lost\n"
        f"Possible reasons: camera offline, cable issue, or network interruption.\n"
        f"Time : {datetime.now().strftime('%d-%m-%Y %H:%M:%S')}"
    )
    send_text_alert(caption)

# SENDING EMPTY FRAMES ALERT
def send_invalid_frame_alert():
    caption = (
        f"INVALID/EMPTY FRAMES ALERT\n\n"
        f"Camera stream is active, but the frame could not be read properly.\n"
        f"Possible reasons: decoder issue, corrupted frame, or temporary stream glitch.\n"
        f"Time : {datetime.now().strftime('%d-%m-%Y %H:%M:%S')}"
    )
    send_text_alert(caption)

# SENDING CAMERA OBSTRUCTION/OCCLUSION ALERT
def send_camera_obstruction_alert():
    caption = (
        f"CAMERA OBSTRUCTION ALERT\n\n"
        f"Camera view appears blocked, blurred, or covered.\n"
        f"Possible tampering or obstruction detected.\n"
        f"Time : {datetime.now().strftime('%d-%m-%Y %H:%M:%S')}"
    )
    send_text_alert(caption)