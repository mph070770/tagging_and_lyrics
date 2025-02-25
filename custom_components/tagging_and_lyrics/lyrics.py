import logging
import datetime
import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
import lrc_kit
import time
import re
import asyncio

_LOGGER = logging.getLogger(__name__)

# Global variable to store last processed media content ID
LAST_MEDIA_CONTENT_ID = None
ACTIVE_LYRICS_LOOP = None  # mananges the situation where new lyrics are requested when some are still playing - important for radio streams where tracks get cut short

SERVICE_FETCH_LYRICS_SCHEMA = vol.Schema({
    vol.Required("entity_id"): cv.entity_id
})

def lyricSplit(lyrics):
    """Split lyrics into a timeline and corresponding lines."""
    timeline = []
    lrc = []

    for line in lyrics.splitlines():
        if line.startswith(("[0", "[1", "[2", "[3")):
            # Match timestamp in square brackets (e.g., [01:15.35])
            regex = re.compile(r'\[.+?\]')
            match = re.match(regex, line)

            if not match:
                continue  # Skip lines with no timestamp

            # Extract and clean the timestamp
            _time = match.group(0)[1:-1]  # Remove square brackets
            line = regex.sub('', line).strip()  # Remove timestamp from the line

            if not line:  # Skip if the line is empty after removing the timestamp
                continue

            # Convert the timestamp to milliseconds
            try:
                time_parts = _time.split(':')
                minutes = int(time_parts[0])
                seconds = float(time_parts[1])
                milliseconds = int((minutes * 60 + seconds) * 1000)

                timeline.append(milliseconds)
                lrc.append(line)
            except (ValueError, IndexError) as e:
                _LOGGER.warning("Invalid timestamp format: %s", _time)
                continue

    return timeline, lrc

def calculate_media_timecode(pos, updated):
    """Calculate the current media position."""
    if pos is None or updated is None:
        return 0

    if isinstance(updated, datetime.datetime):
        last_update_time = updated
    else:
        try:
            last_update_time = datetime.datetime.fromisoformat(updated)
        except ValueError:
            _LOGGER.error("Error parsing updated_at timestamp: %s", updated)
            return 0

    current_time = datetime.datetime.now(datetime.timezone.utc)
    elapsed_time = (current_time - last_update_time).total_seconds()
    return round(pos + elapsed_time, 2)

async def update_lyrics_input_text(hass: HomeAssistant, previous_line: str, current_line: str, next_line: str):
    """Update the input_text entities with the current lyrics lines."""
    await hass.services.async_call("input_text", "set_value", {"entity_id": "input_text.line1", "value": previous_line})
    await hass.services.async_call("input_text", "set_value", {"entity_id": "input_text.line2", "value": current_line})
    await hass.services.async_call("input_text", "set_value", {"entity_id": "input_text.line3", "value": next_line})

def clean_track_name(track):
    """Clean up the track name by removing unwanted text, special characters, and comments."""

    # 1. Remove text inside parentheses or brackets (e.g., "(single version)", "[remastered]")
    track = re.sub(r'\s*[\(\[].*?[\)\]]', '', track)

    # 2. Remove text after a hyphen (e.g., " - From ...")
    track = re.split(r'\s*-\s*', track)[0]

    # 3. Remove phrases like 'From "..." Soundtrack'
    track = re.sub(r'\bfrom\s+".+?"\s+soundtrack\b', '', track, flags=re.IGNORECASE)
    track = re.sub(r'\bfrom\s+.+?\s+soundtrack\b', '', track, flags=re.IGNORECASE)
    track = re.sub(r'\s+\(album version\)\b', '', track, flags=re.IGNORECASE)
    track = re.sub(r'\s+\(radio edit\)\b', '', track, flags=re.IGNORECASE)
    track = re.sub(r'\s+\(remix\)\b', '', track, flags=re.IGNORECASE)
    track = re.sub(r'\s+\(edit\)\b', '', track, flags=re.IGNORECASE)

    # 4. Remove Chinese characters
    track = re.sub(r'[\u4e00-\u9fff]+', '', track)

    # 5. Replace special quotes/apostrophes
    track = track.replace("’", "'").replace("“", '"').replace("”", '"')

    # 6. Trim whitespace
    return track.strip()

