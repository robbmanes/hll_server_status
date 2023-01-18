import asyncio
import http.cookies
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import sys
import time
import tomllib
from datetime import datetime, timedelta
from functools import partial, wraps
from pathlib import Path
from typing import Any, Callable

import aiofiles
import aiohttp
import discord
import tomlkit

from hll_server_status import constants
from hll_server_status.models import (
    URL,
    APIConfig,
    AppStore,
    Config,
    DiscordConfig,
    DisplayConfig,
    GameState,
    LoginParameters,
    Map,
    MessageIDFormat,
    OutputConfig,
    ServerName,
    Slots,
)

# logging.basicConfig(level=os.getenv("LOGGING_LEVEL", logging.INFO), stream=sys.stdout)
# logger = logging.getLogger()


def load_config(path: Path) -> Config:
    """Load and validate a TOML config file"""
    raw_config: dict[str, Any]
    with open(path, mode="rb") as fp:
        raw_config = tomllib.load(fp)

    config = Config(
        output=OutputConfig(**raw_config["output"]),
        discord=DiscordConfig(**raw_config["discord"]),
        api=APIConfig(**raw_config["api"]),
        display=DisplayConfig(**raw_config["display"]),
    )

    return config


async def save_message_id(
    app_store: AppStore, table_name: str, key: str, message_id: int
) -> None:
    """Update a webhook message ID in the app_store"""
    app_store.message_ids[table_name][key] = message_id


async def save_message_ids_to_disk(
    app_store: AppStore,
    config: Config,
    path: str | None = None,
    filename: str | None = None,
) -> None:
    """Save the current message IDs for a specific config to disk"""
    if config.output.message_id_directory:
        path = config.output.message_id_directory

    if config.output.message_id_filename:
        filename = config.output.message_id_filename

    if not path:
        path = constants.MESSAGES_DIR

    if not filename:
        filename = app_store.server_identifier + ".toml"

    file = Path(path, filename)
    app_store.logger.info(f"Saving message IDs to {file}")
    async with aiofiles.open(file, mode="w") as fp:
        toml = tomlkit.dumps(app_store.message_ids)
        await fp.write(toml)


async def login(
    config: Config,
    session: aiohttp.ClientSession,
    username: str,
    password: str,
    endpoint: str = "login",
    api_prefix=constants.API_PREFIX,
) -> http.cookies.Morsel[str] | None:
    """Log into CRCON and return the sessionid cookie for future requests"""
    if not username or not password:
        raise ValueError("Username or password not provided.")

    params = LoginParameters(username=username, password=password)
    response = await session.post(
        config.api.base_server_url + api_prefix + endpoint, data=params.as_json()
    )

    if response.status != 200:
        response.raise_for_status()

    return response.cookies.get(constants.SESSION_ID_COOKIE)


def with_login(func: Callable):
    """Wrap functions that call the CRCON API and save the sessionid cookie"""

    @wraps(func)
    async def inner(
        app_store: AppStore,
        config: Config,
        session: aiohttp.ClientSession,
        *args,
        **kwargs,
    ):
        username = config.api.username
        password = config.api.password

        if not app_store.cookies.get("sessionid", None):
            app_store.cookies["sessionid"] = await login(
                config, session, username, password
            )

        return await func(app_store, config, session, *args, **kwargs)

    return inner


def with_retry(func: Callable, retries=5):
    """Wrapper for functions that call the CRCON API to retry failed API calls"""

    @wraps(func)
    async def inner(app_store: AppStore, *args, **kwargs):
        result = None

        for num in range(1, retries + 1):
            try:
                result = await func(app_store, *args, **kwargs)
                return result
            except aiohttp.ClientResponseError:
                app_store.logger.error(
                    "HTTP error when making API call to CRCON attempt"
                )
            except IndexError:
                app_store.logger.error(
                    "Received an invalid response from your CRCON Server"
                )
            except ValueError:
                # logged in #get_api_result
                pass
            app_store.logger.error(f"Retrying attempt {num}/{retries}")

    return inner


