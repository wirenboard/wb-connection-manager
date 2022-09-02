import dbus
import datetime
import logging

def connection_type_to_device_type(cn_type):
    types = {
        "gsm": 8,
        "802-3-ethernet": 1,
        "802-11-wireless": 2
    }
    return types.get(cn_type, 0)

class NetworkManager:
    def __init__(self):
        self.bus = dbus.SystemBus()
        self.nm_proxy = self.bus.get_object("org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager")
        self.nm = dbus.Interface(self.nm_proxy, "org.freedesktop.NetworkManager")

    def find_connection(self, cn_id):
        settings_proxy = self.bus.get_object("org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager/Settings")
        settings = dbus.Interface(settings_proxy, "org.freedesktop.NetworkManager.Settings")
        for path in settings.ListConnections():
            con_proxy = self.bus.get_object("org.freedesktop.NetworkManager", path)
            c_obj = dbus.Interface(con_proxy, "org.freedesktop.NetworkManager.Settings.Connection")
            settings = c_obj.GetSettings()
            if str(settings["connection"]["id"]) == cn_id:
                return c_obj
        return None

    def get_active_connections(self):
        res = {}
        mgr_props = dbus.Interface(self.nm_proxy, "org.freedesktop.DBus.Properties")
        active = mgr_props.Get("org.freedesktop.NetworkManager", "ActiveConnections")
        for a in active:
            cn_path = self.get_active_connection_property(a, "Connection")
            cn_proxy = self.bus.get_object("org.freedesktop.NetworkManager", cn_path)
            connection = dbus.Interface(cn_proxy, "org.freedesktop.NetworkManager.Settings.Connection")
            settings = connection.GetSettings()
            res[str(settings["connection"]["id"])] = a
        return res

    def find_device_by_param(self, param_name, param_value):
        devices = self.nm.GetDevices()
        for d in devices:
            if self.get_device_property(d, param_name) == param_value:
                return d
        return None

    def find_device_for_connection(self, cn_obj):
        settings = cn_obj.GetSettings()
        param = "Interface"
        value = settings["connection"].get("interface-name", "")
        if not value:
            param = "DeviceType"
            value = connection_type_to_device_type(settings["connection"]["type"])
        dev = self.find_device_by_param(param, value)
        if not dev:
            logging.debug('Device for connection "%s" is not found', settings["connection"]["id"])
            return None
        return dev

    def get_device_property(self, device_path, property_name):
        dev_proxy = self.bus.get_object("org.freedesktop.NetworkManager", device_path)
        prop_iface = dbus.Interface(dev_proxy, "org.freedesktop.DBus.Properties")
        return prop_iface.Get("org.freedesktop.NetworkManager.Device", property_name)

    def get_active_connection_property(self, active_cn_path, property_name):
        cn_proxy = self.bus.get_object("org.freedesktop.NetworkManager", active_cn_path)
        prop_iface = dbus.Interface(cn_proxy, "org.freedesktop.DBus.Properties")
        return prop_iface.Get("org.freedesktop.NetworkManager.Connection.Active", property_name)

    def activate_connection(self, cn_obj, dev_obj):
        return self.nm.ActivateConnection(cn_obj, dev_obj, "/")

    def deactivate_connection(self, cn_path):
        cn_obj = self.bus.get_object("org.freedesktop.NetworkManager", cn_path)
        self.nm.DeactivateConnection(cn_obj)

    def get_active_connection_path(self, device_path):
        cn_path = self.get_device_property(device_path, "ActiveConnection")
        if cn_path == "/":
            return None
        return cn_path

    def get_active_connection_ifaces(self, active_connection_path):
        res = []
        for dev_path in self.get_active_connection_property(active_connection_path, "Devices"):
            res.append(self.get_device_property(dev_path, "IpInterface"))
        return res
