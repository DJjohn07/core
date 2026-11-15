"""Support for ZoneMinder."""

from dataclasses import dataclass
import logging
from typing import Any

import probatio
from requests.exceptions import (
    ConnectionError as RequestsConnectionError,
    RequestException,
    Timeout as RequestsTimeout,
)
from yarl import URL
from zoneminder.monitor import Monitor, MonitorState, TimePeriod
from zoneminder.zm import ZoneMinder

from homeassistant.config import config_per_platform
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import (
    CONF_COMMAND_OFF,
    CONF_COMMAND_ON,
    CONF_HOST,
    CONF_MONITORED_CONDITIONS,
    CONF_PASSWORD,
    CONF_PATH,
    CONF_SSL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    Platform,
)
from homeassistant.core import DOMAIN as HOMEASSISTANT_DOMAIN, HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.issue_registry import IssueSeverity, async_create_issue
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_INCLUDE_ARCHIVED,
    CONF_PATH_ZMS,
    DEFAULT_PATH,
    DEFAULT_PATH_ZMS,
    DEFAULT_SSL,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
)
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.CAMERA,
    Platform.SENSOR,
    Platform.SWITCH,
]

HOST_CONFIG_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_HOST): cv.string,
        probatio.Optional(probatio.Secret(CONF_PASSWORD)): cv.string,
        probatio.Optional(CONF_PATH, default=DEFAULT_PATH): cv.string,
        probatio.Optional(CONF_PATH_ZMS, default=DEFAULT_PATH_ZMS): cv.string,
        probatio.Optional(CONF_SSL, default=DEFAULT_SSL): cv.boolean,
        probatio.Optional(CONF_USERNAME): cv.string,
        probatio.Optional(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): cv.boolean,
    }
)

CONFIG_SCHEMA = probatio.Schema(
    {DOMAIN: probatio.All(probatio.EnsureList(), [HOST_CONFIG_SCHEMA])},
    extra=probatio.ALLOW_EXTRA,
)

# The sensor and switch platform sections are not validated by their platforms
SENSOR_IMPORT_SCHEMA = probatio.Schema(
    {
        probatio.Optional(CONF_INCLUDE_ARCHIVED): cv.boolean,
        probatio.Optional(CONF_MONITORED_CONDITIONS): probatio.All(
            probatio.EnsureList(),
            [probatio.In([period.period for period in TimePeriod])],
        ),
    },
    extra=probatio.ALLOW_EXTRA,
)
SWITCH_IMPORT_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_COMMAND_ON): probatio.In(
            [state.value for state in MonitorState]
        ),
        probatio.Required(CONF_COMMAND_OFF): probatio.In(
            [state.value for state in MonitorState]
        ),
    },
    extra=probatio.ALLOW_EXTRA,
)


@dataclass
class ZoneMinderData:
    """Runtime data of a ZoneMinder config entry."""

    client: ZoneMinder
    monitors: list[Monitor]


