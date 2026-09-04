import os
import time
import threading
from datetime import datetime
from solar_cam.browser_login import login
from solar_cam.config import (
    get_sites,
    get_solar_camera_scn_map_by_site,
    get_active_solar_cameras_by_site,
    update_camera_online_status,
    report_camera_status,
)


# ============================================================================
# 1. STATUS MONITORING (One browser per site - updates online/offline status)
# ============================================================================

def update_site_camera_statuses(page, site_name):
    """
    Scrapes online/offline status for ALL solar cameras on the given site sidebar
    and persists status back to the config file and backend API.
    """
    solar_cam_map = get_solar_camera_scn_map_by_site().get(site_name, {})
    if not solar_cam_map:
        return

    # Query camera node wrappers in the sidebar
    device_nodes = page.locator('#homeindex-sidebar span.device-title')
    node_count = device_nodes.count()

    for i in range(node_count):
        node = device_nodes.nth(i)

        name_node = node.locator('span.tree_node_name')
        if name_node.count() == 0:
            continue

        camera_name = name_node.inner_text().strip()

        if camera_name not in solar_cam_map:
            continue

        scn = solar_cam_map[camera_name]

        # Check DOM tag for "(Offline)"
        status_node = node.locator('span.online-status')
        if status_node.count() > 0 and "(Offline)" in status_node.inner_text():
            is_online = False
            status_str = "inactive"
        else:
            is_online = True
            status_str = "active"

        # Update ini file and API
        update_camera_online_status(scn, is_online)
        report_camera_status(scn, status_str)


def site_status_monitor(site_name, site_config):
    """
    Runs a dedicated browser instance per site that continuously loops over the 
    sidebar nodes to monitor and report camera connectivity status.
    """
    p = None
    browser = None
    print(f"[STATUS MONITOR] Starting status tracker for site: {site_name}")

    try:
        p, browser, page = login(
            site_config["url"],
            site_config["email"],
            site_config["password"],
        )

        page.locator("img").nth(3).click(timeout=0)
        page.wait_for_timeout(2000)
        expand_all_groups(page, f"[STATUS-{site_name}]")

        while True:
            try:
                update_site_camera_statuses(page, site_name)
            except Exception as err:
                print(f"[STATUS-{site_name}] Error scraping statuses: {err}")

            time.sleep(10)

    except Exception as e:
        print(f"[STATUS-{site_name}] Critical error in status monitor: {e}")
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        if p:
            try:
                p.stop()
            except Exception:
                pass


# ============================================================================
# 2. CAPTURE WORKER (Dedicated browser per scheduled active camera)
# ============================================================================

def single_camera_capture_worker(site_config, camera_info, base_path):
    """
    Runs a browser instance to capture frames for a single camera during its 
    configured active schedule window.
    """
    camera_name = camera_info["name"]
    scn = camera_info["scn"]
    camera_folder = os.path.join(base_path, camera_name)

    p = None
    browser = None
    print(f"[CAPTURE WORKER] Starting dedicated worker for {camera_name} ({scn})")

    try:
        p, browser, page = login(
            site_config["url"],
            site_config["email"],
            site_config["password"],
        )

        page.locator("img").nth(3).click(timeout=0)

        screen_button = page.locator(".screen-btn-box")
        screen_button.wait_for(state="visible", timeout=0)
        page.wait_for_timeout(1000)

        debug_dir = os.path.join(base_path, "_debug")
        if not select_layout_9(page, f"[CAM-{camera_name}]", screen_button, debug_dir):
            raise RuntimeError("Could not select layout 9")

        page.wait_for_timeout(4000)
        expand_all_groups(page, f"[CAM-{camera_name}]")

        # Capture frame loop while active
        while True:
            # Re-verify schedule window; exit if window expires
            active_by_site = get_active_solar_cameras_by_site()
            site_active_cams = active_by_site.get(site_config.get("site_name", ""), [])
            
            # Check if camera SCN is still within active windows
            is_still_active = any(c["scn"] == scn for c in site_active_cams)
            if not is_still_active:
                print(f"[CAM-{camera_name}] Schedule window closed. Exiting capture worker.")
                break

            capture_camera(page, camera_name, camera_folder, f"[CAM-{camera_name}]")
            time.sleep(2)

    except Exception as e:
        print(f"[CAM-{camera_name}] Worker error: {e}")
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        if p:
            try:
                p.stop()
            except Exception:
                pass


