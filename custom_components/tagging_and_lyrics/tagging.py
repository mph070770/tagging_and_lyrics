import json
import logging
import socket
import time
import datetime
import io
import re
import wave
import threading
import voluptuous as vol
import asyncio
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_registry import async_get
from acrcloud.recognizer import ACRCloudRecognizer, ACRCloudRecognizeType
# Import trigger function from lyrics.py
from .lyrics import trigger_lyrics_lookup, update_lyrics_input_text

# Define whether lyrics lookup should be enabled after tagging
ENABLE_LYRICS_LOOKUP = True  # Change to False if you don't want automatic lyrics lookup
FINETUNE_SYNC = 2 #was 3

_LOGGER = logging.getLogger(__name__)

# Constants
UDP_PORT = 6056
BUFFER_TIME = 10  # Default seconds
SAMPLE_RATE = 16000
CHUNK_SIZE = 4096
CHANNELS = 1
SAMPLE_WIDTH = 2

# Service Schema
SERVICE_FETCH_AUDIO_TAG_SCHEMA = vol.Schema({
    vol.Optional("duration", default=10): vol.All(vol.Coerce(int), vol.Range(min=1, max=60))
})

def clean_text(text):
        """Remove Chinese characters from the given text."""
        return re.sub(r'[\u4e00-\u9fff]+', '', text).strip()

    
def format_time(ms):
    """Convert milliseconds to MM:SS format."""
    minutes = ms // 60000
    seconds = (ms % 60000) // 1000
    return f"{minutes}:{seconds:02d}"

