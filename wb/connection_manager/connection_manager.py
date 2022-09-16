import dbus
import time
import signal
import datetime
import logging
from network_manager import NetworkManager, NM_CONNECTIVITY_FULL
from modem_manager import ModemManager
import pycurl
from io import BytesIO

# Settings
CHECK_PERIOD = datetime.timedelta(seconds=5)
CONNECTION_ACTIVATION_RETRY_TIMEOUT = datetime.timedelta(seconds=60)
CONNECTION_ACTIVATION_TIMEOUT = datetime.timedelta(seconds=30)
CONNECTION_DEACTIVATION_TIMEOUT = datetime.timedelta(seconds=30)
CONNECTIVITY_CHECK_URL = "http://network-test.debian.org/nm"
CONNECTION_PRIORITY = ["wb-eth0", "wb-eth1", "wb-wifi", "wb-gsm-sim1", "wb-gsm-sim2"]
LOG_LEVEL = logging.DEBUG

#NMActiveConnectionState
NM_ACTIVE_CONNECTION_STATE_UNKNOWN = 0
NM_ACTIVE_CONNECTION_STATE_ACTIVATING = 1
NM_ACTIVE_CONNECTION_STATE_ACTIVATED = 2
NM_ACTIVE_CONNECTION_STATE_DEACTIVATING = 3
NM_ACTIVE_CONNECTION_STATE_DEACTIVATED = 4

connection_up_time = dict()

class ConnectionStateFilter(logging.Filter):
    def __init__(self):
        self.last_event = {}

    def filter(self, record):
        if 'cn_id' in record.__dict__:
            cn_id = record.__dict__['cn_id']
            if cn_id in self.last_event:
                if self.last_event[cn_id] == record.msg:
                    return False
            self.last_event[cn_id] = record.msg
        return True

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
        except dbus.exceptions.DBusException as ex:
            # Some exceptions can be raised during waiting, because MM and NM remove and create devices
            logging.debug('Error during device waiting: %s', ex)
        time.sleep(1)
    return None

def wait_connection_activation(nm, cn_path, timeout):
    logging.debug('Waiting for connection activation')
    start = datetime.datetime.now()
    while start + timeout >= datetime.datetime.now():
        current_state = nm.get_active_connection_property(cn_path, "State")
        if current_state == NM_ACTIVE_CONNECTION_STATE_ACTIVATED:
            return True
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

def activate_gsm_connection(nm, dev, cn_obj):
    dev_path = nm.get_device_property(dev, "Udi")
    logging.debug('Device path "%s"', dev_path)
    # Switching SIM card while other connection is active can cause NM restart
    # So deactivate active connection if it exists
    active_connection_path = nm.get_active_connection_path(dev)
    if active_connection_path:
        old_active_connection_id = nm.get_active_connection_id(active_connection_path)
        logging.debug('Deactivate active connection "%s"', old_active_connection_id)
        connection_up_time[old_active_connection_id] = datetime.datetime.now() - CONNECTION_ACTIVATION_RETRY_TIMEOUT
        nm.deactivate_connection(active_connection_path)
        wait_connection_deactivation(nm, active_connection_path, CONNECTION_DEACTIVATION_TIMEOUT)
    mm = ModemManager()
    if mm.set_primary_sim_slot(dev_path, get_sim_slot(cn_obj)):
        # After switching SIM card MM recreates device with new path
        dev = wait_device_for_connection(nm, cn_obj, datetime.timedelta(seconds=30))
        if not dev:
            logging.debug('New device for connection is not found')
            return None
        dev_path = nm.get_device_property(dev, "Udi")
        logging.debug('Device path after SIM switching "%s"', dev_path)
        active_connection_path = nm.activate_connection(cn_obj, dev)
        if wait_connection_activation(nm, active_connection_path, CONNECTION_ACTIVATION_TIMEOUT):
            return active_connection_path
    return None

def activate_generic_connection(nm, dev, cn_obj):
    active_connection_path = nm.activate_connection(cn_obj, dev)
    if wait_connection_activation(nm, active_connection_path, CONNECTION_ACTIVATION_TIMEOUT):
        return active_connection_path
    return None

def is_time_to_activate(cn_id):
    if cn_id in connection_up_time:
        if connection_up_time[cn_id] + CONNECTION_ACTIVATION_RETRY_TIMEOUT > datetime.datetime.now():
            return False
    return True