def trigger_lyrics_lookup(hass: HomeAssistant, title: str, artist: str, play_offset_ms: int, process_begin: str):
    """Trigger lyrics lookup based on a recognized song."""

    _LOGGER.info("Fetching lyrics for tagged song")

    if not title or not artist:
        _LOGGER.warning("Cannot trigger lyrics lookup: Missing title or artist.")
        return

    _LOGGER.info("Fetching lyrics for: %s - %s", title, artist)

     # Get the configured media player entity ID
    media_player = hass.data["tagging_and_lyrics"]["media_player"]

    fetch_lyrics_for_track(hass, title, artist, play_offset_ms/1000, process_begin, media_player, True) #fingerprinting is true

def get_media_player_info(hass: HomeAssistant, entity_id: str):
    """Retrieve track, artist, media position, and last update time from media player."""
    player_state = hass.states.get(entity_id)

    _LOGGER.info("mediaID: %s", player_state.attributes.get("media_content_id"))
    _LOGGER.info("playerState: %s", player_state)
    _LOGGER.info("@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@")
    _LOGGER.info("Attributes: %s", player_state.attributes)
    _LOGGER.info("@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@")
    
    if not player_state:
        _LOGGER.error("Media player entity not found.")
        update_lyrics_input_text(hass, "Media player entity not found", "", "")
        return None, None, None, None  # Return empty values

    if player_state.state != "playing":
        _LOGGER.info("Media player is not playing. Waiting...")
        update_lyrics_input_text(hass, "Waiting for playback to start", "", "")
        return None, None, None, None

    track = clean_track_name(player_state.attributes.get("media_title", ""))
    artist = player_state.attributes.get("media_artist", "")
    pos = player_state.attributes.get("media_position")
    updated_at = player_state.attributes.get("media_position_updated_at")

    if not track or not artist:
        _LOGGER.warning("Missing track or artist information.")
        update_lyrics_input_text(hass, "Missing track or artist", "", "")
        return None, None, None, None

    return track, artist, pos, updated_at

