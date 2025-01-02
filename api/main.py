from fastapi import FastAPI, WebSocket, HTTPException, WebSocketDisconnect
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

class ClientType(Enum):
    MACOS_APP = "macos_app"
    CHROME_EXTENSION = "chrome_extension"

@dataclass
class Client:
    websocket: WebSocket
    client_type: ClientType
    last_pong: float
    connected_at: datetime

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

app = FastAPI(title="DropBeat Music API")
manager = ConnectionManager()

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
                    query = data["query"]
                    logger.info(f"🔍 [{id(websocket)}] Processing search request: {query}")
                    results = await search(query)
                    await websocket.send_json({
                        "type": "SEARCH_RESULTS",
                        "data": results
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
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port) 