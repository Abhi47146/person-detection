import socket
import subprocess
import requests
import time
import re 
import os
import configparser
from pnayanam.logger import Logger

config_out = configparser.ConfigParser()
config_path = os.path.join(os.path.dirname(__file__), 'resources', 'config.ini')
config_out.read('/var/pnayanam/pnayanam.ini')

protocol = config_out.get(
    "gui_ip_address",
    "protocol",
    fallback="http"
)

gui_ip = config_out.get(
    "gui_ip_address",
    "ip_address",
    fallback="127.0.0.1"
)

port = config_out.get(
    "gui_ip_address",
    "port",
    fallback="8000"
)

EDGE_HEALTH_URL = f"{protocol}://{gui_ip}:{port}/api/edge-health"

CONFIG_FILE = "/var/pnayanam/pnayanam.ini"

logger = Logger("send_gpu_details")

def get_device_type():
    """Read device type from /var/pnayanam/pnayanam.ini"""
    try:
        config = configparser.ConfigParser()
        config.read(CONFIG_FILE)

        # Removes quotes if present, e.g. "Jetson" -> Jetson
        device = config.get("device", "device", fallback="GPU").strip().strip('"')
        return device.lower()

    except Exception as e:
        logger.info(f"[GPU Monitor] Failed to read config: {e}")
        return "gpu"

def get_gpu_resources():
    try:
        device = get_device_type()

        if device == "jetson":
            # Jetson
            proc = subprocess.Popen(
                ["tegrastats"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )

            try:
                result_gpu = proc.stdout.readline().strip()
            finally:
                proc.terminate()
                proc.wait(timeout=2)

            logger.info(f"{result_gpu}")

            gpu_match = re.search(r"GR3D_FREQ\s+(\d+)%", result_gpu)
            util = int(gpu_match.group(1)) if gpu_match else 0

            ram_match = re.search(r"RAM\s+(\d+)/(\d+)MB", result_gpu)
            mem_used = int(ram_match.group(1)) if ram_match else 0
            mem_total = int(ram_match.group(2)) if ram_match else 0

            temp_match = re.search(r"gpu@([\d.]+)C", result_gpu, re.IGNORECASE)
            temp = float(temp_match.group(1)) if temp_match else 0.0

            gpu_info = {
                "gpu_utilization": util,
                "gpu_memory_used_gb": round(mem_used / 1024, 2),
                "gpu_memory_total_gb": round(mem_total / 1024, 2),
                "gpu_temperature": temp,
            }

        else:
            # Desktop / Server NVIDIA GPU
            result_gpu = subprocess.check_output(
                [
                    "nvidia-smi",
                    "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
                    "--format=csv,noheader,nounits"
                ],
                encoding="utf-8"
            ).strip()

            logger.info(f"{result_gpu}")

            util, mem_used, mem_total, temp = [
                x.strip() for x in result_gpu.split(",")
            ]

            gpu_info = {
                "gpu_utilization": int(util),
                "gpu_memory_used_gb": round(int(mem_used) / 1024, 2),
                "gpu_memory_total_gb": round(int(mem_total) / 1024, 2),
                "gpu_temperature": float(temp),
            }

        logger.info(
            f"[GPU] Utilization: {gpu_info['gpu_utilization']}% | "
            f"Memory: {gpu_info['gpu_memory_used_gb']:.2f}/"
            f"{gpu_info['gpu_memory_total_gb']:.2f} GB | "
            f"GPU Temp: {gpu_info['gpu_temperature']}°C"
        )

        return gpu_info

    except Exception as e:
        logger.info(f"[GPU Monitor] {e}")

        return {
            "gpu_utilization": 0,
            "gpu_memory_used_gb": 0,
            "gpu_memory_total_gb": 0,
            "gpu_temperature": 0,
        }

def edge_health_monitor():
    """
    Periodically sends edge node health information to the cloud.
    """

    while True:
        try:

            gpu_info = get_gpu_resources()

            payload = {
                "node_name": socket.gethostname(),
                "status": "Operational",
                "uptime_percentage": 99.9,
                "latency_ms": 3,
                "storage": {
                    "total_space_gb": gpu_info["gpu_memory_total_gb"],
                    "used_space_gb": gpu_info["gpu_memory_used_gb"],
                    "available_space_gb": round(gpu_info["gpu_memory_total_gb"] - gpu_info["gpu_memory_used_gb"],2
                    )
                }
            }

            start = time.perf_counter()

            response = requests.post(
                EDGE_HEALTH_URL,
                json=payload,
                timeout=10
            )

            latency_ms = round((time.perf_counter() - start) * 1000, 2)

            response.raise_for_status()

            logger.info(
                f"[EDGE HEALTH] Sent successfully "
                f"({response.status_code}) | Latency: {latency_ms} ms"
            )

        except requests.RequestException as e:
            logger.info(f"[EDGE HEALTH] Failed to send payload: {e}")

        except Exception as e:
            logger.info(f"[EDGE HEALTH] Unexpected error: {e}")

        time.sleep(2)