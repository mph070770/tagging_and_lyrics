import json
import logging
import urllib.parse
import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
import voluptuous as vol
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN,
    SPOTIFY_AUTH_CALLBACK_PATH,
    SPOTIFY_STORAGE_VERSION,
    SPOTIFY_STORAGE_KEY,
    SPOTIFY_SCOPE,
    DEFAULT_SPOTIFY_PLAYLIST_NAME
)

_LOGGER = logging.getLogger(__name__)

# Configuration schema
SPOTIFY_CONFIG_SCHEMA = vol.Schema({
    vol.Required("client_id"): cv.string,
    vol.Required("client_secret"): cv.string,
    vol.Optional("playlist_id"): cv.string,
    vol.Optional("create_playlist", default=True): cv.boolean,
    vol.Optional("playlist_name", default=DEFAULT_SPOTIFY_PLAYLIST_NAME): cv.string,
})

# Schema for the add_to_spotify service call
SERVICE_ADD_TO_SPOTIFY_SCHEMA = vol.Schema({
    vol.Optional("title"): cv.string,
    vol.Optional("artist"): cv.string,
})

class SpotifyAuthView(HomeAssistantView):
    """Handle Spotify authentication callbacks."""
    url = SPOTIFY_AUTH_CALLBACK_PATH
    name = "api:tagging_and_lyrics:spotify_callback"
    requires_auth = False

    def __init__(self, hass):
        """Initialize the Spotify auth callback view."""
        self.hass = hass

    async def get(self, request):
        """Handle Spotify auth callback requests."""
        code = request.query.get("code")
        
        if not code:
            error = request.query.get("error", "Unknown error")
            _LOGGER.error(f"Spotify authentication failed: {error}")
            return aiohttp.web.Response(
                text=f"<html><body><h1>Authentication Error</h1><p>{error}</p></body></html>",
                content_type="text/html",
            )
        
        spotify_service = self.hass.data.get("spotify_service")
        if not spotify_service:
            _LOGGER.error("Spotify service not initialized")
            return aiohttp.web.Response(
                text="<html><body><h1>Setup Error</h1><p>Spotify service not initialized</p></body></html>",
                content_type="text/html",
            )
        
        await spotify_service.exchange_code(code)
        
        return aiohttp.web.Response(
            text="<html><body><h1>Authentication Successful</h1><p>You can close this window now</p></body></html>",
            content_type="text/html",
        )