# ============================================================================
# 3. HELPER FUNCTIONS FOR PLAYWRIGHT UI INTERACTION
# ============================================================================

def ensure_fullscreen(page, window, label):
    attempt = 0
    while True:
        attempt += 1
        try:
            listener_class = page.locator("#listener").get_attribute("class") or ""
            if "isfullScreen" in listener_class:
                return True

            video_box = page.locator(f'div.video-box[data-index="{window}"]')

            try:
                video_box.wait_for(state="visible", timeout=60000)
            except Exception:
                page.wait_for_timeout(3000)
                continue

            box = video_box.bounding_box()
            if box is None:
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
                return True
            except Exception:
                page.wait_for_timeout(2000)

        except Exception as error:
            page.wait_for_timeout(3000)


def expand_all_groups(page, label):
    tree_root = page.locator('#homeindex-sidebar [role="tree"]').first
    try:
        tree_root.wait_for(state="visible", timeout=15000)
    except Exception:
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
            break

        for i in range(count):
            try:
                expand_icon = collapsed_groups.nth(i).locator('i.el-tree-node__expand-icon').first
                expand_icon.click(timeout=15000)
                page.wait_for_timeout(400)
            except Exception:
                pass

        page.wait_for_timeout(500)


def exit_fullscreen(page, window, label):
    try:
        listener_class = page.locator("#listener").get_attribute("class") or ""
        if "isfullScreen" not in listener_class:
            return True

        video_box = page.locator(f'#listener div.video-box[data-index="{window}"]')
        video_box.wait_for(state="visible", timeout=10000)

        box = video_box.bounding_box()
        if box is None:
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
            pass

        return True
    except Exception:
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
            return True
        except Exception:
            page.wait_for_timeout(2000)

    return False


def capture_camera(page, camera_name, camera_folder, label):
    window = "1"

    try:
        exit_fullscreen(page, window, label)

        video_box = page.locator(f'div.video-box[data-index="{window}"]')
        video_box.wait_for(state="visible", timeout=15000)
        video_box.click(timeout=10000)
        page.wait_for_timeout(1000)

        camera_node = page.locator(f"#homeindex-sidebar span.tree_node_name:text-is('{camera_name}')").first

        if camera_node.count() == 0:
            camera_node = page.get_by_text(camera_name, exact=True).first

        try:
            camera_node.wait_for(state="visible", timeout=10000)
        except Exception:
            expand_all_groups(page, label)
            camera_node.wait_for(state="visible", timeout=15000)

        camera_node.scroll_into_view_if_needed()
        camera_node.click(timeout=10000)
        page.wait_for_timeout(3500)

        os.makedirs(camera_folder, exist_ok=True)
        ensure_fullscreen(page, window, camera_name)

        fs_video_box = page.locator(f'#listener div.video-box[data-index="{window}"]')
        fs_video_box.wait_for(state="visible", timeout=15000)

        tmp_path = os.path.join(camera_folder, "latest_frame.tmp.png")
        final_path = os.path.join(camera_folder, "latest_frame.png")

        fs_video_box.screenshot(path=tmp_path, timeout=10000)
        os.replace(tmp_path, final_path)

        print(f"[{datetime.now().strftime('%H:%M:%S')}] {camera_name} updated in {camera_folder}")
        return True

    except Exception as error:
        print(f"{camera_name}: capture failed - {error}")
        return False