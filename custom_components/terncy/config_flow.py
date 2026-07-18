"""Config flow for Terncy integration."""

import logging
import uuid
from typing import Any

import terncy
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, OptionsFlow
from homeassistant.const import (
    CONF_DEVICE,
    CONF_HOST,
    CONF_PORT,
    MAJOR_VERSION,
    MINOR_VERSION,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_EXPORT_DEVICE_GROUPS,
    CONF_EXPORT_SCENES,
    CONF_IP,
    CONF_NAME,
    DOMAIN,
    TERNCY_HUB_SVC_NAME,
)
from .hub_monitor import TerncyHubManager

if (MAJOR_VERSION, MINOR_VERSION) >= (2025, 2):
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
else:
    from homeassistant.components.zeroconf import ZeroconfServiceInfo

_LOGGER = logging.getLogger(__name__)

MANUAL_ENTRY = "manual"
DISCOVERY_TIMEOUT = 8.0
DEFAULT_PORT = 443


async def _start_discovery(mgr: TerncyHubManager) -> None:
    await mgr.start_discovery()


def _get_discovered_devices(mgr: TerncyHubManager | None) -> dict:
    return {} if mgr is None else mgr.available_hubs()


def _hub_label(hub: dict) -> str:
    name = hub.get(CONF_NAME) or hub.get(CONF_IP) or "Terncy Hub"
    ip = hub.get(CONF_IP)
    if ip:
        return f"{name} ({ip})"
    return name


def _parse_identifier(service_name: str) -> str:
    """Extract hub id from a zeroconf service name."""
    identifier = service_name
    suffix = "." + TERNCY_HUB_SVC_NAME
    if identifier.endswith(suffix):
        return identifier[: -len(suffix)]
    if "._websocket._tcp" in identifier:
        return identifier.split("._websocket._tcp")[0]
    return identifier


class TerncyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Terncy."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_PUSH

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovered_devices = {}
        self._discovery_waited = False

        self.username = "ha_user_" + uuid.uuid4().hex[0:5]
        self.client_id = "homeass_nbhQ43"
        self.identifier = ""
        self.name = ""
        self.host = ""
        self.port = DEFAULT_PORT
        self.token = ""
        self.token_id = 0
        self.context = {}
        self.terncy = terncy.Terncy(
            self.client_id,
            self.identifier,
            self.host,
            self.port,
            self.username,
            "VALID_TOKEN_NOT_ACQUIRED",
        )

    def _configure_terncy(self) -> None:
        self.terncy = terncy.Terncy(
            self.client_id,
            self.identifier,
            self.host,
            self.port,
            self.username,
            "",
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Handle the initial step."""
        _LOGGER.debug("async_step_user: %s", user_input)
        mgr = TerncyHubManager.instance(self.hass)
        await _start_discovery(mgr)

        if user_input is not None and CONF_DEVICE in user_input:
            devid = user_input[CONF_DEVICE]
            if devid == MANUAL_ENTRY:
                return await self.async_step_manual()

            hub = _get_discovered_devices(mgr).get(devid) or mgr.hubs.get(devid)
            if hub is None or not hub.get(CONF_IP):
                return await self.async_step_manual()

            self.identifier = devid
            self.name = hub.get(CONF_NAME) or devid
            self.host = hub[CONF_IP]
            self.port = hub.get(CONF_PORT, DEFAULT_PORT)
            _LOGGER.debug("construct Terncy obj for %s %s", self.name, self.host)
            self._configure_terncy()
            return self.async_show_form(
                step_id="begin_pairing",
                description_placeholders={"name": self.name},
            )

        if not self._discovery_waited:
            self._discovery_waited = True
            await mgr.async_wait_for_hubs(DISCOVERY_TIMEOUT)

        configured_ids = {
            entry.unique_id
            for entry in self._async_current_entries()
            if entry.unique_id
        }
        devices_name: dict[str, str] = {}
        for devid, hub in _get_discovered_devices(mgr).items():
            if devid in configured_ids:
                continue
            devices_name[devid] = _hub_label(hub)

        if not devices_name:
            if configured_ids and _get_discovered_devices(mgr):
                _LOGGER.debug("all discovered hubs already configured")
                return self.async_abort(reason="already_configured")
            _LOGGER.debug("no hubs discovered, falling back to manual setup")
            return await self.async_step_manual()

        language = (self.hass.config.language or "").lower()
        devices_name[MANUAL_ENTRY] = (
            "手动输入 IP" if language.startswith("zh") else "Manual IP setup"
        )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_DEVICE): vol.In(devices_name)}),
        )

    async def async_step_manual(self, user_input: dict[str, Any] | None = None):
        """Handle manual host configuration when discovery is empty or skipped."""
        errors: dict[str, str] = {}

        if user_input is not None:
            identifier = user_input["identifier"].strip()
            host = user_input[CONF_HOST].strip()
            port = int(user_input.get(CONF_PORT, DEFAULT_PORT))
            name = (user_input.get("name") or identifier).strip()

            if not identifier or not host:
                errors["base"] = "invalid_manual_input"
            else:
                await self.async_set_unique_id(identifier)
                self._abort_if_unique_id_configured(updates={CONF_HOST: host})

                self.identifier = identifier
                self.name = name
                self.host = host
                self.port = port
                self._configure_terncy()
                return self.async_show_form(
                    step_id="begin_pairing",
                    description_placeholders={"name": self.name},
                )

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): str,
                    vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                    vol.Required("identifier"): str,
                    vol.Optional("name", default="Terncy Hub"): str,
                }
            ),
            errors=errors,
        )

    async def async_step_begin_pairing(self, user_input=None):
        """Start pairing process for the next available protocol."""
        _LOGGER.debug("async_step_begin_pairing: %s", user_input)
        if self.unique_id is None:
            await self.async_set_unique_id(self.identifier)
            self._abort_if_unique_id_configured(updates={CONF_HOST: self.host})

        if self.token == "":
            _LOGGER.warning("request a new token form terncy %s", self.identifier)
            code, token_id, token, state = await self.terncy.request_token(
                self.username, "HA User"
            )
            self.token = token
            self.token_id = token_id
            self.terncy.token = token

        code, state = await self.terncy.check_token_state(self.token_id, self.token)
        if code != 200:
            _LOGGER.warning("current token invalid, clear it")
            self.token = ""
            self.token_id = 0
            return self.async_show_form(
                step_id="begin_pairing",
                description_placeholders={"name": self.name},
                errors={"base": "need_new_auth"},
            )
        if state == terncy.TokenState.APPROVED.value:
            _LOGGER.warning("token valid, create entry for %s", self.identifier)
            return self.async_create_entry(
                title=self.name,
                data={
                    "identifier": self.identifier,
                    "username": self.username,
                    "token": self.token,
                    "token_id": self.token_id,
                    "host": self.host,
                    "port": self.port,
                },
            )
        return self.async_show_form(
            step_id="begin_pairing",
            description_placeholders={"name": self.name},
            errors={"base": "invalid_auth"},
        )

    async def async_step_confirm(self, user_input=None):
        """Handle user-confirmation of discovered node."""
        _LOGGER.debug("async_step_confirm: %s", user_input)
        if user_input is not None:
            return await self.async_step_begin_pairing()
        return self.async_show_form(
            step_id="confirm", description_placeholders={"name": self.name}
        )

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo):
        """Prepare configuration for a discovered Terncy device."""
        _LOGGER.debug("async_step_zeroconf: %s", discovery_info)
        identifier = _parse_identifier(discovery_info.name)
        await self.async_set_unique_id(identifier)
        self._abort_if_unique_id_configured(updates={CONF_HOST: discovery_info.host})

        properties = discovery_info.properties or {}
        _LOGGER.debug("zeroconf properties: %s", properties)

        name = (
            properties.get(CONF_NAME)
            or properties.get("dn")
            or properties.get("name")
            or identifier
        )

        self.context["identifier"] = self.unique_id
        self.context["title_placeholders"] = {"name": name}
        self.identifier = identifier
        self.name = name
        self.host = discovery_info.host
        self.port = discovery_info.port or DEFAULT_PORT
        self.terncy.ip = self.host
        self.terncy.port = self.port

        mgr = TerncyHubManager.instance(self.hass)
        _LOGGER.debug("start discovery engine of domain %s", DOMAIN)
        await _start_discovery(mgr)
        return await self.async_step_confirm()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(OptionsFlow):
    def __init__(self, config_entry: ConfigEntry):
        """Initialize options flow."""
        if (MAJOR_VERSION, MINOR_VERSION) < (2024, 12):
            self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        _LOGGER.debug("Options step init: %s", user_input)
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        export_device_groups = self.config_entry.options.get(
            CONF_EXPORT_DEVICE_GROUPS, True
        )
        export_scenes = self.config_entry.options.get(CONF_EXPORT_SCENES, False)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_EXPORT_DEVICE_GROUPS, default=export_device_groups
                    ): bool,
                    vol.Required(CONF_EXPORT_SCENES, default=export_scenes): bool,
                }
            ),
        )