def activate_connection(nm, cn_id):
    activation_fns = {
        "gsm": activate_gsm_connection,
        "802-3-ethernet": activate_generic_connection,
        "802-11-wireless": activate_generic_connection
    }
    cn_obj = nm.find_connection(cn_id)
    if not cn_obj:
        logging.debug('"%s" is not found', cn_id)
        return None
    logging.debug('Activate connection "%s"', cn_id)
    dev = nm.find_device_for_connection(cn_obj)
    if not dev:
        logging.debug('Device for connection "%s" is not found', cn_id)
        return None
    settings = cn_obj.GetSettings()
    activate_fn = activation_fns.get(settings["connection"]["type"])
    if activate_fn:
        cn = activate_fn(nm, dev, cn_obj)
    return cn

def deactivate_connection(nm, active_cn_path):
    nm.deactivate_connection(active_cn_path)
    wait_connection_deactivation(nm, active_cn_path, CONNECTION_DEACTIVATION_TIMEOUT)

def get_active_connections(connection_ids, active_connections):
    res = {}
    for cn_id, cn_path in active_connections.items():
        if cn_id in connection_ids:
            res[cn_id] = cn_path
    return res

def deactivate_connections(nm, connections):
    for cn_id, cn_path in connections.items():
        deactivate_connection(nm, cn_path)
        d = {'cn_id': cn_id}
        logging.info('"%s" is deactivated', cn_id, extra=d)

# NM reports limited connectivity for all gsm ppp connections
# Use the implementation after fixing the bug
#
# def check_connectivity(nm, active_cn_path):
#     ip4_connectivity = nm.get_ip4_connectivity(active_cn_path)
#     logging.debug('IPv4 connectivity = %d', ip4_connectivity)
#     return ip4_connectivity == NM_CONNECTIVITY_FULL

def curl_get(iface, url):
    buffer = BytesIO()
    c = pycurl.Curl()
    c.setopt(c.URL, url)
    c.setopt(c.WRITEDATA, buffer)
    c.setopt(c.INTERFACE, iface)
    c.perform()
    c.close()
    return buffer.getvalue().decode('UTF-8')

# Simple implementation that mimics NM behavior
def check_connectivity(nm, active_cn_path):
    ifaces = nm.get_active_connection_ifaces(active_cn_path)
    if len(ifaces):
        try:
            return curl_get(ifaces[0], CONNECTIVITY_CHECK_URL).startswith('NetworkManager is online')
        except pycurl.error as ex:
            logging.debug('Error during connectivity check: %s', ex)
    return False

def deactivate_if_limited_connectivity(nm, active_cn_path):
    if check_connectivity(nm, active_cn_path):
        return False
    deactivate_connection(nm, active_cn_path)
    return True

def check():
    nm = NetworkManager()
    for index, cn_id in enumerate(CONNECTION_PRIORITY):
        d = {'cn_id': cn_id}
        try:
            active_connections = nm.get_active_connections()
            logging.debug('Active connections')
            logging.debug(active_connections)
            active_cn_path = None
            if cn_id in active_connections:
                active_cn_path = active_connections[cn_id]
            else:
                if is_time_to_activate(cn_id):
                    active_cn_path = activate_connection(nm, cn_id)
                    connection_up_time[cn_id] = datetime.datetime.now()
            if active_cn_path:
                if not deactivate_if_limited_connectivity(nm, active_cn_path):
                    logging.info('"%s" is active', cn_id, extra=d)
                    try:
                        less_priority_connections = get_active_connections(CONNECTION_PRIORITY[index + 1:], active_connections)
                        deactivate_connections(nm, less_priority_connections)
                    except Exception as ex:
                        # Not a problem if less priority connections still be active
                        logging.debug('Error during connections deactivation: %s', ex)
                    return
                else:
                    logging.info('"%s" has limited connectivity', cn_id, extra=d)
        # Something went wrong during connection checking. 
        # Proceed to next connection to be always on-line
        except dbus.exceptions.DBusException as ex: 
            logging.warning('Error during connection "%s" checking: %s', cn_id, ex, extra=d)
            connection_up_time[cn_id] = datetime.datetime.now()
        except Exception as ex:
            logging.critical('Error during connection "%s" checking: %s', cn_id, ex, extra=d, exc_info=True)
            connection_up_time[cn_id] = datetime.datetime.now()

def main():
    if LOG_LEVEL > logging.DEBUG: 
        logger = logging.getLogger()
        logger.addFilter(ConnectionStateFilter())
    logging.basicConfig(level=LOG_LEVEL)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    while True:
        check()
        time.sleep(CHECK_PERIOD.total_seconds())

if __name__ == "__main__":
    main()
