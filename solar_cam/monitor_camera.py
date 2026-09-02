import os
import time
from datetime import datetime
from solar_cam.browser_login import login
from solar_cam.config import get_cameras_by_site


def ensure_fullscreen(page, window, label):
    attempt = 0
    while True:
        attempt += 1
        try:
            listener_class = page.locator("#listener").get_attribute("class") or ""
            if "isfullScreen" in listener_class:
                print(f"{label}: fullscreen confirmed")
                return True

            print(f"{label}: fullscreen attempt {attempt}")
            video_box = page.locator(f'div.video-box[data-index="{window}"]')

            try:
                video_box.wait_for(state="visible", timeout=60000)
            except Exception:
                print(f"{label}: video box not visible yet. Retrying...")
                page.wait_for_timeout(3000)
                continue

            box = video_box.bounding_box()
            if box is None:
                print(f"{label}: video box position not ready. Retrying...")
                page.wait_for_timeout(2000)
                continue

            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2

            page.mouse.click(cx, cy)
            page.wait_for_timeout(500)
            page.mouse.dblclick(cx, cy)

            try:
                page.wait_for_function(
                    """
                    () => {
                        const listener = document.querySelector('#listener');
                        return listener && listener.classList.contains('isfullScreen');
                    }
                    """,
                    timeout=5000
                )
                print(f"{label}: fullscreen confirmed after {attempt} attempts")
                return True
            except Exception:
                print(f"{label}: fullscreen attempt {attempt} failed. Retrying...")
                page.wait_for_timeout(2000)

        except Exception as error:
            print(f"{label}: fullscreen attempt error. Retrying... {error}")
            page.wait_for_timeout(3000)


def expand_all_groups(page, label):
    print(f"{label}: expanding sidebar groups...")
    tree_root = page.locator('#homeindex-sidebar [role="tree"]').first

    try:
        tree_root.wait_for(state="visible", timeout=15000)
    except Exception:
        print(f"{label}: sidebar tree not found, skipping group expansion.")
        return

    max_passes = 10
    for pass_num in range(1, max_passes + 1):
        collapsed_groups = page.locator(
            '#homeindex-sidebar div.el-tree-node[aria-expanded="false"]'
        ).filter(
            has=page.locator('i.el-tree-node__expand-icon:not(.is-leaf)')
        )

        count = collapsed_groups.count()
        if count == 0:
            print(f"{label}: all groups expanded (pass {pass_num}).")
            break

        print(f"{label}: expanding {count} group(s) (pass {pass_num}).")
        for i in range(count):
            try:
                expand_icon = collapsed_groups.nth(i).locator('i.el-tree-node__expand-icon').first
                expand_icon.click(timeout=15000)
                page.wait_for_timeout(400)
            except Exception as error:
                print(f"{label}: failed to expand a group (pass {pass_num}, item {i + 1}) - {error}")

        page.wait_for_timeout(500)
    else:
        print(f"{label}: reached max expand passes, continuing anyway.")

    page.wait_for_timeout(500)
    print(f"{label}: finished expanding sidebar groups.")


def exit_fullscreen(page, window, label):
    try:
        listener_class = page.locator("#listener").get_attribute("class") or ""
        if "isfullScreen" not in listener_class:
            return True

        video_box = page.locator(f'#listener div.video-box[data-index="{window}"]')
        video_box.wait_for(state="visible", timeout=10000)

        box = video_box.bounding_box()
        if box is None:
            print(f"{label}: couldn't locate fullscreen box to exit it.")
            return False

        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2

        page.mouse.dblclick(cx, cy)
        page.wait_for_timeout(1000)

        try:
            page.wait_for_function(
                """
                () => {
                    const listener = document.querySelector('#listener');
                    return listener && !listener.classList.contains('isfullScreen');
                }
                """,
                timeout=5000
            )
        except Exception:
            print(f"{label}: exit-fullscreen not confirmed, continuing anyway.")

        return True
    except Exception as error:
        print(f"{label}: failed to exit fullscreen - {error}")
        return False


