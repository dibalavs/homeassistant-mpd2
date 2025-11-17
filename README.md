# MPD2 Player Integration for Home Assistant

## Overview

This MPD2 implementation was originally based on the Home Assistant MPD integration from:
[https://github.com/home-assistant/core/tree/dev/homeassistant/components/mpd](https://github.com/home-assistant/core/tree/dev/homeassistant/components/mpd)

This enhanced integration includes several features not present in the original component.

## Installation

1. Navigate to your Home Assistant custom components directory:
    ```bash
    <homeassistant_config_directory>/custom_components/
    ```
2. Clone the repository:
    ```bash
    git clone https://github.com/dibalavs/homeassistant-mpd2.git mpd2
    ```
3. Restart Home Assistant

## Configuration

The auto-configuration with UI was removed. Use YAML configuration instead:
```
media_player:
  - platform: mpd2
    name: my_mpd2
    host: 127.0.0.1
    id:          # optional, unquie entity identifier
    port: 6600   # optional, default is 6600
    password: your_password  # optional
    volume: 0.45  # optional, float values 0..1. Default - keep volume from MPD
    shuffle: yes  # optional, boolean values "yes", "no"

    # WARNING: quotes are required! in opposite way, off will be interpreted as false
    repeat: "all" # optional, Possible values: "all", "one", "off"

    # WARNING: Use this option only if MPD does not support state_file feature.
    enable_volume_sync: "on" # optional. Do volume level synchronization from MPD service
```

## Enhanced Features

### Media Browser with MPD Database Support

Browse different media content types:

- `Files` - Get directories/files from MPD database
- `Album` - Get list of albums
- `Artist` - Get list of artists
- `Genre` - Get list of genres
- `Title` - Get list of titles
- `Playlist` - Get content of current playlist from MPD
- `Tags` - Get list of all tags from MPD

Example:

```
mpd2_songs_fill:
  sequence:
    - alias: "Fetch MPD current playlist"
      action: media_player.browse_media
      target:
        entity_id: media_player.my_mpd2
      data:
        media_content_type: "Playlist"
        media_content_id: ""
      response_variable: playlist
    - alias: "Fill songs input_select"
      action: input_select.set_options
      target:
        entity_id: input_select.mpd2_select_songs
      data:
        options: >
          [
            {%- for item in playlist['media_player.my_mpd2']['children'] -%}
             "{{ item['title'] }} ----- {{ item['media_content_id'] }}",
            {%- endfor -%}
          ]

```

### media_player.play_media supports `enqueue` parameter

Control how media is added to the playlist:

- `add` - Add media to current playlist without clearing.
- `play` - Play existing media or add new one without clearing playlist.
- `replace` - Replace entire playlist with new content.

Example:

```
- action: media_player.play_media
    target:
    entity_id: media_player.my_mpd2
    data:
    media_content_type: "music"
    media_content_id: "{{ states('input_select.mpd2_select_songs').split('-----') | last }}"
    enqueue: "play"

```

### Event System

The integration fires `mpd2_event` with different types for various state changes.

#### Event Types:

Additional you can check event type to handle condition.

- `state_changed` - When MPD player state changes

Possible states: `off`, `on`, `idle`, `playing`, `paused`, `standby`, `buffering`

Additional data payload from event:

```
data = {
    "entity_id": <entity id>,
    "entity_name": <entity name>,
    "type": "state_changed",
    "prev": <prev_state>,
    "curr": <curr_state>
}
```

- `current_song_changed` - When currently playing song changes.

Additional data payload from event:

```
data = {
    "entity_id": <entity id>,
    "entity_name": <entity name>,
    "type": "current_song_changed",
    "prev_media_id": <media_id of previous song>,
    "curr_media_id": <media_id of new song>,
    "prev_media_title": <title of previous song>,
    "curr_media_title": <title of new song>,
}
```

- `playlist_changed` - When current playlist is updated.

Additional data payload from event:
```
data = {
    "entity_id": <entity id>,
    "entity_name": <entity name>,
    "type": playlist_changed,
}

```

- `list_playlists_changed` - When list of saved playlists is modified.
```
data = {
    "entity_id": <entity id>,
    "entity_name": <entity name>,
    "type": playlist_changed,
    "prev": ["list", "of", "previous", "playlists"],
    "curr": ["list", "of", "current", "playlists"]
}
```

- `volume_changed` - When volume level changes

Additional data payload from event:
```
data = {
    "entity_id": <entity id>,
    "entity_name": <entity name>,
    "type": volume_changed,
    "prev": 0.75,  # float value 0..1
    "curr": 0.80   # float value 0..1
}
```

- `database_updated` - When database song's database was updated (timestamp changed)

Additional data payload from event:

```
data = {
    "entity_id": <entity id>,
    "entity_name": <entity name>,
    "type": database_updated,
    "prev": prev_timestamp,
    "curr": curr_timestamp
}
```

Example Automation:

```
alias: On current song was changed by MPD
triggers:
- trigger: event
    event_type: mpd2_event
    event_data:
      entity_id: media_player.my_mpd2
      type: current_song_changed
actions:
- action: input_select.select_option
    target:
      entity_id: input_select.mpd2_select_songs
    data:
      option: "{{ trigger.event.data.curr_media_title }} ----- {{ trigger.event.data.curr_media_id }} "
```

### Custom actions

#### mpd2.mpd_update

Updates datbase and rescan all songs in music directory.

Example:

```
action: mpd2.mpd_update
target:
  entity_id: media_player.my_mpd2
```