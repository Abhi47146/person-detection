from configparser import ConfigParser
import json
import requests 
from itspelogger import Logger
from fastapi import HTTPException

class Grafana:
    def __init__(self, configfile="config.ini", logger=None):
        config = ConfigParser()
        config.read(configfile)
        self.product = config.get("GRAFANA", "product", fallback="DUMMY")
        self.url = config.get("GRAFANA", "url", fallback="http://grafana.itspe.co.in")
        self.grafana_dashboard_url = config.get("GRAFANA", "grafana_dashboard_url")
      
        if logger is None:
            logger = Logger(__name__)
        self.logger = logger
        self.alertGrafana = None

        self.task_assigner = {
            "device_offline": "ops",
            "signal_conflict": "ops",
            "controller_fault": "ops",
            "hightemperature": "ops",
            "gpsinactive": "ops",
            "gpsdisconnected": "ops",
            "gpstampered": "ops",
            "sumo_updation": "ops",
            "heuristic": "ops",
            "databasedown": "ops",
            "faulty_detector": "ops",
            "slave_fault": "ops",
            "conflict_fault": "ops",
            "lamp_fault": "ops",
            "tim_restart": "ops",
            "otu_time_drift": "ops",
            "tim_process_unresponsive": "ops",
            "data_mismatch": "ops",
            "bad_prediction": "ops"
        }

    def alert_description(self, alert_type, scn, device, desc):
        if alert_type == "device_offline":
            return f"{device} is offline in {self.city} at {scn}"
        elif alert_type == "lamp_fault":
            return f"Lamp fault in {self.city} at {scn}"
        elif alert_type == "signal_conflict" or alert_type == "conflict_fault":
            return f"Signal conflict in {self.city} at {scn}"
        elif alert_type == "controller_fault":
            return f"Controller fault in {self.city} at {scn}"
        elif alert_type == "hightemperature":
            return f"High temperature in {self.city} at {scn}"
        elif alert_type == "gpsinactive":
            return f"GPS inactive in {self.city} at {scn = }"
        elif alert_type == "gpsdisconnected":
            return f"GPS disconnected in {self.city} at {scn = }"
        elif alert_type == "gpstampered":
            return f"GPS tampered in {self.city} at {scn = }"
        elif alert_type == "databasedown":
            return f"Database down in {self.city}"
        elif alert_type == "sumo_updation":
            return f"Sumo operation failed for group scn {scn} in {self.city}"
        elif alert_type == "heuristic":
            return f"{desc} for group scn {scn}"
        elif alert_type == "faulty_detector":
            return f"Detector is faulty in {self.city} at {scn = }"
        elif alert_type == "slave_fault":
            return f"Slave is faulty in {self.city} at {scn = }"
        elif alert_type == "tim_restart":
            return f"TIM is not working properly in {self.city} at {scn = }"
        elif alert_type == "otu_time_drift":
            return f"Time drift in OTU in {self.city} at {scn = }"
        elif alert_type == "tim_process_unresponsive":
            return f"Tim process is unresponsive in {self.city} at {scn = }"
        elif alert_type == "data_mismatch" or alert_type=="bad_prediction":
            return f"{desc} in {self.city}"

    def post_response(self, payload):
        try:
            payload = json.dumps(payload)
            self.logger.debug(f"payload after json dump = {payload}")
            headers = {"Content-Type": "application/json"}
            response = requests.request("POST", self.url, headers=headers, data=payload, timeout=self.timeout)
            if response.status_code == 200:
                self.logger.info("Alert is sent to grafana")
                return response.status_code
            else:
                self.logger.error(response.text, response)

        except requests.exceptions.Timeout as e:
            self.logger.error(f"Request timed out. Error: {e}")
            raise HTTPException(status_code=504, detail=f"Request timed out. Error: {e}")

        except requests.exceptions.RequestException as e:
            self.logger.error(f"Request failed. Error: {e}")
            raise HTTPException(status_code=500, detail=f"Request failed. Error: {e}")

    def send_alert(self, scn: str, alert_type: str, severity="low", device="", desc="", req_payload=None):
        self.logger.info(f"Sending grafana alert for {scn = } with alert {alert_type}")
        if alert_type not in self.task_assigner:
            self.logger.error(f"Unknown alert type {alert_type}")
            raise HTTPException(
                    status_code=400, detail=f"Unknown alert type {alert_type}"
                )
        alert = self.alert_description(alert_type, scn, device, desc)
        payload = {
            "status": "firing",
            "groupKey": alert,
            "externalURL": self.grafana_dashboard_url,
            "groupLabels": {
                "alertname": alert,
            },
            "numResolved": 0,
            "commonLabels": {
                "issue": alert_type,
                "assigned-team": self.task_assigner[alert_type],
                "job": "Check the issue and get it fixed",
                "severity": severity,
                "project": self.product,
                "city": self.city,
            },
        }
        if req_payload is not None:
            payload["commonLabels"]["request_payload"] = req_payload
        if alert_type == "device_offline" or alert_type == "lamp_fault" or alert_type == "controller_fault" or alert_type == "slave_fault":
            payload["commonLabels"]["job"] = "Call the site person and get it fixed"
        if alert_type == "faulty_detector" or alert_type == "tim_restart" or alert_type == "otu_time_drift" or alert_type == "tim_process_unresponsive":
            payload["commonLabels"]["job"] = "Check the logs and get it fixed"
        try:
            if (
                (alert_type == "device_offline" and self.booldeviceoffline)
                or (alert_type == "lamp_fault" and self.boollamp_fault)
                or (alert_type == "signal_conflict" and self.boolsignalconflict)
                or (alert_type == "controller_fault" and self.boolcontrollerfault)
                or (alert_type == "hightemperature" and self.boolhightemperature)
                or (alert_type == "gpsinactive" and self.boolgpsinactive)
                or (alert_type == "gpsdisconnected" and self.boolgpsdisconnected)
                or (alert_type == "gpstampered" and self.boolgpstampered)
                or (alert_type == "databasedown" and self.booldatabasedown)
                or (alert_type == "sumo_updation" and self.boolsumo_updation)
                or (alert_type == "faulty_detector" and self.boolfaulty_detector)
                or (alert_type == "slave_fault" and self.boolslave_fault)
                or (alert_type == "conflict_fault" and self.boolconflict_fault)
                or (alert_type == "tim_restart" and self.booltim_restart)
                or (alert_type == "otu_time_drift" and self.boolotu_time_drift)
                or (alert_type == "tim_process_unresponsive" and self.booltim_process_unresponsive)
                or (alert_type == "data_mismatch" and self.booldata_mismatch)
                or (alert_type == "bad_prediction" and self.boolbad_prediction)
            ):
                return self.post_response(payload)

        except requests.exceptions.RequestException as e:
            error = f"Request failed. Error: {e}"
            self.logger.error(error)
            raise HTTPException(status_code=500, detail=error)


def main():
    grafana = Grafana("config.ini")
    grafana.send_alert("", "data_mismatch", "low", desc = "Bus type Ordinary not found")