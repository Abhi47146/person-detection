from configparser import ConfigParser

CONFIG_PATH = "/var/pnayanam/pnayanam.ini"


def get_sites():

    config = ConfigParser()
    config.read(CONFIG_PATH)

    sites = {}

    if not config.has_section("sites"):
        return sites

    names = [
        name.strip()
        for name in config.get(
            "sites", "names", fallback=""
        ).split(",")
        if name.strip()
    ]

    for site_name in names:

        if not config.has_section(site_name):
            continue

        sites[site_name] = {
            "url": config.get(
                site_name, "url", fallback=""
            ).strip('"'),
            "email": config.get(
                site_name, "email", fallback=""
            ).strip('"'),
            "password": config.get(
                site_name, "password", fallback=""
            ).strip('"'),
        }

    return sites


def get_cameras_by_site():

    config = ConfigParser()
    config.read(CONFIG_PATH)

    cameras_by_site = {}

    if not config.has_section("camera_scn"):
        return cameras_by_site

    scns = [
        scn.strip()
        for scn in config.get(
            "camera_scn", "scns", fallback=""
        ).split(",")
        if scn.strip()
    ]

    for scn in scns:

        if not config.has_section(scn):
            continue

        is_solar_cam = config.getboolean(
            scn, "is_solar_cam", fallback=False
        )

        if not is_solar_cam:
            continue

        camera_name = config.get(
            scn, "name", fallback=scn
        ).strip('"')

        site_name = config.get(
            scn, "site", fallback=""
        ).strip('"')

        if not site_name:

            print(
                f"{scn}: skipped - no 'site' key set, "
                f"can't tell which site this camera belongs to."
            )

            continue

        cameras_by_site.setdefault(
            site_name, []
        ).append(camera_name)

    return cameras_by_site
