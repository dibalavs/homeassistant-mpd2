"""Support to interact with a Music Player Daemon."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from enum import StrEnum
import hashlib
import logging
import os
from pathlib import Path
from socket import gaierror
from typing import Any

import mpd
from mpd.asyncio import MPDClient
import voluptuous as vol

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    PLATFORM_SCHEMA as MEDIA_PLAYER_PLATFORM_SCHEMA,
    BrowseMedia,
    MediaClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    RepeatMode,
    async_process_play_media_url,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PASSWORD, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import Throttle, dt as dt_util

from .const import DOMAIN, LOGGER

EVENT_NAME = f"{DOMAIN}_event"
EVENT_TYPE_STATE_CHANGED = "state_changed" # {"prev" : <MediaPlayerState>, "curr" : <MediaPlayerState>}
EVENT_TYPE_CURRNENT_SONG_CHANGED = "current_song_changed" # {"prev" : <str>, "curr" : <str>}
EVENT_TYPE_PLAYLIST_CHANGED = "playlist_changed" # No playload
EVENT_TYPE_LIST_PLAYLISTS_CHANGED = "list_playlists_changed" # {"prev" : <list[str]>, "curr" : <list[str]>}
EVENT_TYPE_VOLUME_CHANGED = "volume_changed" # {"prev" : <float>, "curr" : <float>}

DEFAULT_NAME = "MPD2"
DEFAULT_PORT = 6600

PLAYLIST_UPDATE_INTERVAL = timedelta(seconds=120)

SUPPORT_MPD = (
    MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.PREVIOUS_TRACK
    | MediaPlayerEntityFeature.NEXT_TRACK
    | MediaPlayerEntityFeature.PLAY_MEDIA
    | MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.CLEAR_PLAYLIST
    | MediaPlayerEntityFeature.REPEAT_SET
    | MediaPlayerEntityFeature.SHUFFLE_SET
    | MediaPlayerEntityFeature.SEEK
    | MediaPlayerEntityFeature.STOP
    | MediaPlayerEntityFeature.TURN_OFF
    | MediaPlayerEntityFeature.TURN_ON
    | MediaPlayerEntityFeature.BROWSE_MEDIA
)

PLATFORM_SCHEMA = MEDIA_PLAYER_PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
    }
)

class MpdMediaType(StrEnum):
    FILES = "Files"
    ALBUM = "Album"
    ARTIST = "Artist"
    GENRE = "Genre"
    TITLE = "Title"
    PLAYLISTS = "Playlists"
    CURRENT_PLAYLIST = "Playlist"
    TAGS = "Tags"

_MEDIA_TYPE_TO_TITLE = {
    MpdMediaType.ALBUM  : "By Album",
    MpdMediaType.ARTIST : "By Artist",
    MpdMediaType.GENRE  : "By Genre",
    MpdMediaType.TITLE  : "By Title",
    MpdMediaType.FILES  : "Files",
    MpdMediaType.PLAYLISTS : "Playlists",
    MpdMediaType.TAGS   : "Tags",
    MpdMediaType.CURRENT_PLAYLIST : "Current Playlist",
    None : "Media Player Daemon"
}

_MEDIA_TYPE_TO_TAG = {
    MpdMediaType.ALBUM  : "AlbumSort",
    MpdMediaType.ARTIST : "ArtistSort",
    MpdMediaType.GENRE  : "Genre",
    MpdMediaType.TITLE  : "Title" # NOTE: "TitleSort" is not working
}

_SEP = "|||"

async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    mpd = MpdDevice(
        hass,
        config[CONF_NAME],
        config[CONF_HOST],
        config[CONF_PORT],
        config.get(CONF_PASSWORD))
    async_add_entities([mpd])


class MpdDevice(MediaPlayerEntity):
    """Representation of a MPD server."""

    _attr_media_content_type = MediaType.MUSIC
    _attr_has_entity_name = True
    _attr_name = None

    def __init__(
        self, hass: HomeAssistant, name: str, server: str, port: int, password: str | None
    ) -> None:
        """Initialize the MPD device."""
        self.hass = hass
        self.server = server
        self.port = port
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, name)},
            entry_type=DeviceEntryType.SERVICE,
        )
        self.password = password

        self._attr_name = name
        self._status: dict[str, Any] = {}
        self._currentsong = None
        self._current_playlist: str | None = None
        self._playlist_songs: list[str] = []
        self._muted_volume = None
        self._media_image_hash = None
        # Track if the song changed so image doesn't have to be loaded every update.
        self._media_image_file = None

        # set up MPD client
        self._client = MPDClient()
        self._client.timeout = 30
        self._client.idletimeout = 10
        self._client_lock = asyncio.Lock()

    def _fire_if_state_changed(self, prev:MediaPlayerState, curr:MediaPlayerState):
        if prev != curr:
            data = {
                "entity_id": self.entity_id,
                "entity_name": self._attr_name,
                "type": EVENT_TYPE_STATE_CHANGED,
                "prev": prev,
                "curr": curr
            }
            self.hass.bus.async_fire(EVENT_NAME, data)

    def _fire_if_current_song_changed(self, prev:str, curr:str):
        if prev != curr:
            data = {
                "entity_id": self.entity_id,
                "entity_name": self._attr_name,
                "type": EVENT_TYPE_CURRNENT_SONG_CHANGED,
                "prev": prev,
                "curr": curr
            }
            self.hass.bus.async_fire(EVENT_NAME, data)

    def _fire_if_list_playlists_changed(self, prev:list, curr:list):
        if prev != curr:
            data = {
                "entity_id": self.entity_id,
                "entity_name": self._attr_name,
                "type": EVENT_TYPE_LIST_PLAYLISTS_CHANGED,
                "prev": prev,
                "curr": curr
            }
            self.hass.bus.async_fire(EVENT_NAME, data)

    def _fire_playlist_changed(self):
        data = {
            "entity_id": self.entity_id,
            "entity_name": self._attr_name,
            "type": EVENT_TYPE_PLAYLIST_CHANGED,
        }
        self.hass.bus.async_fire(EVENT_NAME, data)

    def _fire_if_volume_changed(self, prev:int, curr:int):
        if prev != curr:
            data = {
                "entity_id": self.entity_id,
                "entity_name": self._attr_name,
                "type": EVENT_TYPE_VOLUME_CHANGED,
                "prev": int(prev) / 100,
                "curr": int(curr) / 100
            }
            self.hass.bus.async_fire(EVENT_NAME, data)

    # Instead of relying on python-mpd2 to maintain a (persistent) connection to
    # MPD, the below explicitly sets up a *non*-persistent connection. This is
    # done to workaround the issue as described in:
    #   <https://github.com/Mic92/python-mpd2/issues/31>
    @asynccontextmanager
    async def connection(self):
        """Handle MPD connect and disconnect."""
        async with self._client_lock:
            try:
                # MPDClient.connect() doesn't always respect its timeout. To
                # prevent a deadlock, enforce an additional (slightly longer)
                # timeout on the coroutine itself.
                try:
                    async with asyncio.timeout(self._client.timeout + 5):
                        await self._client.connect(self.server, self.port)
                except TimeoutError as error:
                    # TimeoutError has no message (which hinders logging further
                    # down the line), so provide one.
                    raise TimeoutError("Connection attempt timed out") from error
                if self.password is not None:
                    await self._client.password(self.password)
                self._attr_available = True
                yield
            except (
                TimeoutError,
                gaierror,
                mpd.ConnectionError,
                OSError,
            ) as error:
                # Log a warning during startup or when previously connected; for
                # subsequent errors a debug message is sufficient.
                log_level = logging.DEBUG
                if self._attr_available is not False:
                    log_level = logging.WARNING
                LOGGER.log(
                    log_level, "Error connecting to '%s': %s", self.server, error
                )
                self._attr_available = False
                self._status = {}
                # Also yield on failure. Handling mpd.ConnectionErrors caused by
                # attempting to control a disconnected client is the
                # responsibility of the caller.
                yield
            finally:
                with suppress(mpd.ConnectionError):
                    self._client.disconnect()

    async def async_update(self) -> None:
        """Get the latest data from MPD and update the state."""
        async with self.connection():
            try:
                status = await self._client.status()
                self._fire_if_state_changed(self._to_state(self._status), self._to_state(status))
                self._fire_if_volume_changed((self._status or {}).get("volume", 0), (status or {}).get("volume", 0))
                self._status = status

                song = await self._client.currentsong()
                self._fire_if_current_song_changed((self._currentsong or {}).get("file"), (song or {}).get("file"))
                self._currentsong = song

                await self._async_update_media_image_hash()

                if (position := self._status.get("elapsed")) is None:
                    position = self._status.get("time")

                    if isinstance(position, str) and ":" in position:
                        position = position.split(":")[0]

                if position is not None and self._attr_media_position != position:
                    self._attr_media_position_updated_at = dt_util.utcnow()
                    self._attr_media_position = int(float(position))

                await self._update_playlists()
            except (mpd.ConnectionError, ValueError) as error:
                LOGGER.debug("Error updating status: %s", error)

    def _to_state(self, state) -> MediaPlayerState:
        """Return the media state."""
        if not state:
            return MediaPlayerState.OFF

        state_val = state.get("state")

        if  state_val == "play":
            return MediaPlayerState.PLAYING
        if state_val == "pause":
            return MediaPlayerState.PAUSED
        if state_val == "stop":
            return MediaPlayerState.OFF

        return MediaPlayerState.OFF

    @property
    def state(self) -> MediaPlayerState:
        """Return the media state."""
        return self._to_state(self._status)

    @property
    def media_content_id(self):
        """Return the content ID of current playing media."""
        return self._currentsong.get("file")

    @property
    def media_duration(self):
        """Return the duration of current playing media in seconds."""
        if currentsong_time := self._currentsong.get("time"):
            return currentsong_time

        time_from_status = self._status.get("time")
        if isinstance(time_from_status, str) and ":" in time_from_status:
            return time_from_status.split(":")[1]

        return None

    @property
    def media_title(self):
        """Return the title of current playing media."""
        name = self._currentsong.get("name", None)
        title = self._currentsong.get("title", None)
        file_name = self._currentsong.get("file", None)

        if name is None and title is None:
            if file_name is None:
                return "None"
            return os.path.basename(file_name)
        if name is None:
            return title
        if title is None:
            return name

        return f"{name}: {title}"

    @property
    def media_artist(self):
        """Return the artist of current playing media (Music track only)."""
        artists = self._currentsong.get("artist")
        if isinstance(artists, list):
            return ", ".join(artists)
        return artists

    @property
    def media_album_name(self):
        """Return the album of current playing media (Music track only)."""
        return self._currentsong.get("album")

    @property
    def media_image_hash(self):
        """Hash value for media image."""
        return self._media_image_hash

    async def async_get_media_image(self) -> tuple[bytes | None, str | None]:
        """Fetch media image of current playing track."""
        async with self.connection():
            if self._currentsong is None or not (file := self._currentsong.get("file")):
                return None, None

            with suppress(mpd.ConnectionError):
                response = await self._async_get_file_image_response(file)
            if response is None:
                return None, None

            image = bytes(response["binary"])
            mime = response.get(
                "type", "image/png"
            )  # readpicture has type, albumart does not
            return (image, mime)

    async def _async_update_media_image_hash(self):
        """Update the hash value for the media image."""
        if self._currentsong is None:
            return

        file = self._currentsong.get("file")

        if file == self._media_image_file:
            return

        if (
            file is not None
            and (response := await self._async_get_file_image_response(file))
            is not None
        ):
            self._media_image_hash = hashlib.sha256(
                bytes(response["binary"])
            ).hexdigest()[:16]
        else:
            # If there is no image, this hash has to be None, else the media player component
            # assumes there is an image and returns an error trying to load it and the
            # frontend media control card breaks.
            self._media_image_hash = None

        self._media_image_file = file

    async def _async_get_file_image_response(self, file):
        # not all MPD implementations and versions support the `albumart` and
        # `fetchpicture` commands.
        commands = []
        with suppress(mpd.ConnectionError):
            commands = list(await self._client.commands())
        can_albumart = "albumart" in commands
        can_readpicture = "readpicture" in commands

        response = None

        # read artwork embedded into the media file
        if can_readpicture:
            try:
                with suppress(mpd.ConnectionError):
                    response = await self._client.readpicture(file)
            except mpd.CommandError as error:
                if error.errno is not mpd.FailureResponseCode.NO_EXIST:
                    LOGGER.warning(
                        "Retrieving artwork through `readpicture` command failed: %s",
                        error,
                    )

        # read artwork contained in the media directory (cover.{jpg,png,tiff,bmp}) if none is embedded
        if can_albumart and not response:
            try:
                with suppress(mpd.ConnectionError):
                    response = await self._client.albumart(file)
            except mpd.CommandError as error:
                if error.errno is not mpd.FailureResponseCode.NO_EXIST:
                    LOGGER.warning(
                        "Retrieving artwork through `albumart` command failed: %s",
                        error,
                    )

        # response can be an empty object if there is no image
        if not response:
            return None

        return response

    @property
    def volume_level(self):
        """Return the volume level."""
        if "volume" in self._status:
            return int(self._status["volume"]) / 100
        return None

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        """Flag media player features that are supported."""
        if not self._status:
            return MediaPlayerEntityFeature(0)

        supported = SUPPORT_MPD
        if "volume" in self._status:
            supported |= (
                MediaPlayerEntityFeature.VOLUME_SET
                | MediaPlayerEntityFeature.VOLUME_STEP
                | MediaPlayerEntityFeature.VOLUME_MUTE
            )
        if self._attr_source_list is not None:
            supported |= MediaPlayerEntityFeature.SELECT_SOURCE

        return supported

    @property
    def source(self):
        """Name of the current input source."""
        return self._current_playlist

    async def async_select_source(self, source: str) -> None:
        """Choose a different available playlist and play it."""
        await self.async_play_media(MediaType.PLAYLIST, source)

    @Throttle(PLAYLIST_UPDATE_INTERVAL)
    async def _update_playlists(self, **kwargs: Any) -> None:
        """Update available MPD playlists."""
        try:
            prev_source_list = self._attr_source_list
            prev_playlist_songs = self._playlist_songs

            self._attr_source_list = []
            self._playlist_songs = []
            with suppress(mpd.ConnectionError):
                for playlist_data in await self._client.listplaylists():
                    self._attr_source_list.append(playlist_data["playlist"])
            self._fire_if_list_playlists_changed(prev_source_list, self._attr_source_list)

            for song in await self._client.playlist():
                self._playlist_songs.append(song)

            if prev_playlist_songs != self._playlist_songs:
                    self._fire_playlist_changed()
        except mpd.CommandError as error:
            self._attr_source_list = None
            LOGGER.warning("Playlists could not be updated: %s:", error)

    async def async_set_volume_level(self, volume: float) -> None:
        """Set volume of media player."""
        async with self.connection():
            prev_volume = self._status.get("volume")
            if prev_volume is not None:
                new_volume = int(volume * 100)
                await self._client.setvol(new_volume)
                self._status["volume"] = new_volume

    async def async_volume_up(self) -> None:
        """Service to send the MPD the command for volume up."""
        async with self.connection():
            prev_volume = self._status.get("volume")
            if prev_volume is not None and int(prev_volume) <= 95:
                    new_volume = int(prev_volume) + 5
                    self._client.setvol(new_volume)
                    self._status["volume"] = new_volume


    async def async_volume_down(self) -> None:
        """Service to send the MPD the command for volume down."""
        async with self.connection():
            prev_volume = self._status.get("volume")
            if prev_volume is not None and int(prev_volume) >= 5:
                    new_volume = int(prev_volume) - 5
                    self._client.setvol(new_volume)
                    self._status["volume"] = new_volume

    async def async_media_play(self) -> None:
        """Service to send the MPD the command for play/pause."""
        async with self.connection():
            if self._status.get("state") == "pause":
                await self._client.pause(0)
            else:
                await self._client.play()

    async def async_media_pause(self) -> None:
        """Service to send the MPD the command for play/pause."""
        async with self.connection():
            await self._client.pause(1)

    async def async_media_stop(self) -> None:
        """Service to send the MPD the command for stop."""
        async with self.connection():
            await self._client.stop()

    async def async_media_next_track(self) -> None:
        """Service to send the MPD the command for next track."""
        async with self.connection():
            await self._client.next()

    async def async_media_previous_track(self) -> None:
        """Service to send the MPD the command for previous track."""
        async with self.connection():
            await self._client.previous()

    async def async_mute_volume(self, mute: bool) -> None:
        """Mute. Emulated with set_volume_level."""
        if "volume" in self._status:
            if mute:
                self._muted_volume = self.volume_level
                await self.async_set_volume_level(0)
            elif self._muted_volume is not None:
                await self.async_set_volume_level(self._muted_volume)
            self._attr_is_volume_muted = mute

    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        """Send the media player the command for playing a playlist."""

        if media_id:
            media_id = media_id.split(_SEP)[0]

        async with self.connection():
            if media_source.is_media_source_id(media_id):
                media_type = MediaType.MUSIC
                play_item = await media_source.async_resolve_media(
                    self.hass, media_id, self.entity_id
                )
                media_id = async_process_play_media_url(self.hass, play_item.url)

            if media_type == MediaType.PLAYLIST:
                LOGGER.debug("Playing playlist: %s", media_id)
                if self._attr_source_list and media_id in self._attr_source_list:
                    self._current_playlist = media_id
                else:
                    self._current_playlist = None
                    LOGGER.warning("Unknown playlist name %s", media_id)
                await self._client.clear()
                await self._client.load(media_id)
                await self._client.play()
            else:
                await self._client.clear()
                self._current_playlist = None
                await self._client.add(media_id)
                await self._client.play()
            self._fire_playlist_changed()

    @property
    def repeat(self) -> RepeatMode:
        """Return current repeat mode."""
        if self._status.get("repeat") == "1":
            if self._status.get("single") == "1":
                return RepeatMode.ONE
            return RepeatMode.ALL
        return RepeatMode.OFF

    async def async_set_repeat(self, repeat: RepeatMode) -> None:
        """Set repeat mode."""
        async with self.connection():
            if repeat == RepeatMode.OFF:
                await self._client.repeat(0)
                await self._client.single(0)
            else:
                await self._client.repeat(1)
                if repeat == RepeatMode.ONE:
                    await self._client.single(1)
                else:
                    await self._client.single(0)

    @property
    def shuffle(self):
        """Boolean if shuffle is enabled."""
        return bool(int(self._status.get("random")))

    async def async_set_shuffle(self, shuffle: bool) -> None:
        """Enable/disable shuffle mode."""
        async with self.connection():
            await self._client.random(int(shuffle))

    async def async_turn_off(self) -> None:
        """Service to send the MPD the command to stop playing."""
        async with self.connection():
            await self._client.stop()

    async def async_turn_on(self) -> None:
        """Service to send the MPD the command to start playing."""
        async with self.connection():
            await self._client.play()
            await self._update_playlists(no_throttle=True)

    async def async_clear_playlist(self) -> None:
        """Clear players playlist."""
        async with self.connection():
            await self._client.clear()

    async def async_media_seek(self, position: float) -> None:
        """Send seek command."""
        async with self.connection():
            await self._client.seekcur(position)

    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Implement the websocket media browsing helper."""
        mt = media_content_type
        title = media_content_id or _MEDIA_TYPE_TO_TITLE[mt]

        async with self.connection():
            try:
                match mt:
                    case None: # Show all categories
                        children = [_to_browse_media(_MEDIA_TYPE_TO_TITLE[t], t, empty_content_id=True) for t in MpdMediaType]

                    case MpdMediaType.FILES: ## set title
                        children=[_to_browse_media(x, mt) for x in await self._client.lsinfo(media_content_id)]

                    case MpdMediaType.CURRENT_PLAYLIST:
                        children=[_to_browse_media(x, mt) for x in await self._client.playlistinfo()]

                    case  MpdMediaType.TAGS if not media_content_id: # Get all tags
                        children=[_to_browse_media(x, mt) for x in await self._client.tagtypes()]

                    case MpdMediaType.TAGS if _SEP not in media_content_id: # Get all categories by given tag
                        children=[_to_browse_media(x, mt, media_content_id) for x in await self._client.list(media_content_id)]

                    case MpdMediaType.ALBUM | MpdMediaType.GENRE | MpdMediaType.TITLE | MpdMediaType.ARTIST if not media_content_id:
                        tag = _MEDIA_TYPE_TO_TAG[mt]
                        children=[_to_browse_media(x, mt, tag) for x in await self._client.list(tag)]

                    case MpdMediaType.ALBUM | MpdMediaType.GENRE | MpdMediaType.TITLE | MpdMediaType.ARTIST | MpdMediaType.TAGS:
                        title, category = media_content_id.split(_SEP)
                        children=[_to_browse_media(x, mt, title) for x in await self._client.find(category, title)]

                    case _:
                        raise HomeAssistantError(f"Unknown media content type: {mt}")
                return _to_browse_media(title, mt, empty_content_id=True, children=children)
            except Exception as ex:
                if isinstance(ex, HomeAssistantError):
                    raise
                raise HomeAssistantError(f"Failed to fetch data: {str(ex)}") from ex

def _to_browse_media(info : dict | str, media_type:MpdMediaType, parent:str|None = None, empty_content_id = False, children = []) -> BrowseMedia:
    media_class = MediaClass.DIRECTORY
    content = info

    if isinstance(info, dict):
        if "directory" in info:
            media_class = MediaClass.DIRECTORY
            content = info["directory"]
        elif "file" in info:
            media_class = MediaClass.MUSIC
            content = info["file"]
        elif "playlist" in info:
            media_class = MediaClass.PLAYLIST
            content = info["playlist"]
        elif parent and parent.lower() in info:
            media_class = MediaClass.DIRECTORY
            content = info[parent.lower()]
        else:
            LOGGER.error(f"Unknown response from MPD: {info}")
            raise HomeAssistantError("Unknown response from MPD", info)

    if empty_content_id:
        content_id = ""
    else:
        content_id = content if not parent else f"{content}{_SEP}{parent}"

    return BrowseMedia(
        media_class=media_class,
        media_content_id=content_id,
        media_content_type=media_type,
        title=Path(content).name,
        can_play=media_class in [MediaClass.MUSIC, MediaClass.PLAYLIST],
        can_expand=media_class == MediaClass.DIRECTORY,
        children_media_class=None,
        children=children,
    )