@with_retry
@with_login
async def get_api_result(
    app_store: AppStore,
    config: Config,
    session: aiohttp.ClientSession,
    endpoint: str,
    api_prefix: str | None = None,
    base_url: str | None = None,
) -> Any:
    """Call the CRCON API endpoint and return the unparsed result"""
    if base_url is None:
        base_url = config.api.base_server_url

    if api_prefix is None:
        api_prefix = constants.API_PREFIX

    response = await session.post(
        url=base_url + api_prefix + endpoint, cookies=app_store.cookies
    )

    if response.status != 200:
        app_store.logger.error(
            f"HTTP {response.status} for {endpoint=} for {app_store.server_identifier} {response.content=} {await response.text()=}"
        )
        response.raise_for_status()

    json = await response.json()
    result = json["result"]

    if result is None:
        app_store.logger.error(
            f"Received a None response from {endpoint} {await response.text()=}"
        )
        raise ValueError(
            f"Received a None response from {endpoint} {await response.text()=}"
        )

    return result


def parse_gamestate(app_store: AppStore, result: dict[str, Any]) -> GameState:
    """Parse and validate the result of /api/get_gamestate"""
    time_remaining_pattern = re.compile(r"(\d{1}):(\d{2}):(\d{2})")
    matched = re.match(time_remaining_pattern, result["raw_time_remaining"])
    if not matched:
        raise ValueError("Received an invalid response from your CRCON Server")
    hours, minutes, seconds = matched.groups()

    result["time_remaining"] = timedelta(
        hours=int(hours), minutes=int(minutes), seconds=int(seconds)
    )

    try:
        result["current_map"] = Map(raw_name=result["current_map"])
    except ValueError:
        app_store.logger.error(
            f"Invalid map name received current_map={result['current_map']}"
        )
        raise
    try:
        result["next_map"] = Map(raw_name=result["next_map"])
    except ValueError:
        app_store.logger.error(
            f"Invalid map name received next_map={result['next_map']}"
        )
        raise

    return GameState(**result)


def parse_slots(result: str) -> Slots:
    """Parse and validate the result of /api/get_slots"""
    player_count, max_players = result.split("/")
    return Slots(player_count=int(player_count), max_players=int(max_players))


def parse_map_rotation(result: list[str]) -> list[Map]:
    """Parse and validate the result of /api/get_map_rotation"""
    return [Map(raw_name=map_name) for map_name in result]


def get_map_picture_url(
    config: Config, map: Map, map_prefix=constants.MAP_PICTURES
) -> URL | None:
    """Build and validate a URL to the CRCON map image"""
    if map.raw_name == constants.BETWEEN_MATCHES_MAP_NAME:
        return None

    base_map_name, _ = map.raw_name.split("_", maxsplit=1)
    url = (
        config.api.base_server_url
        + map_prefix
        + constants.MAP_TO_PICTURE[base_map_name]
    )

    # This is valid even though pylance complains about it
    return URL(url=url)  # type: ignore


def parse_server_name(result: dict[str, Any]) -> ServerName:
    """Parse and validate the server name/short name from /api/get_status"""
    return ServerName(name=result["name"], short_name=result["short_name"])


def parse_vip_slots_num(result: str):
    """Parse and validate the number of reserved VIP slots from /api/get_vip_slots_num"""
    return int(result)


def parse_vips_count(result: str):
    """Parse and validate the number of VIPs on the server from /api/get_vip_slots_num"""
    return int(result)


def guess_current_map_rotation_positions(
    rotation: list[Map], current_map: Map, next_map: Map
) -> list[int]:
    """Estimate the index(es) of the current map in the rotation based off current/next map"""
    # As of U13 a map can be in a rotation more than once, but the index isn't
    # provided by RCON so we have to try to guess where we are in the rotation

    # TODO: what about single map rotations
    # TODO: use previous map to better estimate

    # Between rounds
    if current_map.raw_name == constants.BETWEEN_MATCHES_MAP_NAME:
        return []

    raw_names = [map.raw_name for map in rotation]

    # the current map is only in once then we know exactly where we are
    if raw_names.count(current_map.raw_name) == 1:
        return [raw_names.index(current_map.raw_name)]

    # the current map is in more than once, we must estimate
    # if the next map is in only once then we know exactly where we are
    current_map_idxs = []
    for idx in [idx for idx, name in enumerate(raw_names) if name == next_map.raw_name]:

        # if raw_names.count(next_map.raw_name) == 1:
        # next_map_idx = raw_names.index(next_map.raw_name)
        # current_map_idx = None

        # have to account for wrapping from the end to the start
        # current map is the end of the rotation
        if idx == 0:
            current_map_idx = len(raw_names) - 1
        # Somewhere besides the end of the rotation
        else:
            current_map_idx = idx - 1

        current_map_idxs.append(current_map_idx)
        # return [current_map_idx]

    return current_map_idxs

    # the current map is in more than once
    # and the next map is in multiple times so we can't determine where we are
    # return [idx for idx, name in enumerate(raw_names) if name == current_map.raw_name]


