import dbus
import logging

class ModemManager:
    def __init__(self):
        self.bus = dbus.SystemBus()
        self.mm_proxy = self.bus.get_object("org.freedesktop.ModemManager1", "/org/freedesktop/ModemManager1")

    def set_primary_sim_slot(self, modem_path, slot_index):
        objects = dbus.Interface(self.mm_proxy, "org.freedesktop.DBus.ObjectManager")
        for obj in objects.GetManagedObjects():
            if obj == modem_path:
                modem_proxy = self.bus.get_object("org.freedesktop.ModemManager1", obj)
                modem = dbus.Interface(modem_proxy, "org.freedesktop.ModemManager1.Modem")
                modem.SetPrimarySimSlot(slot_index)
                return True
        return False