async def fetch_lyrics_for_track(hass: HomeAssistant, track: str, artist: str, pos, updated_at, entity_id, audiofingerprint):
    """Fetch lyrics for a given track and synchronize with playback."""
    global ACTIVE_LYRICS_LOOP  

    _LOGGER.info("Fetching lyrics for: '%s' by '%s'", track, artist)

    _LOGGER.debug("pos=%s, updated_at=%s", pos, updated_at)

    # Ensure they are valid
    if pos is None or updated_at is None:
        _LOGGER.error("pos or updated_at is not initialized. Exiting lyrics sync.")
        ACTIVE_LYRICS_LOOP = None
        return

    # Stop any existing loop before starting a new one
    #if ACTIVE_LYRICS_LOOP:
    #    _LOGGER.warning("Stopping previous lyrics session.")
    #    ACTIVE_LYRICS_LOOP = False  
        
        # Wait until the previous loop fully exits
        #while ACTIVE_LYRICS_LOOP is not None:
        #    time.sleep(0.1)
        #    _LOGGER.warning(".")

    # Check if the switch is enabled
    if not hass.states.is_state("input_boolean.lyrics_enable", "on"):
        _LOGGER.info("Lyrics fetching is disabled by switch. Exiting.")
        return

    # Check the media_content_id for special cases
    media_content_id = hass.states.get(entity_id).attributes.get("media_content_id", "")
    
    #_LOGGER.info("Checking media source.")
    #if media_content_id.startswith("library://radio"):
    #    _LOGGER.info("Radio stream detected (library://radio). Not fetching lyrics unless manually triggered.")
    #    return
   # 
   # if not media_content_id.startswith("spotify://") and not hass.states.is_state("input_boolean.lyrics_enable", "on"):
   #     _LOGGER.info("Lyrics are disabled, and not a Spotify track. Exiting.")
   #     return

    _LOGGER.info("Start new session")
    await update_lyrics_input_text(hass, "", "", "")
    # Start new session
    ACTIVE_LYRICS_LOOP = True
    timeline = []
    lrc = []

    # Load lyrics
    _LOGGER.info("Load lyrics")
    lyrics_provider = [lrc_kit.QQProvider]
    provider = lrc_kit.ComboLyricsProvider(lyrics_provider)

    _LOGGER.info("Searching for lyrics.")
    search_request = lrc_kit.SearchRequest(artist, track)
    lyrics_result = provider.search(search_request)

    if not lyrics_result:
        _LOGGER.warning("No lyrics found for '%s'.", track)
        #update_lyrics_input_text(hass, "No lyrics found", "", "")
        await update_lyrics_input_text(hass, "", "", "")
        return

    _LOGGER.warning("Processing lyrics into timeline")
    timeline, lrc = lyricSplit(str(lyrics_result))

    if not timeline:
        _LOGGER.error("Lyrics have no timeline.")
        await update_lyrics_input_text(hass, "Lyrics not synced", "", "")
        return

    _LOGGER.warning("Synchronizing lyrics")

    # Track last known title and artist
    #if audiofingerprint: # new metadata was used from tagging for the lyrics look up, put them back to (probably) the radio station name etc (TODO - make this less messy)
    #    track = clean_track_name(hass.states.get(entity_id).attributes.get("media_title", ""))
    #    artist = hass.states.get(entity_id).attributes.get("media_artist", "")
    #    audiofingerprint=False
    
    #last_title = track
    #last_artist = artist

    last_media = media_content_id

    #_LOGGER.info("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!  AUDIOFINGERPRINT = %s", audiofingerprint)  

    while ACTIVE_LYRICS_LOOP:  
        player_state = hass.states.get(entity_id).state

        # Check if media player is paused
        if player_state == "paused":
            _LOGGER.info("Media player paused. Clearing lyrics display.")
            await update_lyrics_input_text(hass, "", "", "")

            pause_start = datetime.datetime.now(datetime.timezone.utc)
            while hass.states.get(entity_id).state == "paused":
                #time.sleep(1)
                await asyncio.sleep(1)

            pause_end = datetime.datetime.now(datetime.timezone.utc)
            pause_duration = (pause_end - pause_start).total_seconds()
            _LOGGER.info("Media player resumed. Adjusting updated_at by %s seconds.", pause_duration)

            if isinstance(updated_at, str):
                updated_at = datetime.datetime.fromisoformat(updated_at)

            updated_at += datetime.timedelta(seconds=pause_duration)

        # Check for title or artist change
        #current_title = clean_track_name(hass.states.get(entity_id).attributes.get("media_title", ""))
        #current_artist = hass.states.get(entity_id).attributes.get("media_artist", "")

        #_LOGGER.info("AUDIOFINGERPRINT - [%s] %s->%s, %s->%s. ", audiofingerprint, current_artist, last_artist, current_title, last_title)

        media_content_id = hass.states.get(entity_id).attributes.get("media_content_id", "")
        #_LOGGER.info(">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>> %s->%s, %s", media_content_id, last_media, ACTIVE_LYRICS_LOOP)
        #if (current_title != last_title or current_artist != last_artist):
#        if media_content_id != last_media: #media has changed and is being processed by mediaplayer monitor
        #if media_content_id != LAST_MEDIA_CONTENT_ID: #media has changed