def guess_next_map_rotation_positions(
    current_map_positions: list[int], rotation: list[Map]
) -> list[int]:
    """Estimate the index(es) of the next map in the rotation based off current/next map"""
    rotation_length = len(rotation)

    positions: list[int] = []
    for position in current_map_positions:
        # handle wrapping back to the start of the rotation
        if position == rotation_length - 1:
            positions.append(0)
        # otherwise the next map is immediately after the current map
        else:
            positions.append(position + 1)

    return positions


ENDPOINTS_TO_PARSERS = {
    "get_gamestate": parse_gamestate,
    "get_vip_slots_num": parse_vip_slots_num,
    "get_vips_count": parse_vips_count,
    "get_status": parse_server_name,
    "get_slots": parse_slots,
}

OPTIONS_TO_ENDPOINTS = {
    "reserved_vip_slots": "get_vip_slots_num",
    "current_vips": "get_vips_count",
}


async def build_header(
    app_store: AppStore, config: Config, session: aiohttp.ClientSession
) -> tuple[str | None, discord.Embed | None]:
    """Build up the Discord.Embed for the header message"""

    # TODO: Add map vote info

    header_embed = discord.Embed()

    result = await get_api_result(app_store, config, session, endpoint="get_status")
    server_name = parse_server_name(result)

    match config.display.header.server_name:
        case "name":
            header_embed.title = server_name.name
        case "short_name":
            header_embed.title = server_name.short_name

    if url := config.display.header.quick_connect_url:
        header_embed.add_field(name="Quick Connect", value=url, inline=False)

    if url := config.display.header.battlemetrics_url:
        header_embed.add_field(name="BattleMetrics Page", value=url, inline=False)

    for option in config.display.header.embeds:
        endpoint = OPTIONS_TO_ENDPOINTS[option.value]
        result = await get_api_result(app_store, config, session, endpoint=endpoint)
        parser = ENDPOINTS_TO_PARSERS[endpoint]
        value = parser(result)
        header_embed.add_field(name=option.name, value=value, inline=option.inline)

    if config.display.header.display_last_refreshed:
        header_embed.set_footer(text=config.display.header.last_refresh_text)
        header_embed.timestamp = datetime.now()

    return None, header_embed


async def build_gamestate(
    app_store: AppStore,
    config: Config,
    session: aiohttp.ClientSession,
    endpoint: str = "get_gamestate",
) -> tuple[str | None, discord.Embed | None]:
    """Build up the Discord.Embed for the gamestate message"""
    gamestate_embed = discord.Embed()

    result = await get_api_result(app_store, config, session, endpoint=endpoint)
    gamestate = parse_gamestate(app_store, result)

    if config.display.gamestate.image:
        url = get_map_picture_url(config, gamestate["current_map"])

        if url:
            gamestate_embed.set_image(url=url.url)

    for option in config.display.gamestate.embeds:
        if option.value == "slots":
            result = await get_api_result(
                app_store, config, session, endpoint="get_slots"
            )
            slots = parse_slots(result)
            value = f"{slots.player_count}/{slots.max_players}"
        elif option.value == constants.EMPTY_EMBED:
            value = option.value
        elif option.value == "score":
            if (
                config.display.gamestate.score_format_ger_us
                and gamestate["current_map"].raw_name in constants.US_MAPS
            ):
                format_str = config.display.gamestate.score_format_ger_us
            elif (
                config.display.gamestate.score_format_ger_rus
                and gamestate["current_map"].raw_name in constants.RUSSIAN_MAPS
            ):
                format_str = config.display.gamestate.score_format_ger_rus
            else:
                format_str = config.display.gamestate.score_format

            value = format_str.format(
                gamestate["allied_score"], gamestate["axis_score"]
            )
        elif option.value in ("current_map", "next_map"):
            value = gamestate[option.value].name
        else:
            value = gamestate[option.value]

        gamestate_embed.add_field(name=option.name, value=value, inline=option.inline)

    if config.display.gamestate.display_last_refreshed:
        gamestate_embed.set_footer(text=config.display.gamestate.last_refresh_text)
        gamestate_embed.timestamp = datetime.now()

    return None, gamestate_embed


