import dbus
import time
import signal
import datetime
import logging
from network_manager import NetworkManager, NM_CONNECTIVITY_FULL
from modem_manager import ModemManager
from collections import namedtuple

# Settings
CHECK_PERIOD = datetime.timedelta(seconds=5)
CONNECTION_ACTIVATION_RETRY_TIMEOUT = datetime.timedelta(seconds=60)
CONNECTION_ACTIVATION_TIMEOUT = datetime.timedelta(seconds=30)
CONNECTION_DEACTIVATION_TIMEOUT = datetime.timedelta(seconds=30)
CONNECTION_PRIORITY = ["wb-eth0", "wb-eth1", "wb-wifi", "wb-gsm-sim1", "wb-gsm-sim2"]

#NMActiveConnectionState
NM_ACTIVE_CONNECTION_STATE_UNKNOWN = 0
NM_ACTIVE_CONNECTION_STATE_ACTIVATING = 1
NM_ACTIVE_CONNECTION_STATE_ACTIVATED = 2
NM_ACTIVE_CONNECTION_STATE_DEACTIVATING = 3
NM_ACTIVE_CONNECTION_STATE_DEACTIVATED = 4

ActivateConnectionResult = namedtuple('ActivateConnectionResult', ['path', 'deactivated_connection_id'])

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
        except dbus.exceptions.DBusException as ex:
            if "org.freedesktop.DBus.Error.UnknownMethod" == ex.get_dbus_name():
                # Connection object is already unexported
                return
        time.sleep(1)

def activate_gsm_connection(nm, cn_obj, cn_id):
    dev = nm.find_device_for_connection(cn_obj)
    if not dev:
        logging.debug('Device for connection "%s" is not found', cn_id)
        return ActivateConnectionResult(None, False)
    dev_path = nm.get_device_property(dev, "Udi")
    logging.debug('Device path "%s"', dev_path)
    # Switching SIM card while other connection is active can cause NM restart
    # So deactivate active connection if it exists
    old_active_connection_id = None
    active_connection_path = nm.get_active_connection_path(dev)
    if active_connection_path:
        old_active_connection_id = nm.get_active_connection_id(active_connection_path)
        logging.debug('Deactivate active connection "%s"', old_active_connection_id)
        nm.deactivate_connection(active_connection_path)
        wait_connection_deactivation(nm, active_connection_path, CONNECTION_DEACTIVATION_TIMEOUT)
    mm = ModemManager()
    if mm.set_primary_sim_slot(dev_path, get_sim_slot(cn_obj)):
        # After switching SIM card MM recreates device with new path
        dev = wait_device_for_connection(nm, cn_obj, datetime.timedelta(seconds=30))
        if not dev:
            logging.debug('Device for connection "%s" is not found', cn_id)
            return ActivateConnectionResult(None, old_active_connection_id)
        dev_path = nm.get_device_property(dev, "Udi")
        logging.debug('Device path after SIM switching "%s"', dev_path)
        active_connection_path = nm.activate_connection(cn_obj, dev)
        if wait_connection_activation(nm, active_connection_path, CONNECTION_ACTIVATION_TIMEOUT):
            return ActivateConnectionResult(active_connection_path, old_active_connection_id)
    return ActivateConnectionResult(None, old_active_connection_id)

def activate_generic_connection(nm, cn_obj, cn_id):
    dev = nm.find_device_for_connection(cn_obj)
    if not dev:
        logging.debug('Device for connection "%s" is not found', cn_id)
        return ActivateConnectionResult(None, None)
    active_connection_path = nm.activate_connection(cn_obj, dev)
    if wait_connection_activation(nm, active_connection_path, CONNECTION_ACTIVATION_TIMEOUT):
        return ActivateConnectionResult(active_connection_path, None)
    return ActivateConnectionResult(None, None)

def is_time_to_activate(cn_id):
    if cn_id in connection_up_time:
        if connection_up_time[cn_id] + CONNECTION_ACTIVATION_RETRY_TIMEOUT > datetime.datetime.now():
            return False
    return True

def activate_connection(nm, cn_id):
    cn = ActivateConnectionResult(None, False)
    activation_fns = {
        "gsm": activate_gsm_connection,
        "802-3-ethernet": activate_generic_connection,
        "802-11-wireless": activate_generic_connection
    }
    try:
        cn_obj = nm.find_connection(cn_id)
        if not cn_obj:
            logging.debug('"%s" not found', cn_id)
            return cn
        logging.debug('Activate connection "%s"', cn_id)
        settings = cn_obj.GetSettings()
        activate_fn = activation_fns.get(settings["connection"]["type"])
        if activate_fn:
            cn = activate_fn(nm, cn_obj)
        return cn
    except Exception as ex:
        logging.debug(ex)
        return cn

def deactivate_connection(nm, active_cn_path):
    try:
        nm.deactivate_connection(active_cn_path)
        wait_connection_deactivation(nm, active_cn_path, CONNECTION_DEACTIVATION_TIMEOUT)
    except Exception as ex:
        logging.debug(ex)

def get_active_connections(connection_ids, active_connections):
    res = {}
    for cn_id, cn_path in active_connections.items():
        if cn_id in connection_ids:
            res[cn_id] = cn_path
    return res

def deactivate_connections(nm, connections):
    for cn_id, cn_path in connections.items():
        logging.debug('Deactivate connection "%s"', cn_id)
        deactivate_connection(nm, cn_path)

def deactivate_if_limited_connectivity(nm, active_cn_path):
    ip4_connectivity = nm.get_ip4_connectivity(nm, active_cn_path)
    logging.debug('IPv4 connectivity = %s', ip4_connectivity.name)
    if ip4_connectivity == NM_CONNECTIVITY_FULL:
        return False
    deactivate_connection(nm, active_cn_path)
    return True

def check():
    nm = NetworkManager()
    active_connections = nm.get_active_connections()
    logging.debug('Active connections')
    logging.debug(active_connections)
    for index, cn_id in enumerate(CONNECTION_PRIORITY):
        if cn_id in active_connections:
            logging.debug('"%s" is active', cn_id)
            if not deactivate_if_limited_connectivity(nm, active_connections[cn_id]):
                less_priority_connections = get_active_connections(CONNECTION_PRIORITY[index + 1:], active_connections)
                deactivate_connections(nm, less_priority_connections)
                return
        else:
            if is_time_to_activate(cn_id):
                active_cn = activate_connection(nm, cn_id)
                connection_up_time[cn_id] = datetime.datetime.now()
                if active_cn.path:
                    if not deactivate_if_limited_connectivity(nm, active_cn.path):
                        less_priority_connections = get_active_connections(CONNECTION_PRIORITY[index + 1:], active_connections)
                        deactivate_connections(nm, less_priority_connections)
                        return
                if active_cn.deactivated_connection_id:
                    connection_up_time[active_cn.deactivated_connection_id] = datetime.datetime.now() - CONNECTION_ACTIVATION_RETRY_TIMEOUT
                    return

def main():
    logging.basicConfig(level=logging.DEBUG)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    while True:
        try:
            check()
        except Exception as ex:
            logging.critical(ex, exc_info=True)
            exit(1)
        time.sleep(CHECK_PERIOD.total_seconds())

if __name__ == "__main__":
    main()