class SpotifyService:
    """Service to add tracks to Spotify playlists."""
    def __init__(self, hass: HomeAssistant, config):
        """Initialize the Spotify service."""
        self.hass = hass
        self.client_id = config["client_id"]
        self.client_secret = config["client_secret"]
        self.playlist_id = config.get("playlist_id")
        self.create_playlist = config.get("create_playlist", True)
        self.playlist_name = config.get("playlist_name", DEFAULT_SPOTIFY_PLAYLIST_NAME)
        self.session = async_get_clientsession(hass)
        self.user_id = None
        self.authorized = False
        
        # Set up storage for tokens
        self.store = Store(hass, SPOTIFY_STORAGE_VERSION, f"{DOMAIN}_{SPOTIFY_STORAGE_KEY}")
        self.access_token = None
        self.refresh_token = None
        self.expires_at = 0
    
    async def async_setup(self):
        """Set up the Spotify service."""
        await self.load_tokens()
        
        # If we have a refresh token, try to use it
        if self.refresh_token:
            await self.refresh_access_token()
        
        # Set up authentication callback
        self.hass.http.register_view(SpotifyAuthView(self.hass))
        
        # If we're authorized and don't have a playlist ID but should create one
        if self.authorized and not self.playlist_id and self.create_playlist:
            await self._create_playlist()
            
        return self.authorized
    
    async def load_tokens(self):
        """Load tokens from storage."""
        data = await self.store.async_load()
        if data:
            self.access_token = data.get("access_token")
            self.refresh_token = data.get("refresh_token")
            self.expires_at = data.get("expires_at", 0)
            self.user_id = data.get("user_id")
            
            # Check if token is still valid or can be refreshed
            if self.refresh_token:
                return True
        return False
    
    async def save_tokens(self):
        """Save tokens to storage."""
        data = {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "user_id": self.user_id,
        }
        await self.store.async_save(data)
    
    def get_authorize_url(self):
        """Get the authorization URL for Spotify."""
        redirect_uri = f"{self.hass.config.api.base_url}{SPOTIFY_AUTH_CALLBACK_PATH}"
        
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SPOTIFY_SCOPE,
            "show_dialog": "true",
        }
        
        return f"https://accounts.spotify.com/authorize?{urllib.parse.urlencode(params)}"
    
    async def exchange_code(self, code):
        """Exchange authorization code for tokens."""
        redirect_uri = f"{self.hass.config.api.base_url}{SPOTIFY_AUTH_CALLBACK_PATH}"
        
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        
        try:
            async with self.session.post("https://accounts.spotify.com/api/token", data=payload) as resp:
                if resp.status != 200:
                    resp_json = await resp.json()
                    _LOGGER.error(f"Failed to exchange code: {resp.status} - {resp_json}")
                    return False
                
                tokens = await resp.json()
                self.access_token = tokens["access_token"]
                self.refresh_token = tokens["refresh_token"]
                self.expires_at = tokens["expires_in"] + int(self.hass.loop.time())
                
                # Get user info
                await self._fetch_user_info()
                
                # Save tokens
                await self.save_tokens()
                
                self.authorized = True
                
                # Create playlist if enabled and no playlist_id provided
                if not self.playlist_id and self.create_playlist:
                    await self._create_playlist()
                
                return True
        except Exception as e:
            _LOGGER.error(f"Error exchanging code: {e}")
            return False
    
    async def refresh_access_token(self):
        """Refresh the access token."""
        if not self.refresh_token:
            _LOGGER.error("No refresh token available")
            self.authorized = False
            return False
        
        # Check if token is still valid
        if self.expires_at > int(self.hass.loop.time()) + 300:  # 5 minute margin
            self.authorized = True
            return True
        
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        
        try:
            async with self.session.post("https://accounts.spotify.com/api/token", data=payload) as resp:
                if resp.status != 200:
                    resp_json = await resp.json()
                    _LOGGER.error(f"Failed to refresh token: {resp.status} - {resp_json}")
                    self.authorized = False
                    return False
                
                tokens = await resp.json()
                self.access_token = tokens["access_token"]
                self.expires_at = tokens["expires_in"] + int(self.hass.loop.time())
                
                # Refresh token might be returned
                if "refresh_token" in tokens:
                    self.refresh_token = tokens["refresh_token"]
                
                # Save tokens
                await self.save_tokens()
                
                self.authorized = True
                return True
        except Exception as e:
            _LOGGER.error(f"Error refreshing token: {e}")
            self.authorized = False
            return False
    
    async def _fetch_user_info(self):
        """Fetch user information from Spotify."""
        try:
            headers = {"Authorization": f"Bearer {self.access_token}"}
            async with self.session.get("https://api.spotify.com/v1/me", headers=headers) as resp:
                if resp.status != 200:
                    _LOGGER.error(f"Failed to fetch user info: {resp.status}")
                    return False
                
                user_info = await resp.json()
                self.user_id = user_info["id"]
                _LOGGER.info(f"Spotify authenticated for user: {self.user_id}")
                return True
        except Exception as e:
            _LOGGER.error(f"Error fetching user info: {e}")
            return False
    
    async def _create_playlist(self):
        """Create a new playlist for discovered tracks."""
        try:
            await self.refresh_access_token()
            if not self.authorized:
                return False
            
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            }
            
            payload = {
                "name": self.playlist_name,
                "public": False,
                "description": "Tracks identified by Home Assistant ACR",
            }
            
            async with self.session.post(
                f"https://api.spotify.com/v1/users/{self.user_id}/playlists",
                headers=headers,
                json=payload
            ) as resp:
                if resp.status not in (200, 201):
                    _LOGGER.error(f"Failed to create playlist: {resp.status}")
                    return False
                
                playlist = await resp.json()
                self.playlist_id = playlist["id"]
                _LOGGER.info(f"Created new Spotify playlist: {self.playlist_name} (ID: {self.playlist_id})")
                
                # Show notification
                await self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "Spotify Playlist Created",
                        "message": f"Created new playlist '{self.playlist_name}' for discovered tracks.",
                        "notification_id": "spotify_playlist_created"
                    }
                )
                
                return True
        except Exception as e:
            _LOGGER.error(f"Error creating playlist: {e}")
            return False
    
    async def search_track(self, title, artist):
        """Search for a track and return its Spotify URI."""
        await self.refresh_access_token()
        if not self.authorized:
            _LOGGER.error("Not authorized with Spotify")
            return None, None, None
        
        try:
            # Format the search query
            query = f"track:{title} artist:{artist}"
            query_params = {
                "q": query,
                "type": "track",
                "limit": 1
            }
            
            headers = {"Authorization": f"Bearer {self.access_token}"}
            
            async with self.session.get(
                f"https://api.spotify.com/v1/search?{urllib.parse.urlencode(query_params)}",
                headers=headers
            ) as resp:
                if resp.status != 200:
                    _LOGGER.error(f"Failed to search for track: {resp.status}")
                    return None, None, None
                
                results = await resp.json()
                
                if results["tracks"]["items"]:
                    track = results["tracks"]["items"][0]
                    return track["uri"], track["name"], track["artists"][0]["name"]
                else:
                    _LOGGER.warning(f"No Spotify track found for: {title} - {artist}")
                    return None, None, None
        except Exception as e:
            _LOGGER.error(f"Error searching for track: {e}")
            return None, None, None
    
    async def check_track_in_playlist(self, track_uri):
        """Check if the track is already in the playlist."""
        await self.refresh_access_token()
        if not self.authorized or not self.playlist_id:
            return False
        
        try:
            headers = {"Authorization": f"Bearer {self.access_token}"}
            
            # First, get the playlist's total tracks
            async with self.session.get(
                f"https://api.spotify.com/v1/playlists/{self.playlist_id}",
                headers=headers
            ) as resp:
                if resp.status != 200:
                    _LOGGER.error(f"Failed to get playlist info: {resp.status}")
                    return False
                
                playlist_info = await resp.json()
                total_tracks = playlist_info["tracks"]["total"]
            
            # Now check if the track is in the playlist
            # We might need to make multiple requests for large playlists
            offset = 0
            limit = 100
            
            while offset < total_tracks:
                params = {
                    "fields": "items(track(uri))",
                    "limit": limit,
                    "offset": offset
                }
                
                async with self.session.get(
                    f"https://api.spotify.com/v1/playlists/{self.playlist_id}/tracks?{urllib.parse.urlencode(params)}",
                    headers=headers
                ) as resp:
                    if resp.status != 200:
                        _LOGGER.error(f"Failed to get playlist tracks: {resp.status}")
                        return False
                    
                    tracks_data = await resp.json()
                    
                    # Check if the track is in this batch
                    track_uris = [item["track"]["uri"] for item in tracks_data["items"] if item["track"]]
                    if track_uri in track_uris:
                        return True
                    
                    offset += limit
            
            # Track not found in playlist
            return False
        except Exception as e:
            _LOGGER.error(f"Error checking track in playlist: {e}")
            return False
    
    async def add_track_to_playlist(self, title, artist):
        """Add a track to the specified playlist."""
        if not self.authorized:
            # Show notification for user to authorize
            auth_url = self.get_authorize_url()
            
            # Get the notification message
            message = f"Spotify authorization required to add tracks to playlists. " \
                      f"[Click here to authorize]({auth_url})"
            
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Spotify Authorization Required",
                    "message": message,
                    "notification_id": "spotify_auth_required"
                }
            )
            return False
        
        if not self.playlist_id:
            if self.create_playlist:
                success = await self._create_playlist()
                if not success:
                    _LOGGER.error("Failed to create playlist")
                    return False
            else:
                _LOGGER.error("No playlist ID provided")
                return False
        
        # Search for the track
        result = await self.search_track(title, artist)
        if not result or not result[0]:
            # Show notification
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Spotify Track Not Found",
                    "message": f"Could not find '{title}' by {artist} on Spotify.",
                    "notification_id": "spotify_track_status"
                }
            )
            return False
        
        track_uri, spotify_title, spotify_artist = result
        
        # Check if track is already in playlist
        in_playlist = await self.check_track_in_playlist(track_uri)
        if in_playlist:
            # Show notification
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Spotify Track Already Saved",
                    "message": f"The track '{spotify_title}' by {spotify_artist} is already in your playlist.",
                    "notification_id": "spotify_track_status"
                }
            )
            return True
        
        # Add track to playlist
        try:
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            }
            
            payload = {"uris": [track_uri]}
            
            async with self.session.post(
                f"https://api.spotify.com/v1/playlists/{self.playlist_id}/tracks",
                headers=headers,
                json=payload
            ) as resp:
                if resp.status not in (200, 201):
                    resp_json = await resp.json()
                    _LOGGER.error(f"Failed to add track to playlist: {resp.status} - {resp_json}")
                    
                    # Show error notification
                    await self.hass.services.async_call(
                        "persistent_notification",
                        "create",
                        {
                            "title": "Spotify Error",
                            "message": f"Failed to add track to playlist: HTTP {resp.status}",
                            "notification_id": "spotify_track_status"
                        }
                    )
                    return False
                
                # Show success notification
                await self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "Added Track to Spotify",
                        "message": f"Successfully added '{spotify_title}' by {spotify_artist} to your Spotify playlist.",
                        "notification_id": "spotify_track_status"
                    }
                )
                return True
        except Exception as e:
            _LOGGER.error(f"Error adding track to playlist: {e}")
            
            # Show error notification
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Spotify Error",
                    "message": f"Failed to add track to playlist: {str(e)}",
                    "notification_id": "spotify_track_status"
                }
            )
            return False

