from solar_cam.browser_login import login
import os
import time
from datetime import datetime


def ensure_fullscreen(
    page,
    window,
    camera_name
):

    attempt = 0

    while True:

        attempt += 1

        try:

            listener_class = (
                page.locator("#listener")
                .get_attribute("class") or ""
            )

            if "isfullScreen" in listener_class:

                print(
                    f"{camera_name}: "
                    f"fullscreen confirmed"
                )

                return True

            print(
                f"{camera_name}: "
                f"fullscreen attempt {attempt}"
            )

            video_box = page.locator(
                f'div.video-box[data-index="{window}"]'
            )

            try:

                video_box.wait_for(
                    state="visible",
                    timeout=60000
                )

            except Exception:

                print(
                    f"{camera_name}: "
                    f"video box not visible yet. "
                    f"Retrying..."
                )

                page.wait_for_timeout(3000)

                continue

            box = video_box.bounding_box()

            if box is None:

                print(
                    f"{camera_name}: "
                    f"video box position not ready. "
                    f"Retrying..."
                )

                page.wait_for_timeout(2000)

                continue

            cx = (
                box["x"]
                + box["width"] / 2
            )

            cy = (
                box["y"]
                + box["height"] / 2
            )

            page.mouse.click(
                cx,
                cy
            )

            page.wait_for_timeout(
                500
            )

            page.mouse.dblclick(
                cx,
                cy
            )

            try:

                page.wait_for_function(
                    """
                    () => {

                        const listener =
                            document.querySelector(
                                '#listener'
                            );

                        return (
                            listener &&
                            listener.classList.contains(
                                'isfullScreen'
                            )
                        );
                    }
                    """,
                    timeout=5000
                )

                print(
                    f"{camera_name}: "
                    f"fullscreen confirmed "
                    f"after {attempt} attempts"
                )

                return True

            except Exception:

                print(
                    f"{camera_name}: "
                    f"fullscreen attempt "
                    f"{attempt} failed. "
                    f"Retrying..."
                )

                page.wait_for_timeout(
                    2000
                )

        except Exception as error:

            print(
                f"{camera_name}: "
                f"fullscreen attempt error. "
                f"Retrying..."
            )

            print(error)

            page.wait_for_timeout(
                3000
            )