#            time.sleep(0.5) #seems there's a race between all the media attributes getting updated.  Allow time for media_content_id to also be detected by the mediaplayer monitor
#            _LOGGER.info("Media has changed - %s->%s. Quit.", media_content_id, last_media)
#            update_lyrics_input_text(hass, "", "", "")
#            ACTIVE_LYRICS_LOOP = False  # Stop current loop
#            timeline = []
#            lrc = []
#            _LOGGER.info("Track has been changed, exiting loop.")
#            break

            # Re-fetch fresh timing info for the new track
        #    new_pos = hass.states.get(entity_id).attributes.get("media_position")
        #    new_updated_at = hass.states.get(entity_id).attributes.get("media_position_updated_at")

            # Trigger new lyrics fetch with fresh timing data
        #    fetch_lyrics_for_track(hass, current_title, current_artist, new_pos, new_updated_at, entity_id)
        #    return  # Exit current function to allow the new loop to start

        # Calculate media timecode
        media_timecode = calculate_media_timecode(pos, updated_at)

        if media_timecode * 1000 >= timeline[-1]:  # Exit if lyrics finished
            _LOGGER.info("Lyrics finished, exiting loop.")
            await update_lyrics_input_text(hass, "", "", "")
            break

        # Display synchronized lyrics
        if timeline:
            for n in range(1, len(timeline)):
                if timeline[n - 1] <= media_timecode * 1000 <= timeline[n]:
                    previous_line = lrc[n - 2] if n > 1 else ""
                    current_line = lrc[n - 1]
                    next_line = lrc[n] if n < len(lrc) else ""

                    await update_lyrics_input_text(hass, previous_line, current_line, next_line)

                    sleep_time = (timeline[n] - media_timecode * 1000) / 1000.0
                    total_sleep = max(0.1, sleep_time)  # Ensure minimum sleep time
                    interval = 0.1  # Check every 0.1 seconds

                    while total_sleep > 0:
                        if not ACTIVE_LYRICS_LOOP:  # Check if loop should exit
                            _LOGGER.info("Lyrics loop interrupted, exiting sleep.")
                            await update_lyrics_input_text(hass, "", "", "")
                            break  # Exit the async function immediately
                        
                        #time.sleep(min(interval, total_sleep))  # Sleep for interval or remaining time
                        await asyncio.sleep(min(0.1, total_sleep))
                        total_sleep -= interval  # Reduce remaining sleep time

    #get more lyrics if the mediaplayer is continuing (but not when it's streaming radio...)
    _LOGGER.info("Lyrics sync loop ended.")
    
    # Reset ACTIVE_LYRICS_LOOP to None when loop exits
    ACTIVE_LYRICS_LOOP = None  

    # Check if media player is still playing and a new track is playing
    #if hass.states.get(entity_id).state == "playing":
    #    new_title = clean_track_name(hass.states.get(entity_id).attributes.get("media_title", ""))
    #    new_artist = hass.states.get(entity_id).attributes.get("media_artist", "")

        # Only refetch if a new track or artist is detected
#        if (new_title != track or new_artist != artist) and not media_content_id.startswith("library://radio"):
  #          _LOGGER.info("New track detected: '%s' by '%s'. Refetching lyrics.", new_title, new_artist)
            
           # Get fresh timing info
    #        new_pos = hass.states.get(entity_id).attributes.get("media_position")
    #        new_updated_at = hass.states.get(entity_id).attributes.get("media_position_updated_at")
            
            # Start new lyrics fetch for the new track
     #       fetch_lyrics_for_track(hass, new_title, new_artist, new_pos, new_updated_at, entity_id)