async def build_map_rotation_color(
    app_store: AppStore,
    config: Config,
    session: aiohttp.ClientSession,
    endpoint: str = "get_map_rotation",
) -> tuple[str | None, discord.Embed | None]:
    """Build up the content str for the map rotation color message"""
    result = await get_api_result(app_store, config, session, endpoint=endpoint)
    map_rotation = parse_map_rotation(result)

    gamestate_result = await get_api_result(
        app_store, config, session, endpoint="get_gamestate"
    )

    gamestate = parse_gamestate(app_store, gamestate_result)
    current_map_positions = guess_current_map_rotation_positions(
        map_rotation, gamestate["current_map"], gamestate["next_map"]
    )

    next_map_positions = guess_next_map_rotation_positions(
        current_map_positions, map_rotation
    )

    app_store.logger.debug(f"current map positions color {current_map_positions=}")
    app_store.logger.debug(f"next map positions color {next_map_positions}")

    content: list[str] = []

    if config.display.map_rotation.color.display_title:
        content.append(config.display.map_rotation.color.title)

    start_block = "```"
    end_block = "```"
    current_map_color = constants.COLOR_TO_CODE_BLOCK[
        config.display.map_rotation.color.current_map_color
    ]
    next_map_color = constants.COLOR_TO_CODE_BLOCK[
        config.display.map_rotation.color.next_map_color
    ]
    other_map_color = constants.COLOR_TO_CODE_BLOCK[
        config.display.map_rotation.color.other_map_color
    ]

    for idx, map in enumerate(map_rotation):
        if idx in current_map_positions:
            style = current_map_color
        elif idx in next_map_positions:
            style = next_map_color
        # other map color
        else:
            style = other_map_color
        line = start_block + style + "\n" + map.name + "\n" + end_block
        content.append(line)

    if config.display.map_rotation.color.display_legend:
        content.append(config.display.map_rotation.color.legend_title)
        current, next, other = config.display.map_rotation.color.legend

        content.append(start_block + current_map_color + "\n" + current + end_block)
        content.append(start_block + next_map_color + "\n" + next + end_block)
        content.append(start_block + other_map_color + "\n" + other + end_block)

    if config.display.map_rotation.color.display_last_refreshed:
        content.append(
            config.display.map_rotation.color.last_refresh_text.format(
                int(datetime.now().timestamp())
            )
        )

    return "".join(content), None


async def build_map_rotation_embed(
    app_store: AppStore,
    config: Config,
    session: aiohttp.ClientSession,
    endpoint: str = "get_map_rotation",
) -> tuple[str | None, discord.Embed | None]:
    """Build up the Discord.Embed for the map rotation embed message"""
    result = await get_api_result(app_store, config, session, endpoint=endpoint)
    map_rotation = parse_map_rotation(result)

    gamestate_result = await get_api_result(
        app_store, config, session, endpoint="get_gamestate"
    )
    gamestate = parse_gamestate(app_store, gamestate_result)

    current_map_positions = guess_current_map_rotation_positions(
        map_rotation, gamestate["current_map"], gamestate["next_map"]
    )
    next_map_positions = guess_next_map_rotation_positions(
        current_map_positions, map_rotation
    )

    app_store.logger.debug(f"current map positions embed {current_map_positions=}")
    app_store.logger.debug(f"next map positions embed {next_map_positions}")

    map_rotation_embed = discord.Embed()

    description = []
    for idx, map in enumerate(map_rotation):
        if idx in current_map_positions:
            description.append(
                config.display.map_rotation.embed.current_map.format(map.name, idx + 1)
            )
        elif idx in next_map_positions:
            description.append(
                config.display.map_rotation.embed.next_map.format(map.name, idx + 1)
            )
        # other map
        else:
            description.append(
                config.display.map_rotation.embed.other_map.format(map.name, idx + 1)
            )

    if config.display.map_rotation.embed.display_legend:
        description.append(config.display.map_rotation.embed.legend)

    map_rotation_embed.add_field(
        name=config.display.map_rotation.embed.title, value="\n".join(description)
    )

    if config.display.map_rotation.embed.display_last_refreshed:
        map_rotation_embed.set_footer(
            text=config.display.map_rotation.embed.last_refresh_text
        )
        map_rotation_embed.timestamp = datetime.now()

    return None, map_rotation_embed