def monitor_camera(
    camera_name,
    window
):

    p = None
    browser = None

    CAMERA_BASE_PATH = "/var/pnayanam/cctv"
    os.makedirs( CAMERA_BASE_PATH, exist_ok=True)

    try:
        p, browser, page = login()
        os.makedirs(
            CAMERA_BASE_PATH,
            exist_ok=True
        )
        page.locator(
            "img"
        ).nth(3).click(
            timeout=0
        )
        screen_button = page.locator(
            ".screen-btn-box"
        )

        screen_button.wait_for(
            state="visible",
            timeout=0
        )

        print(
            f"{camera_name}: "
            f"CCTV page ready"
        )
        screen_button.click(
            timeout=0
        )

        page.wait_for_timeout(
            1000
        )

        layout_9 = page.get_by_text(
            "9",
            exact=True
        )

        layout_9.wait_for(
            state="visible",
            timeout=0
        )

        layout_9.click(
            timeout=0
        )

        page.wait_for_timeout(
            6000
        )

        print(
            f"Loading {camera_name}..."
        )
        video_box = page.locator(
            f'div.video-box[data-index="{window}"]'
        )

        video_box.wait_for(
            state="visible",
            timeout=0
        )

        video_box.click(
            timeout=0
        )

        page.wait_for_timeout(
            2000
        )
        camera_node = page.locator(
            "#homeindex-sidebar "
            "span.tree_node_name",
            has_text=camera_name
        ).first

        camera_node.wait_for(
            state="visible",
            timeout=0
        )

        camera_node.click(
            timeout=0
        )
        page.wait_for_timeout(
            6000
        )
        safe_camera_name = camera_name

        camera_folder = os.path.join(
            CAMERA_BASE_PATH,

            
            safe_camera_name
        )

        os.makedirs(
            camera_folder,
            exist_ok=True
        )

        print(
            f"{camera_name} loaded."
        )
        ensure_fullscreen(
            page,
            window,
            camera_name
        )
        video_box = page.locator(
            f'#listener '
            f'div.video-box[data-index="{window}"]'
        )

        video_box.wait_for(
            state="visible",
            timeout=0
        )

        last_camera_refresh = (
            time.monotonic()
        )

        REFRESH_INTERVAL = 240
        tmp_path = os.path.join(
            camera_folder,
            "latest_frame.tmp.png"
        )

        final_path = os.path.join(
            camera_folder,
            "latest_frame.png"
        )
        while True:
            if (
                time.monotonic()
                - last_camera_refresh
                >= REFRESH_INTERVAL
            ):

                print(
                    f"Refreshing {camera_name}..."
                )

                try:

                    camera_node = page.locator(
                        "#homeindex-sidebar "
                        "span.tree_node_name",
                        has_text=camera_name
                    ).first

                    camera_node.wait_for(
                        state="visible",
                        timeout=0
                    )

                    try:

                        camera_node.click(
                            force=True,
                            timeout=5000
                        )

                        print(
                            f"{camera_name}: "
                            f"force click successful"
                        )

                    except Exception as click_error:

                        print(
                            f"{camera_name}: "
                            f"force click failed"
                        )

                        print(
                            click_error
                        )
                        camera_node.evaluate(
                            "element => element.click()"
                        )

                        print(
                            f"{camera_name}: "
                            f"JavaScript click successful"
                        )

                    page.wait_for_timeout(
                        5000
                    )

                    ensure_fullscreen(
                        page,
                        window,
                        camera_name
                    )

                    video_box = page.locator(
                        f'#listener '
                        f'div.video-box'
                        f'[data-index="{window}"]'
                    )

                    video_box.wait_for(
                        state="visible",
                        timeout=0
                    )

                    print(
                        f"{camera_name} "
                        f"refreshed successfully"
                    )

                except Exception as refresh_error:

                    print(
                        f"{camera_name}: "
                        f"refresh failed"
                    )

                    print(
                        refresh_error
                    )

                    print(
                        f"{camera_name}: "
                        f"process will continue running"
                    )

                finally:

                    last_camera_refresh = (
                        time.monotonic()
                    )
            try:

                listener_class = (
                    page.locator("#listener")
                    .get_attribute("class") or ""
                )

            except Exception as listener_error:

                print(
                    f"{camera_name}: "
                    f"could not check fullscreen state"
                )

                print(
                    listener_error
                )

                page.wait_for_timeout(
                    2000
                )

                continue
            if (
                "isfullScreen"
                not in listener_class
            ):

                print(
                    f"{camera_name}: "
                    f"fullscreen lost before screenshot"
                )

                ensure_fullscreen(
                    page,
                    window,
                    camera_name
                )

                video_box = page.locator(
                    f'#listener '
                    f'div.video-box'
                    f'[data-index="{window}"]'
                )

                video_box.wait_for(
                    state="visible",
                    timeout=0
                )

                print(
                    f"{camera_name}: "
                    f"fullscreen restored"
                )
            try:

                video_box.screenshot(
                    path=tmp_path,
                    timeout=10000
                )

                os.replace(
                    tmp_path,
                    final_path
                )

                print(
                    f"["
                    f"{datetime.now().strftime('%H:%M:%S')}"
                    f"] "
                    f"{camera_name} updated"
                )

            except Exception as screenshot_error:

                print(
                    f"{camera_name}: "
                    f"screenshot failed"
                )

                print(
                    screenshot_error
                )

                if os.path.exists(
                    tmp_path
                ):

                    try:

                        os.remove(
                            tmp_path
                        )

                    except Exception:

                        pass

                video_box = page.locator(
                    f'#listener '
                    f'div.video-box'
                    f'[data-index="{window}"]'
                )
            page.wait_for_timeout(
                125
            )
    except KeyboardInterrupt:

        print(
            f"{camera_name} "
            f"stopped by keyboard interrupt."
        )
    except Exception as process_error:

        print(
            f"{camera_name}: "
            f"unexpected error"
        )

        print(
            process_error
        )
    finally:
        try:

            if (
                'tmp_path' in locals()


            ):

                os.remove(
                    tmp_path
                )

        except Exception:

            pass

        if browser is not None:

            try:

                browser.close()

            except Exception:

                pass

        if p is not None:

            try:

                p.stop()

            except Exception:

                pass

        print(
            f"{camera_name}: "
            f"browser and Playwright closed."
        )