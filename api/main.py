from fastapi import FastAPI, WebSocket, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from ytmusicapi import YTMusic
from typing import Dict, List, Optional, Set
import json
import asyncio
import logging
import time
import uvicorn
import os
from dotenv import load_dotenv
from enum import Enum
from dataclasses import dataclass
from datetime import datetime

# Load environment variables
load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Cache configuration
cache: Dict[str, tuple[List, float]] = {}
CACHE_DURATION = int(os.getenv("SEARCH_CACHE_DURATION", 3600))  # 1 hour default

# Add after cache configuration
playlist_cache: Dict[str, tuple[dict, float]] = {}
PLAYLIST_CACHE_DURATION = int(os.getenv("PLAYLIST_CACHE_DURATION", 300))  # 5 minutes default

class ClientType(Enum):
    MACOS_APP = "macos_app"
    CHROME_EXTENSION = "chrome_extension"

@dataclass
class Client:
    websocket: WebSocket
    client_type: ClientType
    last_pong: float
    connected_at: datetime

app = FastAPI(title="DropBeat Music API")

# Enable CORS with environment configuration
allowed_origins = os.getenv("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize YTMusic with environment configuration
ytmusic_oauth = os.getenv("YTMUSIC_OAUTH_FILE", "oauth.json")
ytmusic_headers = os.getenv("YTMUSIC_HEADERS_FILE", "headers_auth.json")

try:
    ytmusic = YTMusic(ytmusic_oauth)
except Exception as e:
    logger.warning(f"OAuth initialization failed: {e}, falling back to browser authentication")
    try:
        ytmusic = YTMusic(ytmusic_headers)
    except Exception as e:
        logger.warning(f"Browser authentication failed: {e}, falling back to unauthenticated mode")
        ytmusic = YTMusic()

# WebSocket connections
active_connections: Set[WebSocket] = set()

class ConnectionManager:
    def __init__(self):
        self.active_clients: Dict[int, Client] = {}
        self.ping_interval = 5  # seconds
        self.pong_timeout = 15  # seconds
        
    async def connect(self, websocket: WebSocket, client_type: ClientType):
        await websocket.accept()
        client_id = id(websocket)
        self.active_clients[client_id] = Client(
            websocket=websocket,
            client_type=client_type,
            last_pong=time.time(),
            connected_at=datetime.now()
        )
        logger.info(f"🔌 New {client_type.value} connection established (ID: {client_id})")
        
    def disconnect(self, websocket: WebSocket):
        client_id = id(websocket)
        if client_id in self.active_clients:
            client = self.active_clients[client_id]
            logger.info(f"🔌 {client.client_type.value} disconnected (ID: {client_id})")
            del self.active_clients[client_id]
            
    async def broadcast(self, message: dict, exclude: Optional[WebSocket] = None):
        for client_id, client in list(self.active_clients.items()):
            if client.websocket != exclude:
                try:
                    await client.websocket.send_json(message)
                except Exception as e:
                    logger.error(f"❌ Error broadcasting to client {client_id}: {e}")
                    await self.handle_disconnection(client.websocket)
                    
    async def send_to_type(self, message: dict, client_type: ClientType):
        for client_id, client in list(self.active_clients.items()):
            if client.client_type == client_type:
                try:
                    await client.websocket.send_json(message)
                except Exception as e:
                    logger.error(f"❌ Error sending to {client_type.value} {client_id}: {e}")
                    await self.handle_disconnection(client.websocket)
    
    async def handle_disconnection(self, websocket: WebSocket):
        try:
            await websocket.close()
        except:
            pass
        self.disconnect(websocket)
    
    async def start_ping_loop(self):
        while True:
            await asyncio.sleep(self.ping_interval)
            await self.check_connections()
    
    async def check_connections(self):
        current_time = time.time()
        for client_id, client in list(self.active_clients.items()):
            if current_time - client.last_pong > self.pong_timeout:
                logger.warning(f"⚠️ Client {client_id} ({client.client_type.value}) timed out")
                await self.handle_disconnection(client.websocket)
            else:
                try:
                    await client.websocket.send_json({"type": "PING", "timestamp": current_time})
                except Exception as e:
                    logger.error(f"❌ Error sending ping to {client_id}: {e}")
                    await self.handle_disconnection(client.websocket)

    def update_pong(self, websocket: WebSocket):
        client_id = id(websocket)
        if client_id in self.active_clients:
            self.active_clients[client_id].last_pong = time.time()

manager = ConnectionManager()

@app.get("/health")
async def health_check():
    """Check if the server is running"""
    return {"status": "healthy", "timestamp": time.time()}

@app.get("/test-ytmusic")
async def test_ytmusic():
    """Test if YTMusic API is working"""
    try:
        # Try a simple search
        test_query = "test"
        results = ytmusic.search(test_query, limit=1)
        return {
            "status": "ok",
            "message": "YTMusic API is working",
            "sample_results": results
        }
    except Exception as e:
        logger.error(f"YTMusic test failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/search/{query}")
async def test_search(query: str, limit: Optional[int] = None):
    """Test endpoint for search functionality"""
    try:
        results = await search(query, limit)
        return results
    except Exception as e:
        logger.error(f"Search test failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    client_type = None
    try:
        # Wait for initial message to determine client type
        initial_message = await websocket.receive_text()
        data = json.loads(initial_message)
        
        if data.get("type") != "IDENTIFY":
            raise ValueError("First message must be IDENTIFY")
            
        client_type_str = data.get("client")
        try:
            client_type = ClientType(client_type_str)
        except ValueError:
            raise ValueError(f"Invalid client type: {client_type_str}")
        
        await manager.connect(websocket, client_type)
        
        while True:
            try:
                message = await websocket.receive_text()
                data = json.loads(message)
                logger.info(f"📥 [{id(websocket)}] Received message: {data}")
                
                if data["type"] == "PING":
                    logger.info(f"🏓 [{id(websocket)}] Received PING, sending PONG")
                    await websocket.send_json({
                        "type": "PONG",
                        "timestamp": time.time()
                    })
                    manager.update_pong(websocket)
                
                elif data["type"] == "PONG":
                    manager.update_pong(websocket)
                
                elif data["type"] == "SEARCH":
                    try:
                        query = data["query"]
                        logger.info(f"🔍 [{id(websocket)}] Processing search request: {query}")
                        results = await search(query)
                        await websocket.send_json({
                            "type": "SEARCH_RESULTS",
                            "data": results
                        })
                    except Exception as e:
                        logger.error(f"❌ Search error: {str(e)}")
                        await websocket.send_json({
                            "type": "ERROR",
                            "error": str(e)
                        })
                
                elif data["type"] == "COMMAND":
                    # Forward commands from macOS app to Chrome extension
                    if client_type == ClientType.MACOS_APP:
                        await manager.send_to_type(data, ClientType.CHROME_EXTENSION)
                
                elif data["type"] == "TRACK_INFO":
                    # Forward track info from Chrome extension to macOS app
                    if client_type == ClientType.CHROME_EXTENSION:
                        await manager.send_to_type(data, ClientType.MACOS_APP)
                
            except json.JSONDecodeError as e:
                logger.error(f"❌ [{id(websocket)}] Invalid JSON received: {str(e)}")
                continue
            except Exception as e:
                logger.error(f"❌ [{id(websocket)}] Error processing message: {str(e)}", exc_info=True)
                continue
    
    except WebSocketDisconnect:
        logger.info(f"🔌 WebSocket disconnected normally")
    except Exception as e:
        logger.error(f"❌ WebSocket error: {str(e)}", exc_info=True)
    finally:
        await manager.handle_disconnection(websocket)

async def search(query: str, limit: Optional[int] = 15):
    try:
        logger.info(f"🔍 Starting search for: {query}")
        
        # Check cache
        cache_key = f"{query}_{limit}"
        if cache_key in cache:
            results, timestamp = cache[cache_key]
            if time.time() - timestamp < CACHE_DURATION:
                logger.info(f"💾 Cache hit for query: {query}")
                return results

        # Track seen IDs to avoid duplicates
        seen_ids = set()
        song_results = []
        
        # Search with better parameters
        logger.info("🎵 Performing YTMusic search...")
        search_results = ytmusic.search(
            query=query,
            filter="songs",
            limit=50,  # Increased to get more candidates for better filtering
            ignore_spelling=False
        )
        logger.info(f"✅ Found {len(search_results)} results")
        
        # Helper function to score results
        def score_result(item):
            score = 0
            title = item.get("title", "").lower()
            query_lower = query.lower()
            
            # Exact title match gets highest priority
            if query_lower == title:
                score += 300
            elif query_lower in title:
                score += 150
            
            # Check for movie soundtrack matches
            if "from" in title.lower() and query_lower in title.lower():
                score += 200
            
            # Official artist/album matches
            if item.get("album", {}).get("name", "").lower() == query_lower:
                score += 100
            
            # Official artist channel (highest authority)
            if "topic" in (item.get("author", "").lower()):
                score += 250  # Increased because official channels usually have the most relevant content
            
            # Verified artists
            if item.get("isVerified", False):
                score += 100
            
            # Duration penalty for very short or long videos (likely not songs)
            duration = item.get("duration", "")
            if duration:
                duration_parts = duration.split(":")
                if len(duration_parts) >= 2:
                    minutes = int(duration_parts[-2])
                    if minutes < 2 or minutes > 8:
                        score -= 50
            
            # Popularity bonus (significant factor in YouTube's ranking)
            if item.get("views"):
                try:
                    views = int(item["views"].replace(",", ""))
                    # Logarithmic scaling for views to prevent extremely popular videos from dominating
                    view_score = min(200, int(50 * (1 + views / 10000000)))  # Up to 200 points for views
                    score += view_score
                except (ValueError, AttributeError):
                    pass
            
            # Recent content bonus
            if item.get("year"):
                try:
                    year = int(item["year"])
                    current_year = datetime.now().year
                    if year == current_year:
                        score += 50
                    elif year == current_year - 1:
                        score += 30
                except (ValueError, TypeError):
                    pass
            
            return score
        
        scored_results = []
        
        for item in search_results:
            try:
                # Get and validate ID
                video_id = item.get("videoId")
                
                # Skip if no video ID or if we've seen it before
                if not video_id or video_id in seen_ids:
                    logger.debug(f"⚠️ Skipping item: {item.get('title')} - {'duplicate' if video_id in seen_ids else 'no video ID'}")
                    continue
                
                seen_ids.add(video_id)
                
                # Enhanced quality filtering
                category = item.get("category", "").lower()
                item_type = str(item.get("type", "")).lower()
                
                # Skip non-songs (but allow movie soundtracks)
                title = item.get("title", "").strip().lower()
                if (category != "songs" and "song" not in item_type) and "from" not in title:
                    logger.debug(f"⚠️ Skipping non-song: {video_id}")
                    continue
                
                # Get artists with better handling
                artists = []
                if item.get("artists"):
                    for artist in item["artists"]:
                        if isinstance(artist, dict) and artist.get("name"):
                            artists.append(artist["name"])
                
                # Get high-quality thumbnail
                thumbnail = None
                if item.get("thumbnails"):
                    thumbnails = item["thumbnails"]
                    if thumbnails:
                        thumbnail = thumbnails[-1]["url"]
                
                # Skip if title is missing or empty
                if not title:
                    logger.debug(f"⚠️ Skipping item without title: {video_id}")
                    continue
                
                # Create result object
                result = {
                    "id": video_id,
                    "title": item.get("title").strip(),
                    "artist": " & ".join(artists) if artists else item.get("author", "Unknown Artist"),
                    "thumbnailUrl": thumbnail,
                    "type": "song",
                    "duration": item.get("duration", "")
                }
                
                # Score the result
                score = score_result(item)
                scored_results.append((score, result))
                logger.debug(f"📝 Added song: {result['title']} with score {score}")
                
            except Exception as e:
                logger.error(f"❌ Error formatting result: {str(e)}", exc_info=True)
                continue

        # Sort by score and take top results
        scored_results.sort(key=lambda x: x[0], reverse=True)
        final_limit = limit if limit is not None else 15
        song_results = [result for score, result in scored_results[:final_limit]]
        
        logger.info(f"Found {len(song_results)} high-quality songs")

        # Prepare response
        response = {
            "categories": {"songs": song_results},
            "cached": False,
            "total": len(song_results)
        }

        # Cache results
        cache[cache_key] = (response, time.time())
        logger.info(f"✅ Search completed successfully with {response['total']} songs")
        return response
    
    except Exception as e:
        logger.error(f"❌ Search error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")

@app.get("/playlist/{playlist_id}")
async def get_playlist(playlist_id: str):
    """Get playlist details and tracks"""
    try:
        # Remove VL prefix if present
        if playlist_id.startswith("VL"):
            playlist_id = playlist_id[2:]
        
        # Check cache
        if playlist_id in playlist_cache:
            playlist_data, timestamp = playlist_cache[playlist_id]
            if time.time() - timestamp < PLAYLIST_CACHE_DURATION:
                logger.info(f"💾 Cache hit for playlist: {playlist_id}")
                return playlist_data
        
        logger.info(f"🎵 Fetching playlist: {playlist_id}")
        playlist_data = ytmusic.get_playlist(playlist_id, limit=None)
        
        # Cache the result
        playlist_cache[playlist_id] = (playlist_data, time.time())
        
        return playlist_data
    except Exception as e:
        logger.error(f"Failed to get playlist {playlist_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/playlist/{playlist_id}/refresh")
async def refresh_playlist(playlist_id: str):
    """Force refresh a playlist's cache"""
    try:
        # Remove VL prefix if present
        if playlist_id.startswith("VL"):
            playlist_id = playlist_id[2:]
        
        # Remove from cache to force refresh
        if playlist_id in playlist_cache:
            del playlist_cache[playlist_id]
        
        # Fetch fresh data
        playlist_data = ytmusic.get_playlist(playlist_id, limit=None)
        
        # Update cache
        playlist_cache[playlist_id] = (playlist_data, time.time())
        
        return {"status": "success", "message": "Playlist refreshed"}
    except Exception as e:
        logger.error(f"Failed to refresh playlist {playlist_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/playlist/{playlist_id}/first-song")
async def get_first_playable_song(playlist_id: str):
    """Get the first playable song from a playlist"""
    try:
        # Remove VL prefix if present
        if playlist_id.startswith("VL"):
            playlist_id = playlist_id[2:]
        
        logger.info(f"🎵 Getting first playable song for playlist: {playlist_id}")
        
        # Get playlist data
        playlist_data = ytmusic.get_playlist(playlist_id, limit=None)
        
        # Find first playable song
        for track in playlist_data.get('tracks', []):
            video_id = track.get('videoId')
            if video_id and not track.get('isAvailable', True):
                continue
            
            if video_id:
                logger.info(f"✅ Found first playable song: {video_id}")
                return {
                    "videoId": video_id,
                    "title": track.get('title', ''),
                    "artist": track.get('artists', [{}])[0].get('name', 'Unknown Artist')
                }
        
        raise HTTPException(status_code=404, detail="No playable songs found in playlist")
        
    except Exception as e:
        logger.error(f"Failed to get first playable song from playlist {playlist_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.on_event("startup")
async def startup_event():
    environment = os.getenv("ENVIRONMENT", "development")
    port = int(os.getenv("PORT", 8000))
    ws_port = int(os.getenv("WS_PORT", 8089))
    
    logger.info(f"Starting DropBeat Music API in {environment} mode")
    logger.info(f"HTTP server running on port {port}")
    logger.info(f"WebSocket server running on port {ws_port}")
    
    try:
        ytmusic.search("test", limit=1)
        logger.info("YTMusic connection successful")
    except Exception as e:
        logger.error(f"YTMusic connection failed: {e}")
        raise
    
    # Start the ping loop
    asyncio.create_task(manager.start_ping_loop())

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000) 