async def get_message_ids(app_store: AppStore, config: Config) -> tomlkit.TOMLDocument:
    if not (message_ids := app_store.message_ids):
        try:
            message_ids = await load_message_ids_from_disk(app_store)
        except FileNotFoundError:
            app_store.logger.warning(
                f"{app_store.server_identifier}.toml config file not found."
            )

        message_ids = validate_message_ids_format(app_store, message_ids)
        app_store.message_ids = message_ids
    return message_ids


async def load_message_ids_from_disk(
    app_store: AppStore,
    path: str | None = None,
    filename: str | None = None,
) -> tomlkit.TOMLDocument:
    if not path:
        path = constants.MESSAGES_DIR

    if not filename:
        filename = app_store.server_identifier + ".toml"

    file = Path(path, filename)
    app_store.logger.info(f"Loading message IDs from {file}")
    async with aiofiles.open(file, mode="r") as fp:
        contents = await fp.read()

    message_ids = tomlkit.loads(contents)
    app_store.logger.info(f"Loaded message IDs={message_ids}")
    return message_ids


def validate_message_ids_format(
    app_store: AppStore,
    message_ids: tomlkit.TOMLDocument | None,
    format: MessageIDFormat = constants.MESSAGE_ID_FORMAT,
    default_value: int = constants.NONE_MESSAGE_ID,
) -> tomlkit.TOMLDocument:
    """Validate the structure of saved message IDs and create defaults for missing keys"""
    # TODO include file name for better error messages

    if message_ids is None:
        app_store.logger.warning(
            f"{app_store.server_identifier} No message IDs passed, creating a new TOML document"
        )
        message_ids = tomlkit.document()

    table_name = format["table_name"]
    table = message_ids.get(table_name)
    if table is None:
        app_store.logger.warning(
            f"{app_store.server_identifier} {table_name=} missing, creating a new table"
        )
        table = tomlkit.table()
        message_ids.add(table_name, table)

    for field in format["fields"]:
        if field not in table:
            app_store.logger.warning(
                f"{app_store.server_identifier} Creating missing {field=} with {default_value=}"
            )
            message_ids[table_name].add(field, default_value)
        if field not in constants.MESSAGE_ID_FORMAT["fields"]:
            app_store.logger.error(
                f"{app_store.server_identifier} Unknown field {field} in saved message IDs"
            )

    return message_ids


async def handle_webhook(
    app_store: AppStore,
    key: str,
    webhook: discord.Webhook,
    message_id: int | None = None,
    embed: discord.Embed | None = None,
    content: str | None = None,
) -> int | None:
    """Send the content/embed for a given webhook and return the message ID"""
    if content is None:
        content = ""

    # TODO: handle rate limiting
    # TODO: abstract better so we can try/catch exceptions in one place

    if message_id:
        log_message = f"Editing {key} message ID={message_id}"
        func = partial(
            webhook.edit_message, message_id=message_id, content=content, embed=embed
        )
    else:
        log_message = f"Creating new {key} webhook message"
        if embed:
            func = partial(webhook.send, content=content, embed=embed, wait=True)
        else:
            func = partial(webhook.send, content=content, wait=True)

    try:
        app_store.logger.info(log_message)
        message = await func()
        message_id = message.id
    except discord.errors.NotFound:
        app_store.logger.warning(
            f"Tried to edit non-existent {key} message ID={message_id}"
        )
        message_id = None
    except discord.errors.RateLimited:
        app_store.logger.warning(f"This message was rate limited by Discord")

    return message_id


async def update_hook_for_section(
    app_store: AppStore,
    config: Config,
    webhook: discord.Webhook,
    session: aiohttp.ClientSession,
    table_name: str,
    key: str,
    message_id: int | None,
    content_embed_creator_func: Callable,
) -> None:
    """Infinitely update/sleep between refreshes for a specific section"""
    while True:
        start_time = time.perf_counter_ns()
        content, embed = await content_embed_creator_func(app_store, config, session)
        message_id = await handle_webhook(
            app_store, key, webhook, message_id, content=content, embed=embed
        )
        if message_id:
            await save_message_id(
                app_store, table_name=table_name, key=key, message_id=message_id
            )

        await save_message_ids_to_disk(app_store, config)
        end_time = time.perf_counter_ns()
        elapsed_time_ns = end_time - start_time
        factor = 1_000_000_000
        refresh_delay: int = config.discord.time_between_refreshes
        refresh_delay_ns = refresh_delay * factor
        time_to_sleep = round((refresh_delay_ns - elapsed_time_ns) / factor, ndigits=0)
        app_store.logger.info(
            f"Sleeping {app_store.server_identifier}.{key} for {time_to_sleep} seconds"
        )
        await asyncio.sleep(time_to_sleep)


