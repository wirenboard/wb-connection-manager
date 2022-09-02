import dbus
import time
import signal
import datetime
import logging
import subprocess
from network_manager import NetworkManager
from modem_manager import ModemManager

# Settings
CHECK_PERIOD = datetime.timedelta(seconds=5)
CONNECTION_ACTIVATION_RETRY_TIMEOUT = datetime.timedelta(seconds=60)
CONNECTION_ACTIVATION_TIMEOUT = datetime.timedelta(seconds=30)
CONNECTION_DEACTIVATION_TIMEOUT = datetime.timedelta(seconds=30)
PING_TIMEOUT = datetime.timedelta(seconds=10)
PING_HOST = "ya.ru"
CONNECTION_PRIORITY = ["wb-eth0", "wb-eth1", "wb-wifi", "wb-gsm-sim1", "wb-gsm-sim2"]

NM_ACTIVE_CONNECTION_STATE_ACTIVATED = 2
NM_ACTIVE_CONNECTION_STATE_DEACTIVATED = 4

connection_up_time = dict()

def get_sim_slot(cn_obj):
    settings = cn_obj.GetSettings()
    if "sim2" in settings["connection"]["id"]:
        return 2
    return 1

def wait_device_for_connection(nm, cn_obj, timeout):
    logging.debug('Waiting for device')
    start = datetime.datetime.now()
    while start + timeout >= datetime.datetime.now():
        try:
            dev = nm.find_device_for_connection(cn_obj)
            if dev:
                return dev
        except Exception as ex:
            logging.debug(ex)
        time.sleep(1)
    return None

def wait_connection_activation(nm, cn_path, timeout):
    logging.debug('Waiting for connection activation')
    start = datetime.datetime.now()
    while start + timeout >= datetime.datetime.now():
        try:
            current_state = nm.get_active_connection_property(cn_path, "State")
            if current_state == NM_ACTIVE_CONNECTION_STATE_ACTIVATED:
                return True
        except Exception as ex:
            logging.debug(ex)
            return False
        time.sleep(1)
    return False

def wait_connection_deactivation(nm, cn_path, timeout):
    logging.debug('Waiting for connection deactivation')
    start = datetime.datetime.now()
    while start + timeout >= datetime.datetime.now():
        try:
            current_state = nm.get_active_connection_property(cn_path, "State")
            if current_state == NM_ACTIVE_CONNECTION_STATE_DEACTIVATED:
                return
        except dbus.exceptions.UnknownMethodException:
            # Connection object is already unexported
            return
        time.sleep(1)

def activate_gsm_connection(nm, cn_obj):
    dev = nm.find_device_for_connection(cn_obj)
    if not dev:
        return None
    dev_path = nm.get_device_property(dev, "Udi")
    logging.debug('Device path "%s"', dev_path)
    # Switching SIM card while other connection is active can cause NM restart
    # So deactivate active connection if it exists
    active_connection_path = nm.get_active_connection_path(dev)
    if active_connection_path:
        logging.debug('Deactivate active connection')
        nm.deactivate_connection(active_connection_path)
        wait_connection_deactivation(nm, active_connection_path, CONNECTION_DEACTIVATION_TIMEOUT)
    mm = ModemManager()
    if mm.set_primary_sim_slot(dev_path, get_sim_slot(cn_obj)):
        # After switching SIM card MM recreates device with new path
        dev = wait_device_for_connection(nm, cn_obj, datetime.timedelta(seconds=30))
        if not dev:
            return None
        dev_path = nm.get_device_property(dev, "Udi")
        logging.debug('Device path after SIM switching "%s"', dev_path)
        active_connection_path = nm.activate_connection(cn_obj, dev)
        if wait_connection_activation(nm, active_connection_path, CONNECTION_ACTIVATION_TIMEOUT):
            return active_connection_path
    return None

def activate_generic_connection(nm, cn_obj):
    dev = nm.find_device_for_connection(cn_obj)
    if not dev:
        return None
    active_connection_path = nm.activate_connection(cn_obj, dev)
    if wait_connection_activation(nm, active_connection_path, CONNECTION_ACTIVATION_TIMEOUT):
        return active_connection_path
    return None

def activate_connection(nm, cn_id):
    try:
        if cn_id in connection_up_time:
            if connection_up_time[cn_id] + CONNECTION_ACTIVATION_RETRY_TIMEOUT > datetime.datetime.now():
                return None
        cn_obj = nm.find_connection(cn_id)
        if not cn_obj:
            logging.debug('"%s" not found', cn_id)
            return None
        logging.debug('Try to activate "%s"', cn_id)
        settings = cn_obj.GetSettings()
        cn_path = None
        cn_type = settings["connection"]["type"]
        if cn_type == "gsm":
            cn_path = activate_gsm_connection(nm, cn_obj)
        elif cn_type == "802-3-ethernet":
            cn_path = activate_generic_connection(nm, cn_obj)
        elif cn_type == "802-11-wireless":
            cn_path = activate_generic_connection(nm, cn_obj)
        else:
            connection_up_time[cn_id] = datetime.datetime.now()
            return None
        connection_up_time[cn_id] = datetime.datetime.now()
        return cn_path
    except Exception as ex:
        logging.debug(ex)
        connection_up_time[cn_id] = datetime.datetime.now()
        return None

def deactivate_connection(nm, active_cn_path):
    try:
        nm.deactivate_connection(active_cn_path)
        wait_connection_deactivation(nm, active_cn_path, CONNECTION_DEACTIVATION_TIMEOUT)
    except Exception as ex:
        logging.debug(ex)


def deactivate_connections(nm, connections, active_connections):
    for cn_id in connections:
        if cn_id in active_connections:
            logging.debug('Try to deactivate connection"%s"', cn_id)
            deactivate_connection(nm, active_connections[cn_id])

def check_resource_availability(iface):
    start = datetime.datetime.now()
    while start + PING_TIMEOUT >= datetime.datetime.now():
        if subprocess.call("ping -W 1 -c 3 %s -I %s" % (PING_HOST, iface), shell=True) == 0:
            return True
    logging.debug('Ping "%s" failed', PING_HOST)
    return False

def check():
    nm = NetworkManager()
    active_connections = nm.get_active_connections()
    logging.debug('Active connections')
    logging.debug(active_connections)
    for index, cn_id in enumerate(CONNECTION_PRIORITY):
        active_cn_path = None
        if cn_id in active_connections:
            logging.debug('"%s" is active', cn_id)
            active_cn_path = active_connections[cn_id]
        else:
            active_cn_path = activate_connection(nm, cn_id)
        if active_cn_path:
            ifaces = nm.get_active_connection_ifaces(active_cn_path)
            if len(ifaces):
                if check_resource_availability(ifaces[0]):
                    deactivate_connections(nm, CONNECTION_PRIORITY[index+1:], active_connections)
                    return
            deactivate_connection(nm, active_cn_path)

def main():
    logging.basicConfig(level=logging.DEBUG)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    while True:
        try:
            check()
        except Exception as ex:
            logging.debug(ex)
        time.sleep(CHECK_PERIOD.total_seconds())

if __name__ == "__main__":
    main()
