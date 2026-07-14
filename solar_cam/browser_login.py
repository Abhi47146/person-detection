from playwright.sync_api import sync_playwright
import time


def login():

    p = sync_playwright().start()

    browser = p.chromium.launch(
        headless=True,
        slow_mo=100
    )

    page = browser.new_page()

    while True:

        try:

            page.goto(
                "https://v.dvr163.com/?type=truecloud#/login",
                wait_until="load",
                timeout=0
            )

            break

        except Exception as error:

            print(
                "Login page network error. "
                "Retrying in 5 seconds..."
            )

            print(error)

            time.sleep(5)

    email = page.get_by_role(
        "textbox",
        name="Mobile/Email"
    )

    email.wait_for(
        state="visible",
        timeout=0
    )

    email.fill(
        "ssesignaldornakal@gmail.com"
    )

    password = page.get_by_role(
        "textbox",
        name="Password"
    )

    password.wait_for(
        state="visible",
        timeout=0
    )

    password.fill(
        "DKJ@2025"
    )

    select_icon = page.locator(
        ".select-icon img"
    ).first

    select_icon.wait_for(
        state="visible",
        timeout=0
    )

    select_icon.click(
        timeout=0
    )

    login_button = page.get_by_role(
        "button",
        name="Login"
    )

    login_button.wait_for(
        state="visible",
        timeout=0
    )

    login_button.click(
        timeout=0
    )

    return p, browser, page