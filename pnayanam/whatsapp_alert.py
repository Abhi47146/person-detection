import requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import cv2
import configparser
import os

from pnayanam.logger import Logger
from pnayanam.pnayanam_utils import Requirements

requirements = Requirements()

CONFIG_FILE = os.path.join(os.path.dirname(__file__),"resources","config.ini")
config = configparser.ConfigParser()
config.read(CONFIG_FILE)

logger = Logger("whatsapp_alerts")

ID_INSTANCE = config.get("GREENAPI", "id_instance")
API_TOKEN_INSTANCE = config.get("GREENAPI", "api_token_instance")
API_URL = config.get("GREENAPI", "api_url")

SEND_MESSAGE_URL = f"{API_URL}/waInstance{ID_INSTANCE}/sendMessage/{API_TOKEN_INSTANCE}"
SEND_FILE_URL = f"{API_URL}/waInstance{ID_INSTANCE}/sendFileByUpload/{API_TOKEN_INSTANCE}"


def _to_chat_id(number):
    """Accepts '+91XXXXXXXXXX' or '91XXXXXXXXXX' and returns
    Green API's chatId format '91XXXXXXXXXX@c.us'."""
    number = number.strip().lstrip("+")
    return number if number.endswith("@c.us") else f"{number}@c.us"


def send_image_to_user(chat_id, image_bytes, caption):
    to_chat_id = _to_chat_id(chat_id)
    try:
        response = requests.post(
            SEND_FILE_URL,
            data={
                "chatId": to_chat_id,
                "caption": caption
            },
            files={
                "file": ("alert.jpg", image_bytes, "image/jpeg")
            },
            timeout=20
        )
        logger.info(f"Chat ID: {to_chat_id} | Status: {response.status_code} | Response: {response.text}")

    except Exception:
        logger.exception(f"Failed for {to_chat_id}")


def send_text_to_user(chat_id, caption):
    to_chat_id = _to_chat_id(chat_id)
    try:
        response = requests.post(
            SEND_MESSAGE_URL,
            json={
                "chatId": to_chat_id,
                "message": caption
            },
            timeout=10
        )
        logger.info(f"Alert sent to {to_chat_id}: {response.status_code} | {response.text}")

    except Exception as e:
        logger.info(f"Failed for {to_chat_id}: {e}")


def send_person_alert(image, person_count, scn, camera_name, site):
    subscribers = requirements.get_subscribers()
    if not subscribers:
        return

    caption = (
        f"HUMAN DETECTION ALERT!\n\n"
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

def send_person_count_alert(person_count):
    caption = (
        f"PERSON OCCUPANCY ALERT\n\n"
        f"Number Of Persons : {person_count}\n"
        f"Time : {datetime.now().strftime('%d-%m-%Y %H:%M:%S')}"
    )
    send_text_alert(caption)


def send_camera_disconnect_alert():
    caption = (
        f"CAMERA OFFLINE/DISCONNECTION ALERT\n\n"
        f"Camera Connection Lost\n"
        f"Possible reasons: camera offline, cable issue, or network interruption.\n"
        f"Time : {datetime.now().strftime('%d-%m-%Y %H:%M:%S')}"
    )
    send_text_alert(caption)


def send_invalid_frame_alert():
    caption = (
        f"INVALID/EMPTY FRAMES ALERT\n\n"
        f"Camera stream is active, but the frame could not be read properly.\n"
        f"Possible reasons: decoder issue, corrupted frame, or temporary stream glitch.\n"
        f"Time : {datetime.now().strftime('%d-%m-%Y %H:%M:%S')}"
    )
    send_text_alert(caption)


def send_camera_obstruction_alert():
    caption = (
        f"CAMERA OBSTRUCTION ALERT\n\n"
        f"Camera view appears blocked, blurred, or covered.\n"
        f"Possible tampering or obstruction detected.\n"
        f"Time : {datetime.now().strftime('%d-%m-%Y %H:%M:%S')}"
    )
    send_text_alert(caption)