type ZoneMinderConfigEntry = ConfigEntry[ZoneMinderData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the ZoneMinder component."""
    async_setup_services(hass)

    if DOMAIN not in config:
        return True

    options = _platform_options(config)
    for conf in config[DOMAIN]:
        hass.async_create_task(_async_import(hass, {**conf, **options}))

    return True


def _platform_options(config: ConfigType) -> dict[str, Any]:
    """Return the options set in the YAML sensor and switch platform sections."""
    options: dict[str, Any] = {}
    for platform, schema in (
        (Platform.SENSOR, SENSOR_IMPORT_SCHEMA),
        (Platform.SWITCH, SWITCH_IMPORT_SCHEMA),
    ):
        for p_type, p_config in config_per_platform(config, platform):
            if p_type != DOMAIN:
                continue
            try:
                validated = schema(p_config)
            except probatio.Invalid as err:
                _LOGGER.warning(
                    "Ignoring invalid ZoneMinder %s configuration: %s", platform, err
                )
                continue
            if CONF_INCLUDE_ARCHIVED in validated:
                options[CONF_INCLUDE_ARCHIVED] = validated[CONF_INCLUDE_ARCHIVED]
            if CONF_MONITORED_CONDITIONS in validated:
                options[CONF_MONITORED_CONDITIONS] = validated[
                    CONF_MONITORED_CONDITIONS
                ]
            for key in (CONF_COMMAND_ON, CONF_COMMAND_OFF):
                if key in validated:
                    options[key] = validated[key].lower()
    return options


async def _async_import(hass: HomeAssistant, import_data: dict[str, Any]) -> None:
    """Import a ZoneMinder server from YAML and raise the matching issue."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_IMPORT}, data=import_data
    )

    if (
        result["type"] is FlowResultType.ABORT
        and (reason := result["reason"]) != "already_configured"
    ):
        host = import_data[CONF_HOST]
        async_create_issue(
            hass,
            DOMAIN,
            f"deprecated_yaml_import_issue_{reason}_{host}",
            breaks_in_ha_version="2027.6.0",
            is_fixable=False,
            issue_domain=DOMAIN,
            severity=IssueSeverity.WARNING,
            translation_key=f"deprecated_yaml_import_issue_{reason}",
            translation_placeholders={
                "domain": DOMAIN,
                "host": host,
                "integration_title": "ZoneMinder",
            },
        )
        return

    async_create_issue(
        hass,
        HOMEASSISTANT_DOMAIN,
        f"deprecated_yaml_{DOMAIN}",
        breaks_in_ha_version="2027.6.0",
        is_fixable=False,
        issue_domain=DOMAIN,
        severity=IssueSeverity.WARNING,
        translation_key="deprecated_yaml",
        translation_placeholders={
            "domain": DOMAIN,
            "integration_title": "ZoneMinder",
        },
    )


@callback
def async_create_platform_yaml_issue(hass: HomeAssistant, platform: Platform) -> None:
    """Raise an issue for a leftover ZoneMinder platform section in YAML."""
    async_create_issue(
        hass,
        DOMAIN,
        f"deprecated_yaml_platform_{platform}",
        breaks_in_ha_version="2027.6.0",
        is_fixable=False,
        issue_domain=DOMAIN,
        severity=IssueSeverity.WARNING,
        translation_key="deprecated_yaml_platform",
        translation_placeholders={"domain": DOMAIN, "platform": platform},
    )


def create_client(data: dict[str, Any]) -> ZoneMinder:
    """Create a ZoneMinder client from config entry data."""
    server_origin = URL.build(
        scheme="https" if data[CONF_SSL] else "http", authority=data[CONF_HOST]
    )
    return ZoneMinder(
        str(server_origin),
        data.get(CONF_USERNAME),
        data.get(CONF_PASSWORD),
        data[CONF_PATH],
        data[CONF_PATH_ZMS],
        data[CONF_VERIFY_SSL],
    )


async def async_setup_entry(hass: HomeAssistant, entry: ZoneMinderConfigEntry) -> None:
    """Set up ZoneMinder from a config entry."""
    client = create_client(dict(entry.data))

    try:
        logged_in = await hass.async_add_executor_job(client.login)
    except (RequestsConnectionError, RequestsTimeout) as err:
        raise ConfigEntryNotReady(
            f"Unable to connect to ZoneMinder at {entry.data[CONF_HOST]}"
        ) from err
    except RequestException as err:
        raise ConfigEntryError(
            f"Invalid request to ZoneMinder at {entry.data[CONF_HOST]}"
        ) from err

    if not logged_in:
        raise ConfigEntryError(
            f"Invalid credentials for ZoneMinder at {entry.data[CONF_HOST]}"
        )
    if not (monitors := await hass.async_add_executor_job(client.get_monitors)):
        raise ConfigEntryNotReady(
            f"Unable to fetch any monitors from ZoneMinder at {entry.data[CONF_HOST]}"
        )

    entry.runtime_data = ZoneMinderData(client=client, monitors=monitors)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)


async def async_unload_entry(hass: HomeAssistant, entry: ZoneMinderConfigEntry) -> None:
    """Unload a ZoneMinder config entry."""
    await hass.config_entries.async_unload_platforms_or_raise(entry, PLATFORMS)
