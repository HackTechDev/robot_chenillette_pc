#!/usr/bin/env python3
"""Appairage automatique du robot BatMobile (BlueZ via D-Bus).

Allume l'adaptateur, cherche l'appareil nommé « BatMobile », l'appaire avec le PIN 0000
et le marque de confiance. Ne fait rien s'il est déjà appairé (sauf --repair).
Affiche « ADDRESS=xx:xx:xx:xx:xx:xx » en dernière ligne en cas de succès (lu par batmobile.py).

Usage : python3 bt_pairing.py [--repair] [--timeout 30]
"""

import argparse
import sys

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

ROBOT_NAME = "BatMobile"
ROBOT_PIN = "0000"

BUS = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
AGENT_IFACE = "org.bluez.Agent1"
AGENT_PATH = "/batmobile/agent"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"


def log(msg):
    print(msg, flush=True)


class Agent(dbus.service.Object):
    """Agent BlueZ : répond au PIN demandé par le module (appairage « legacy »)."""

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        log(f"PIN {ROBOT_PIN} envoyé")
        return ROBOT_PIN

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        return dbus.UInt32(int(ROBOT_PIN))

    @dbus.service.method(AGENT_IFACE, in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        pass  # accepte

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        pass

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        pass

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        pass

    @dbus.service.method(AGENT_IFACE, in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        pass

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Cancel(self):
        log("Appairage annulé par BlueZ")

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Release(self):
        pass


class Pairing:
    def __init__(self, repair, timeout):
        self.repair = repair
        self.timeout = timeout
        self.bus = dbus.SystemBus()
        self.loop = GLib.MainLoop()
        self.result = None  # adresse en cas de succès
        self.error = None
        self.discovering = False
        self.pairing = False

        self.om = dbus.Interface(self.bus.get_object(BUS, "/"), OM_IFACE)
        self.adapter_path = self.find_adapter()
        self.adapter = dbus.Interface(self.bus.get_object(BUS, self.adapter_path), ADAPTER_IFACE)
        self.adapter_props = dbus.Interface(self.bus.get_object(BUS, self.adapter_path), PROPS_IFACE)

    def find_adapter(self):
        for path, ifaces in self.om.GetManagedObjects().items():
            if ADAPTER_IFACE in ifaces:
                return path
        raise RuntimeError("Aucun adaptateur Bluetooth trouvé")

    def find_robot(self):
        """(chemin, propriétés) du robot parmi les appareils connus de BlueZ, ou (None, None)."""
        for path, ifaces in self.om.GetManagedObjects().items():
            dev = ifaces.get(DEVICE_IFACE)
            if dev and path.startswith(self.adapter_path) and ROBOT_NAME in (dev.get("Name"), dev.get("Alias")):
                return path, dev
        return None, None

    def run(self):
        if not self.adapter_props.Get(ADAPTER_IFACE, "Powered"):
            log("Allumage de l'adaptateur Bluetooth")
            self.adapter_props.Set(ADAPTER_IFACE, "Powered", dbus.Boolean(True))

        path, dev = self.find_robot()
        if path and self.repair:
            log(f"Suppression de l'appairage existant ({dev['Address']})")
            self.adapter.RemoveDevice(path)
            path = dev = None

        if path and dev.get("Paired"):
            self.trust(path)
            log(f"{ROBOT_NAME} déjà appairé ({dev['Address']})")
            return str(dev["Address"])

        agent = Agent(self.bus, AGENT_PATH)
        manager = dbus.Interface(self.bus.get_object(BUS, "/org/bluez"), "org.bluez.AgentManager1")
        manager.RegisterAgent(AGENT_PATH, "KeyboardDisplay")
        try:
            if path:
                self.pair(path)  # déjà vu par BlueZ : inutile de rechercher
            else:
                self.discover()
            GLib.timeout_add_seconds(self.timeout + 30, self.on_timeout)
            self.loop.run()
        finally:
            self.stop_discovery()
            try:
                manager.UnregisterAgent(AGENT_PATH)
            except dbus.DBusException:
                pass
            agent.remove_from_connection()

        if self.error:
            raise RuntimeError(self.error)
        return self.result

    # --- Recherche ---

    def discover(self):
        log(f"Recherche de {ROBOT_NAME}… (robot allumé, à portée)")
        self.bus.add_signal_receiver(self.on_interfaces_added, "InterfacesAdded", OM_IFACE)
        self.bus.add_signal_receiver(self.on_props_changed, "PropertiesChanged", PROPS_IFACE,
                                     path_keyword="path")
        try:
            self.adapter.SetDiscoveryFilter({"Transport": "bredr"})  # le Bluetooth Bee est en classique
        except dbus.DBusException:
            pass
        self.adapter.StartDiscovery()
        self.discovering = True
        GLib.timeout_add_seconds(self.timeout, self.on_discovery_timeout)

    def stop_discovery(self):
        if self.discovering:
            self.discovering = False
            try:
                self.adapter.StopDiscovery()
            except dbus.DBusException:
                pass

    def on_interfaces_added(self, path, ifaces):
        dev = ifaces.get(DEVICE_IFACE)
        if dev and ROBOT_NAME in (dev.get("Name"), dev.get("Alias")):
            self.found(path, dev.get("Address"))

    def on_props_changed(self, iface, changed, invalidated, path=None):
        # Le nom arrive parfois après la création de l'appareil
        if iface == DEVICE_IFACE and changed.get("Name") == ROBOT_NAME:
            self.found(path, None)

    def found(self, path, address):
        if self.pairing:
            return
        log(f"{ROBOT_NAME} trouvé" + (f" ({address})" if address else ""))
        self.stop_discovery()
        self.pair(path)

    def on_discovery_timeout(self):
        if self.discovering and not self.pairing:
            self.fail(f"{ROBOT_NAME} introuvable après {self.timeout} s : robot allumé et à portée ?")
        return False

    def on_timeout(self):
        self.fail("Délai dépassé")
        return False

    # --- Appairage ---

    def pair(self, path):
        self.pairing = True
        log(f"Appairage (PIN {ROBOT_PIN})…")
        device = dbus.Interface(self.bus.get_object(BUS, path), DEVICE_IFACE)
        device.Pair(reply_handler=lambda: self.on_paired(path),
                    error_handler=lambda e: self.on_pair_error(path, e),
                    timeout=60)

    def on_paired(self, path):
        self.trust(path)
        props = dbus.Interface(self.bus.get_object(BUS, path), PROPS_IFACE)
        self.result = str(props.Get(DEVICE_IFACE, "Address"))
        log(f"{ROBOT_NAME} appairé ({self.result})")
        self.loop.quit()

    def on_pair_error(self, path, e):
        name = e.get_dbus_name()
        if name == "org.bluez.Error.AlreadyExists":
            self.on_paired(path)
        elif name in ("org.bluez.Error.ConnectionAttemptFailed", "org.bluez.Error.AuthenticationTimeout"):
            self.fail(f"Robot injoignable ({e.get_dbus_message()}) : robot allumé et à portée ?")
        elif name in ("org.bluez.Error.AuthenticationFailed", "org.bluez.Error.AuthenticationRejected"):
            self.fail(f"PIN refusé ({e.get_dbus_message()}) : réessayer avec --repair")
        else:
            self.fail(f"Échec de l'appairage : {e.get_dbus_message() or name}")

    def trust(self, path):
        props = dbus.Interface(self.bus.get_object(BUS, path), PROPS_IFACE)
        if not props.Get(DEVICE_IFACE, "Trusted"):
            props.Set(DEVICE_IFACE, "Trusted", dbus.Boolean(True))
            log(f"{ROBOT_NAME} marqué de confiance")

    def fail(self, msg):
        if self.error is None and self.result is None:
            self.error = msg
            self.loop.quit()


def main():
    parser = argparse.ArgumentParser(description=f"Appaire automatiquement le robot {ROBOT_NAME} (PIN {ROBOT_PIN}).")
    parser.add_argument("--repair", action="store_true", help="supprimer l'appairage existant et recommencer")
    parser.add_argument("--timeout", type=int, default=30, help="durée maximale de recherche en secondes (défaut 30)")
    args = parser.parse_args()

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    try:
        address = Pairing(args.repair, args.timeout).run()
    except (RuntimeError, dbus.DBusException) as e:
        msg = e.get_dbus_message() if isinstance(e, dbus.DBusException) else str(e)
        print(f"Erreur : {msg}", file=sys.stderr, flush=True)
        sys.exit(1)
    log(f"ADDRESS={address}")


if __name__ == "__main__":
    main()
