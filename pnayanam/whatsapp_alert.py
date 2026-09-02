import requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import cv2
import configparser
import os

from pnayanam.logger import Logger
from pnayanam.pnayanam_utils import Requirements

requirements = Requirements()

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "resources", "config.ini")
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
    number = str(number).strip().lstrip("+")
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
    # Fetch subscribers strictly for the target site
    subscribers = requirements.get_subscribers(target_site_name=site)
    
    if not subscribers:
        logger.warning(f"[{scn}] No primary subscribers registered for site '{site}'. Alert skipped.")
        return None

    caption = (
        f"HUMAN DETECTION ALERT!\n\n"
        f"Total persons detected: {person_count}\n"
        f"Time : {datetime.now().strftime('%d-%m-%Y %H:%M:%S')}\n\n"
        f"Camera SCN: {scn}\n"
        f"Camera Name: {camera_name}\n"
        f"Site Name: {site}\n"
    )

    success, buffer = cv2.imencode(".jpg", image)
    if not success:
        return None

    image_bytes = buffer.tobytes()
    logger.info(f"[{scn}] Dispatching alert for site '{site}' to recipients: {subscribers}")
    
    with ThreadPoolExecutor(max_workers=max(1, len(subscribers))) as executor:
        for chat_id in subscribers:
            executor.submit(
                send_image_to_user,
                chat_id,
                image_bytes,
                caption
            )
    return caption


def send_text_alert(caption, site=None):
    subscribers = requirements.get_subscribers(target_site_name=site)
    if not subscribers:
        return

    with ThreadPoolExecutor(max_workers=max(1, len(subscribers))) as executor:
        for chat_id in subscribers:
            executor.submit(
                send_text_to_user,
                chat_id,
                caption
            )