def bootstrap(logger: logging.Logger, directories=constants.MANDATORY_DIRECTORIES):
    for directory in directories:
        try:
            Path(directory).mkdir(exist_ok=True)
        except FileNotFoundError:
            logger.error(f"Unable to create {directory}")
            sys.exit(1)


async def main():
    """Load all the config files create asyncio tasks"""
    formatter = logging.Formatter("%(asctime)s:%(levelname)s:%(message)s")

    root_logger = logging.getLogger(constants.ROOT_LOGGER_NAME)
    root_logger.setLevel(os.getenv("LOGGING_LEVEL", logging.INFO))
    file_handler = RotatingFileHandler(
        filename=Path(
            constants.LOG_DIR, constants.ROOT_LOGGER_NAME + constants.LOG_EXTENSION
        ),
        maxBytes=constants.LOG_SIZE_BYTES,
        backupCount=constants.LOG_COUNT,
    )
    console_handler = logging.StreamHandler(stream=sys.stderr)
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    bootstrap(root_logger)
    servers: list[tuple[AppStore, Config]] = []
    for file_path in Path(constants.CONFIG_DIR).iterdir():
        root_logger.info(f"Reading config file for {file_path}")
        logger = logging.getLogger(file_path.stem)
        logger.setLevel(os.getenv("LOGGING_LEVEL", logging.INFO))
        handler = RotatingFileHandler(
            filename=Path(constants.LOG_DIR, file_path.stem + constants.LOG_EXTENSION),
            maxBytes=constants.LOG_SIZE_BYTES,
            backupCount=constants.LOG_COUNT,
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        config = load_config(file_path)
        app_store = AppStore(server_identifier=file_path.stem, logger=logger)
        servers.append((app_store, config))

    if not servers:
        root_logger.error(
            f"No config files found add one or more to {constants.LOG_DIR} "
        )

    async with aiohttp.ClientSession() as session:
        server_sections = []
        for app_store, config in servers:
            webhook = discord.Webhook.from_url(
                config.discord.webhook_url, session=session
            )
            message_ids = await get_message_ids(app_store, config)
            table_name = constants.MESSAGE_ID_FORMAT["table_name"]

            sections: list[
                tuple[
                    AppStore,
                    Config,
                    discord.Webhook,
                    aiohttp.ClientSession,
                    str,
                    str,
                    int,
                    Callable,
                ]
            ] = []

            callables = (
                build_header,
                build_gamestate,
                build_map_rotation_color,
                build_map_rotation_embed,
            )
            keys = ("header", "gamestate", "map_rotation_color", "map_rotation_embed")
            enableds = (
                config.display.header.enabled,
                config.display.gamestate.enabled,
                config.display.map_rotation.color.enabled,
                config.display.map_rotation.embed.enabled,
            )

            for callable, key, enabled in zip(callables, keys, enableds):
                if enabled:
                    sections.append(
                        (
                            app_store,
                            config,
                            webhook,
                            session,
                            table_name,
                            key,
                            message_ids[table_name][key],
                            callable,
                        )
                    )

            server_sections.append(sections)

        async with asyncio.taskgroups.TaskGroup() as tg:
            for server_section in server_sections:
                for section in server_section:
                    (
                        app_store,
                        config,
                        webhook,
                        session,
                        table_name,
                        key,
                        message_id,
                        func,
                    ) = section
                    root_logger.info(
                        f"Starting {app_store.server_identifier}:{key} check log files for further output"
                    )
                    tg.create_task(
                        update_hook_for_section(
                            app_store,
                            config,
                            webhook,
                            session,
                            table_name,
                            key,
                            message_id,
                            func,
                        )
                    )


if __name__ == "__main__":
    asyncio.run(main())

# TODO: add Dockerfile for deployment
# TODO: Update README
# TODO: /api/get_gamestate 500 on map change?

# Future
# TODO: add score sections
