"""Hub monitor for the Terncy integration."""

import asyncio
import ipaddress
import logging
from typing import ForwardRef

from homeassistant.components import zeroconf as hasszeroconf
from homeassistant.const import CONF_PORT
from zeroconf import ServiceBrowser

from .const import (
    CONF_DEVID,
    CONF_IP,
    CONF_NAME,
    TERNCY_EVENT_SVC_ADD,
    TERNCY_EVENT_SVC_REMOVE,
    TERNCY_EVENT_SVC_UPDATE,
    TERNCY_HUB_ID_PREFIX,
    TERNCY_HUB_SVC_NAME,
)

_LOGGER = logging.getLogger(__name__)


def _device_id_from_name(name: str, svc_type: str) -> str:
    suffix = "." + svc_type
    if name.endswith(suffix):
        return name[: -len(suffix)]
    if "._websocket._tcp" in name:
        return name.split("._websocket._tcp")[0]
    return name


def _parse_svc(dev_id, info):
    txt_records = {CONF_DEVID: dev_id}
    ip_addr = ""
    if info.addresses:
        raw = info.addresses[0]
        if len(raw) == 4:
            ip_addr = str(ipaddress.IPv4Address(raw))
        elif len(raw) == 16:
            ip_addr = str(ipaddress.IPv6Address(raw))
    txt_records[CONF_IP] = ip_addr
    txt_records[CONF_PORT] = info.port
    for key, value in info.properties.items():
        if value is None:
            continue
        text_key = key.decode("utf-8") if isinstance(key, bytes) else str(key)
        text_value = value.decode("utf-8") if isinstance(value, bytes) else str(value)
        txt_records[text_key] = text_value
    if CONF_NAME not in txt_records and "dn" in txt_records:
        txt_records[CONF_NAME] = txt_records["dn"]
    return txt_records


class TerncyZCListener:
    """Terncy zeroconf discovery listener."""

    def __init__(self, manager: ForwardRef("TerncyHubManager")):
        """Create Terncy discovery listener."""
        self.manager = manager

    def remove_service(self, zconf, svc_type, name):
        """Get a terncy service removed event."""
        if not name.startswith(TERNCY_HUB_ID_PREFIX):
            return
        _LOGGER.debug("remove_service %s %s", svc_type, name)
        dev_id = _device_id_from_name(name, svc_type)
        if dev_id in self.manager.hubs:
            del self.manager.hubs[dev_id]
        self.manager.hass.bus.fire(TERNCY_EVENT_SVC_REMOVE, {CONF_DEVID: dev_id})

    def update_service(self, zconf, svc_type, name):
        """Get a terncy service updated event."""
        if not name.startswith(TERNCY_HUB_ID_PREFIX):
            return
        info = zconf.get_service_info(svc_type, name)
        if info is None:
            return
        _LOGGER.debug("update_service %s %s %s", svc_type, name, info)
        dev_id = _device_id_from_name(name, svc_type)
        txt_records = _parse_svc(dev_id, info)
        self.manager.hubs[dev_id] = txt_records
        self.manager.hass.bus.fire(TERNCY_EVENT_SVC_UPDATE, txt_records)

    def add_service(self, zconf, svc_type, name):
        """Get a new terncy service discovered event."""
        if not name.startswith(TERNCY_HUB_ID_PREFIX):
            return
        info = zconf.get_service_info(svc_type, name)
        if info is None:
            _LOGGER.debug("add_service missing info for %s", name)
            return
        _LOGGER.debug("add_service %s %s %s", svc_type, name, info)
        dev_id = _device_id_from_name(name, svc_type)
        txt_records = _parse_svc(dev_id, info)
        self.manager.hubs[dev_id] = txt_records
        self.manager.hass.bus.fire(TERNCY_EVENT_SVC_ADD, txt_records)


class TerncyHubManager:
    """Manager of terncy hubs."""

    __instance = None

    def __init__(self, hass):
        """Create instance of terncy manager, use instance instead."""
        self.hass = hass
        self._browser = None
        self._discovery_engine = None
        self.hubs = {}
        TerncyHubManager.__instance = self

    @staticmethod
    def instance(hass):
        """Get singleton instance of terncy manager."""
        if TerncyHubManager.__instance is None:
            TerncyHubManager(hass)
        return TerncyHubManager.__instance

    def available_hubs(self) -> dict:
        """Return discovered Terncy hubs that already have an IP."""
        return {
            devid: hub
            for devid, hub in self.hubs.items()
            if devid.startswith(TERNCY_HUB_ID_PREFIX) and hub.get(CONF_IP)
        }

    async def start_discovery(self):
        """Start terncy discovery engine."""
        if self._discovery_engine:
            await self._async_seed_from_cache()
            return

        zconf = await hasszeroconf.async_get_instance(self.hass)
        self._discovery_engine = zconf
        listener = TerncyZCListener(self)
        self._browser = ServiceBrowser(zconf, TERNCY_HUB_SVC_NAME, listener)
        await self._async_seed_from_cache()

    async def _async_seed_from_cache(self) -> None:
        """Load already-cached Terncy services from Home Assistant zeroconf."""
        zconf = self._discovery_engine
        if zconf is None:
            return

        def _scan() -> None:
            try:
                names = list(zconf.cache.names())
            except Exception:  # noqa: BLE001 - cache shape varies by zeroconf version
                _LOGGER.debug("zeroconf cache unavailable for seeding")
                return

            for name in names:
                if not name.startswith(TERNCY_HUB_ID_PREFIX):
                    continue
                if TERNCY_HUB_SVC_NAME.rstrip(".") not in name:
                    continue
                info = zconf.get_service_info(TERNCY_HUB_SVC_NAME, name)
                if info is None:
                    continue
                dev_id = _device_id_from_name(name, TERNCY_HUB_SVC_NAME)
                txt_records = _parse_svc(dev_id, info)
                if not txt_records.get(CONF_IP):
                    continue
                self.hubs[dev_id] = txt_records
                _LOGGER.debug("seeded hub from cache: %s %s", dev_id, txt_records)

        await self.hass.async_add_executor_job(_scan)

    async def async_wait_for_hubs(self, timeout: float = 8.0) -> dict:
        """Wait until at least one hub is discovered or timeout elapses."""
        await self.start_discovery()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            hubs = self.available_hubs()
            if hubs:
                return hubs
            if loop.time() >= deadline:
                return self.available_hubs()
            await asyncio.sleep(0.5)

    async def stop_discovery(self):
        """Stop terncy discovery engine."""
        if self._discovery_engine:
            if self._browser:
                self._browser.cancel()
            # Do not close HA's shared Zeroconf instance.
            self._browser = None
            self._discovery_engine = None
