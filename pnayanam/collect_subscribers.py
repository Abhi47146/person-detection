import time
import requests
import configparser
from pnayanam.logger import Logger
import os

config_path = os.path.join(
    os.path.dirname(__file__),
    "resources",
    "config.ini"
)
config = configparser.ConfigParser()
config.read(config_path)

BOT_TOKEN = config.get("bot_token", "token", fallback="")
telegram_url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"

last_update_id = 0
logger = Logger("collect_subscribers")


def load_existing_subscribers():
    config = configparser.ConfigParser()
    config.read(config_path)

    chat_ids = []
    owners = []

    if config.has_option("subscribers", "chat_ids"):
        chat_ids = [x.strip() for x in config.get("subscribers", "chat_ids").split(",") if x.strip()]

    if config.has_option("subscribers", "owners"):
        owners = [x.strip() for x in config.get("subscribers", "owners").split(",") if x.strip()]

    return dict(zip(chat_ids, owners))


def print_subscribers(subscriber_map):
    logger.info("\n===== CURRENT SUBSCRIBERS =====")
    if not subscriber_map:
        logger.info("No subscribers found Initially.")
    else:
        for chat_id, owner in subscriber_map.items():
            logger.info(f"Chat ID: {chat_id} | Owner: {owner}")


def update_subscribers():
    global last_update_id

    if not BOT_TOKEN:
        return

    try:
        response = requests.get(
            telegram_url,
            params={"offset": last_update_id + 1, "timeout": 10}
        )
        data = response.json()
    except Exception as e:
        logger.warning(f"Telegram connection error: {e}")
        return

    if not data.get("ok"):
        # Suppress conflict log spam
        if data.get("error_code") == 409:
            logger.warning("Telegram conflict 409: Another bot instance is active. Sleeping...")
            time.sleep(10)
        else:
            logger.info(f"Telegram API error: {data}")
        return

    config = configparser.ConfigParser()
    config.read(config_path)

    subscriber_map = load_existing_subscribers()

    for update in data.get("result", []):
        if "message" not in update:
            continue

        last_update_id = update["update_id"]
        message = update["message"]
        chat = message["chat"]
        chat_id = str(chat["id"])

        first_name = chat.get("first_name", "")
        last_name = chat.get("last_name", "")
        owner_name = f"{first_name} {last_name}".strip()

        if message.get("text", "") == "/start":
            subscriber_map[chat_id] = owner_name

    if "subscribers" not in config:
        config["subscribers"] = {}

    config["subscribers"]["chat_ids"] = ",".join(subscriber_map.keys())
    config["subscribers"]["owners"] = ",".join(subscriber_map.values())

    with open(config_path, "w") as configfile:
        config.write(configfile)


def subscriber_monitor():
    existing = load_existing_subscribers()
    logger.info(f"Existing subscribers: {existing}")

    while True:
        try:
            update_subscribers()
        except Exception as e:
            logger.info(f"Error in subscriber monitor: {e}")
        time.sleep(10)


if __name__ == "__main__":
    subscriber_monitor()