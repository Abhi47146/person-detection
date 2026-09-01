from playwright.sync_api import sync_playwright
import time


def login(url, email, password):

    p = sync_playwright().start()

    browser = p.chromium.launch(
        headless=True,
        slow_mo=100,
        args=[
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-extensions",
            "--disable-background-networking",
            "--mute-audio",
            "--js-flags=--max-old-space-size=256",
        ]
    )

    page = browser.new_page()

    while True:

        try:

            page.goto(
                url,
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

    email_field = page.get_by_role(
        "textbox",
        name="Mobile/Email"
    )

    email_field.wait_for(
        state="visible",
        timeout=0
    )

    email_field.fill(
        email
    )

    password_field = page.get_by_role(
        "textbox",
        name="Password"
    )

    password_field.wait_for(
        state="visible",
        timeout=0
    )

    password_field.fill(
        password
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