async def handle_add_to_spotify(hass, call):
    """Handle the service call to add a track to Spotify."""
    # Check if we have a title and artist in the call data
    title = call.data.get("title")
    artist = call.data.get("artist")
    
    # If not provided in the call, try to get from the last tagged song sensor
    if not title or not artist:
        last_song = hass.states.get("sensor.last_tagged_song")
        if last_song and last_song.attributes:
            title = last_song.attributes.get("title")
            artist = last_song.attributes.get("artist")
    
    if not title or not artist:
        _LOGGER.error("No title or artist provided")
        return
    
    _LOGGER.info(f"add_to_spotify service called for: {title} - {artist}")
    
    spotify_service = hass.data.get("spotify_service")
    if not spotify_service:
        _LOGGER.error("Spotify service not initialized")
        return
    
    await spotify_service.add_track_to_playlist(title, artist)

async def async_setup_spotify_service(hass, config):
    """Set up the Spotify integration and register service."""
    if "spotify" not in config:
        _LOGGER.info("No Spotify configuration found - skipping setup")
        return
    
    try:
        spotify_config = config["spotify"]
        spotify_service = SpotifyService(hass, spotify_config)
        hass.data["spotify_service"] = spotify_service
        
        # Initialize the service
        await spotify_service.async_setup()
        
        # Register the service
        hass.services.async_register(
            DOMAIN,
            "add_to_spotify",
            handle_add_to_spotify,
            schema=SERVICE_ADD_TO_SPOTIFY_SCHEMA
        )
        
        _LOGGER.info("Spotify service registered successfully")
    except Exception as e:
        _LOGGER.error(f"Failed to setup Spotify service: {e}")