def select_layout_9(page, label, screen_button, debug_dir):
    layout_9 = page.locator('li.el-dropdown-menu__item').filter(
        has=page.locator('p.text:text-is("9")')
    )

    max_attempts = 10
    for attempt in range(1, max_attempts + 1):
        try:
            screen_button.click(timeout=15000)
            layout_9.wait_for(state="visible", timeout=15000)
            layout_9.click(timeout=15000)
            print(f"{label}: layout 9 selected (attempt {attempt})")
            return True
        except Exception as error:
            print(f"{label}: layout-9 selection attempt {attempt} failed, retrying... - {error}")
            try:
                os.makedirs(debug_dir, exist_ok=True)
                page.screenshot(path=os.path.join(debug_dir, f"{label}_layout9_attempt{attempt}.png"))
                with open(os.path.join(debug_dir, f"{label}_layout9_attempt{attempt}.html"), "w", encoding="utf-8") as f:
                    f.write(page.content())
            except Exception as debug_error:
                print(f"{label}: couldn't save debug artifacts - {debug_error}")

            page.wait_for_timeout(2000)

    print(f"{label}: giving up on layout-9 selection after {max_attempts} attempts.")
    return False


def capture_camera(page, camera_name, camera_folder, label):
    window = "1"

    try:
        exit_fullscreen(page, window, label)

        video_box = page.locator(f'div.video-box[data-index="{window}"]')
        video_box.wait_for(state="visible", timeout=15000)
        video_box.click(timeout=10000)
        page.wait_for_timeout(1000)

        # STRICT EXACT MATCHING: Uses exact text selector to prevent matching other camera names
        camera_node = page.locator(f"#homeindex-sidebar span.tree_node_name:text-is('{camera_name}')").first

        if camera_node.count() == 0:
            # Secondary strict exact fallback
            camera_node = page.get_by_text(camera_name, exact=True).first

        try:
            camera_node.wait_for(state="visible", timeout=10000)
        except Exception:
            print(f"{camera_name}: exact node not visible, re-expanding groups...")
            expand_all_groups(page, label)
            camera_node.wait_for(state="visible", timeout=15000)

        # Scroll node into view before clicking to prevent clicking adjacent camera items
        camera_node.scroll_into_view_if_needed()
        camera_node.click(timeout=10000)

        # Allow stream connection and canvas render to settle
        page.wait_for_timeout(3500)

        os.makedirs(camera_folder, exist_ok=True)

        ensure_fullscreen(page, window, camera_name)

        fs_video_box = page.locator(f'#listener div.video-box[data-index="{window}"]')
        fs_video_box.wait_for(state="visible", timeout=15000)

        tmp_path = os.path.join(camera_folder, "latest_frame.tmp.png")
        final_path = os.path.join(camera_folder, "latest_frame.png")

        fs_video_box.screenshot(path=tmp_path, timeout=10000)

        # Atomic replacement ensures camera readers never read empty or partial files
        os.replace(tmp_path, final_path)

        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] "
            f"{camera_name} updated successfully in {camera_folder}"
        )
        return True

    except Exception as error:
        print(f"{camera_name}: capture failed - {error}")
        return False


def monitor_site(site_name, site_config):
    p = None
    browser = None

    CAMERA_BASE_PATH = "/var/pnayanam/cctv"
    os.makedirs(CAMERA_BASE_PATH, exist_ok=True)

    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = 5

    try:
        p, browser, page = login(
            site_config["url"],
            site_config["email"],
            site_config["password"],
        )

        page.locator("img").nth(3).click(timeout=0)

        screen_button = page.locator(".screen-btn-box")
        screen_button.wait_for(state="visible", timeout=0)

        print(f"{site_name}: CCTV page ready")
        page.wait_for_timeout(1000)

        if not select_layout_9(
            page,
            site_name,
            screen_button,
            os.path.join(CAMERA_BASE_PATH, "_debug")
        ):
            raise RuntimeError(f"{site_name}: could not select layout 9, restarting session.")

        page.wait_for_timeout(6000)
        expand_all_groups(page, site_name)

        while True:
            cameras = get_cameras_by_site().get(site_name, [])

            if not cameras:
                print(f"{site_name}: no cameras configured yet, waiting...")
                time.sleep(5)
                continue

            for camera_name in cameras:
                camera_folder = os.path.join(CAMERA_BASE_PATH, camera_name)

                success = capture_camera(
                    page,
                    camera_name,
                    camera_folder,
                    site_name
                )

                if success:
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        raise RuntimeError(
                            f"{site_name}: {consecutive_failures} consecutive "
                            f"camera captures failed, restarting browser session."
                        )

                page.wait_for_timeout(500)

    except KeyboardInterrupt:
        print(f"{site_name}: stopped by keyboard interrupt.")
    except Exception as process_error:
        print(f"{site_name}: unexpected error - {process_error}")
    finally:
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

        print(f"{site_name}: browser and Playwright closed.")