class TaggingService:
    """Service to listen for UDP audio samples and process them."""
    def __init__(self, hass: HomeAssistant):
        self.hass = hass

        if self.hass:
            _LOGGER.debug("TaggingService initialized with hass.")
        else:
            _LOGGER.error("TaggingService initialized WITHOUT hass.")

        conf = hass.data["tagging_and_lyrics"]

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # Allow reuse
        self.sock.bind(("0.0.0.0", conf["port"]))
        self.running = True

        self.config = {
            'host': conf["host"],
            'access_key': conf["access_key"],
            'access_secret': conf["access_secret"],
            'recognize_type': ACRCloudRecognizeType.ACR_OPT_REC_AUDIO,
            'debug': False,
            'timeout': 10
        }
        self.recognizer = ACRCloudRecognizer(self.config)

    async def receive_udp_data(self, duration):
        """Non-blocking UDP data reception using asyncio."""
        loop = asyncio.get_running_loop()
        data_buffer = []

        def socket_recv():
            try:
                return self.sock.recvfrom(CHUNK_SIZE)
            except Exception as e:
                _LOGGER.error("Socket error: %s", e)
                return None, None

        start_time = time.time()
        while time.time() - start_time < duration:
            data, addr = await asyncio.to_thread(socket_recv)
            if data:
                data_buffer.append(data)
            await asyncio.sleep(0.01)  # Yield control to the event loop

        return data_buffer
    
    def write_audio_file(self, filename, frames):
        with wave.open(filename, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(SAMPLE_WIDTH)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(b"".join(frames))


    def recognize_audio(self, filename):
        return self.recognizer.recognize_by_file(filename, 0, 10)


    async def listen_for_audio(self, duration):
        """Listen for UDP audio data for the specified duration."""
        try:
            _LOGGER.info("Waiting for incoming UDP audio data...")

            await self.hass.services.async_call("switch", "turn_on", {"entity_id": "switch.home_assistant_mic_093d58_tagging_enable"})
    
            #data, addr = self.sock.recvfrom(CHUNK_SIZE)

            buffer = await self.receive_udp_data(duration)

            _LOGGER.info("Audio detected, starting recording for %d seconds...", duration)
            
            await update_lyrics_input_text(self.hass, "Listening......", "", "")
            #process_begin = datetime.datetime.now(datetime.timezone.utc)

            #start_time = time.time()
            #buffer = []

            #while time.time() - start_time < duration:
            #    buffer.append(data)
            #    data, addr = self.sock.recvfrom(CHUNK_SIZE)

            

            # Convert buffer to WAV file
            wav_filename = "recorded_audio.wav"
            await asyncio.to_thread(self.write_audio_file, wav_filename, buffer)
            _LOGGER.info("Recording complete. Sending to ACRCloud...")
            #with wave.open(wav_filename, "wb") as wf:
            #wf = await asyncio.to_thread(wave.open, wav_filename, "wb")
            #with wf:
            #    wf.setnchannels(CHANNELS)
            #    wf.setsampwidth(SAMPLE_WIDTH)
            #    wf.setframerate(SAMPLE_RATE)
            #    wf.writeframes(b"".join(buffer))

            # Disable the tagging switch after WAV file creation
            await self.hass.services.async_call("switch", "turn_off", {"entity_id": "switch.home_assistant_mic_093d58_tagging_enable"})

            try:
                response = await asyncio.to_thread(self.recognize_audio, wav_filename)
                _LOGGER.info("ACRCloud Response: %s", response)
                #summary = self.parse_acrcloud_response(response)
                #self.hass.states.set("sensor.tagging_result", summary)
            except Exception as e:
                _LOGGER.error("Error in Tagging Service: %s", e)
                await asyncio.to_thread(self.hass.states.set("sensor.tagging_result", "No match"))
            finally:
                await asyncio.to_thread(self.hass.states.set("switch.tag_enable", "off")) #Needed??

            # Send to ACRCloud
            #process_begin = datetime.datetime.now(datetime.timezone.utc)
            #response = self.recognizer.recognize_by_file(wav_filename, 0, 10)
            #response = await asyncio.to_thread(self.recognizer.recognize_by_file, wav_filename, 0, 10)
            #await update_lyrics_input_text(self.hass, "Matching......", "", "")
            #_LOGGER.info("ACRCloud Response: %s", response)

            # Parse JSON response
            response_data = json.loads(response)

            if "metadata" in response_data and "music" in response_data["metadata"]:
                first_match = response_data["metadata"]["music"][0]  # Get the first match
                
                artist_name = clean_text(first_match["artists"][0]["name"]) if "artists" in first_match else "Unknown Artist"
                title = clean_text(first_match.get("title", "Unknown Title"))
                play_offset_ms = first_match.get("play_offset_ms", 0)
                play_time = format_time(play_offset_ms)

                # Short summary for sensor (title, artist, playtime)
                summary = f"{title} - {artist_name} ({play_time})"
                self.hass.states.async_set("sensor.tagging_result", summary)
                #if self.hass:
                #    await self.hass.states.async_set("sensor.tagging_result", summary)
                #else:
                #    _LOGGER.error("Home Assistant instance is None. Cannot set state.") #MH - not sure about this as the fix??


                # Full response stored in a persistent notification
                self.hass.services.call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "Audio Tagging Full Result",
                        "message": f"```json\n{response}\n```",
                        "notification_id": "tagging_full_result"
                    }
                )

                # Formatted response for the main notification
                message = f"🎵 **Title**: {title}\n👤 **Artist**: {artist_name}\n⏱️ **Play Offset**: {play_time} (MM:SS)"

            else:
                message = "No music recognized."
                self.hass.states.async_set("sensor.tagging_result", "No match")
                #if self.hass:
                #    await self.hass.states.async_set("sensor.tagging_result", "No match")
                #else:
                #    _LOGGER.error("Home Assistant instance is None. Cannot set state.")

            await update_lyrics_input_text(self.hass, "", "", "")

            # Create a persistent notification with the formatted response
            self.hass.services.call(
                "persistent_notification",
                "create",
                {
                    "title": "Audio Tagging Result",
                    "message": message,
                    "notification_id": "tagging_result"
                }
            )

            # Inside TaggingService.listen_for_audio() after successful tagging:
            if ENABLE_LYRICS_LOOKUP:
                if title and artist_name:
                    process_begin = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=FINETUNE_SYNC)
                    _LOGGER.info("Triggering lyrics lookup for: %s - %s", title, artist_name)
                    await trigger_lyrics_lookup(self.hass, title, artist_name, play_offset_ms, process_begin.isoformat())


        except Exception as e:
            _LOGGER.error("Error in Tagging Service: %s", e)
            # Ensure switch is turned off in case of an error
            self.hass.states.async_set("switch.tag_enable", "off")
            #if self.hass:
            #    await self.hass.states.async_set("switch.tag_enable", "off")
            #else:
            #    _LOGGER.error("Home Assistant instance is None. Cannot set state.")

    def stop(self):
        """Stop the tagging service."""
        self.running = False
        self.sock.close()


async def handle_fetch_audio_tag(hass: HomeAssistant, call: ServiceCall):
    """Handle the service call for fetching audio tags."""
    duration = call.data.get("duration", 10)

    _LOGGER.info("fetch_audio_tag service called. Recording duration: %s seconds", duration)

    # Stop any running instance before starting a new one
    if "tagging_service" in hass.data:
        _LOGGER.info("Stopping existing tagging service before starting a new one.")
        hass.data["tagging_service"].stop()

    tagging_service = TaggingService(hass)
    hass.data["tagging_service"] = tagging_service  # Store the instance

    #thread = threading.Thread(target=tagging_service.listen_for_audio, args=(duration,), daemon=True)
    #thread.start()

    await tagging_service.listen_for_audio(duration)


async def async_setup_tagging_service(hass: HomeAssistant):
    """Register the fetch_audio_tag service in Home Assistant."""
    _LOGGER.info("Registering the fetch_audio_tag service.")

    async def async_wrapper(call):
        await handle_fetch_audio_tag(hass, call)

    hass.services.async_register(
        "tagging_and_lyrics",
        "fetch_audio_tag",
        async_wrapper,
        schema=SERVICE_FETCH_AUDIO_TAG_SCHEMA
    )

    _LOGGER.info("fetch_audio_tag service registered successfully.")