def handle_fetch_lyrics(hass: HomeAssistant, call: ServiceCall):
    """Main service handler: gets media info and fetches lyrics."""
    entity_id = call.data.get("entity_id")
    
    # Stop any current session cleanly
    global ACTIVE_LYRICS_LOOP
    if ACTIVE_LYRICS_LOOP:
        _LOGGER.warning("Stopping current lyrics session for new request.")
        ACTIVE_LYRICS_LOOP = False

    def monitor_playback(entity, old_state, new_state):
        """Monitor media player state changes."""
        global LAST_MEDIA_CONTENT_ID
        global ACTIVE_LYRICS_LOOP

        _LOGGER.debug("Media player state changed: %s -> %s", old_state.state if old_state else "None", new_state.state)

        _LOGGER.info("***************************************************************")
        _LOGGER.info("Entity ID from call data: %s", entity)
        media_content_id = hass.states.get(entity).attributes.get("media_content_id", "")
        _LOGGER.info("fetch_lyrics service called for entity: %s", entity)
        _LOGGER.info(">>>media_content_id: %s", media_content_id)
        _LOGGER.info("***************************************************************")

        #_LOGGER.info("Entity ID: old:%s new:%s", old_state.entity_id, new_state.entity_id)
        #_LOGGER.info("State: old:%s new:%s", old_state.state, new_state.state)
        #_LOGGER.info("Attributes: old:%s new:%s", old_state.attributes, new_state.attributes)
        #_LOGGER.info("Last Changed: old:%s new:%s", old_state.last_changed, new_state.last_changed)
        #_LOGGER.info("Last Updated: old:%s new:%s", old_state.last_updated, new_state.last_updated)

        # Only act if the player changes to 'playing' and it's not a radio station
        if new_state.state == "playing" and not media_content_id.startswith("library://radio"):
            #_LOGGER.info("Media player is now playing. Starting lyrics fetching.")
            #track, artist, pos, updated_at = get_media_player_info(hass, entity_id)
            #_LOGGER.info("********* artist %s, track %s, media_content_id %s *********", artist, track, media_content_id)
            
            _LOGGER.debug("LAST_MEDIA_CONTENT_ID: %s", LAST_MEDIA_CONTENT_ID)
            _LOGGER.debug("media_content_id: %s", media_content_id)

            # Check if the media_content_id is different from the last one processed
            if media_content_id and media_content_id != LAST_MEDIA_CONTENT_ID:
                _LOGGER.info("ACTIVE_LYRICS_LOOP = False")
                ACTIVE_LYRICS_LOOP = False
                _LOGGER.info("New media detected. Fetching lyrics.")
                update_lyrics_input_text(hass, "", "", "")
                track, artist, pos, updated_at = get_media_player_info(hass, entity)
                _LOGGER.info("********* artist %s, track %s, media_content_id %s *********", 
                            artist, track, media_content_id)
                
                # Call the lyrics function and update the last processed ID
                if track and artist:
                    _LOGGER.debug("Fetching>>>>>>>>>>")
                    LAST_MEDIA_CONTENT_ID = media_content_id
                    #fetch_lyrics_for_track(hass, track, artist, pos, updated_at, entity, False)
                    fetch_lyrics_for_track(hass, track, artist, 0, updated_at, entity, False) #Pos wasn't updated at the same time as media_content_id??
            else:
                _LOGGER.info("Track already processed. Skipping lyrics fetch.")
        else:
            if new_state.state=="playing" and media_content_id:
                LAST_MEDIA_CONTENT_ID = media_content_id #it's a radio station so capture the change of stream but don't fetch lyrics
            if ACTIVE_LYRICS_LOOP is not None:
                _LOGGER.info("+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++ACTIVE_LYRICS_LOOP = %s", ACTIVE_LYRICS_LOOP)
            else:
                _LOGGER.info("-----------------------------------------------------------------------None")
            ACTIVE_LYRICS_LOOP = None
            if ACTIVE_LYRICS_LOOP is not None:
                _LOGGER.info("+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++ACTIVE_LYRICS_LOOP = %s", ACTIVE_LYRICS_LOOP)
            else:
                _LOGGER.info("-----------------------------------------------------------------------None")


    # Register listener for state changes
    hass.helpers.event.async_track_state_change(entity_id, monitor_playback)
    _LOGGER.debug("Registered state change listener for: %s", entity_id)

    # Check current state immediately
    #current_state = hass.states.get(entity_id).state
    #if current_state == "playing":
    #    _LOGGER.info("Media player is already playing. Starting lyrics fetching immediately.")
    #    track, artist, pos, updated_at = get_media_player_info(hass, entity_id)
    #    if track and artist:
    #        fetch_lyrics_for_track(hass, track, artist, pos, updated_at, entity_id, False)
    #else:
    #    _LOGGER.info("Media player is not playing. Waiting for state change.")


async def async_setup_lyrics_service(hass: HomeAssistant):
    """Register the fetch_lyrics service."""
    _LOGGER.debug("Registering the fetch_lyrics service.")

    hass.services.async_register(
        "tagging_and_lyrics",
        "fetch_lyrics",
        lambda call: handle_fetch_lyrics(hass, call),
        schema=SERVICE_FETCH_LYRICS_SCHEMA
    )
    _LOGGER.info("fetch_lyrics service registered successfully.")