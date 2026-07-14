import json
import logging
from typing import Dict, List, Set
from fastapi import WebSocket

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("presence")

class PresenceManager:
    def __init__(self):
        # Maps user_id (int) -> Active WebSocket connection for presence/alerts
        self.presence_connections: Dict[int, WebSocket] = {}
        
        # Maps session_id (int) -> List of active WebSocket connections in that room
        self.chat_connections: Dict[int, List[WebSocket]] = {}
        
        # Maps user_id (int) -> Set of tag_ids (int) they can help with
        self.user_skills: Dict[int, Set[int]] = {}

    async def connect_presence(self, user_id: int, websocket: WebSocket):
        await websocket.accept()
        # Clean up any existing presence connection for this user
        if user_id in self.presence_connections:
            try:
                await self.presence_connections[user_id].close()
            except Exception:
                pass
        self.presence_connections[user_id] = websocket
        logger.info(f"User {user_id} connected to presence WebSocket.")

    def disconnect_presence(self, user_id: int):
        if user_id in self.presence_connections:
            del self.presence_connections[user_id]
        logger.info(f"User {user_id} disconnected from presence WebSocket.")

    def register_user_skills(self, user_id: int, tag_ids: List[int]):
        self.user_skills[user_id] = set(tag_ids)
        logger.info(f"Registered user {user_id} skills: {tag_ids}")

    def get_online_helpers_for_tag(self, tag_id: int, exclude_user_id: int) -> List[int]:
        """Finds all online helpers who specialize in tag_id, excluding the seeker."""
        matched_helpers = []
        for helper_id in self.presence_connections.keys():
            if helper_id == exclude_user_id:
                continue
            # If helper has registered skills and has this tag_id, add them
            if helper_id in self.user_skills and tag_id in self.user_skills[helper_id]:
                matched_helpers.append(helper_id)
            # If helper hasn't registered skills, we treat them as general helper (or exclude them). 
            # We'll default to matching them if they have the skill registered.
        return matched_helpers

    async def send_match_notification(
        self, 
        session_id: int, 
        seeker_id: int, 
        seeker_username: str, 
        tag_id: int, 
        tag_name: str, 
        error_log: str
    ) -> int:
        """Broadcasts matchmaking alert to all online helpers matching the tag."""
        helpers_to_notify = self.get_online_helpers_for_tag(tag_id, exclude_user_id=seeker_id)
        notified_count = 0
        
        payload = {
            "type": "match_request",
            "session_id": session_id,
            "seeker_id": seeker_id,
            "seeker_username": seeker_username,
            "tag_id": tag_id,
            "tag_name": tag_name,
            "error_log": error_log[:800]  # Avoid sending massive logs over WS initially
        }
        
        for helper_id in helpers_to_notify:
            ws = self.presence_connections.get(helper_id)
            if ws:
                try:
                    await ws.send_json(payload)
                    notified_count += 1
                except Exception as e:
                    logger.error(f"Failed to send matchmaking ping to user {helper_id}: {e}")
                    
        return notified_count

    # Chat Room Connection Handlers
    async def connect_chat(self, session_id: int, websocket: WebSocket):
        await websocket.accept()
        if session_id not in self.chat_connections:
            self.chat_connections[session_id] = []
        self.chat_connections[session_id].append(websocket)
        logger.info(f"WebSocket joined chat session {session_id}. Total active connections in room: {len(self.chat_connections[session_id])}")

    async def disconnect_chat(self, session_id: int, websocket: WebSocket):
        if session_id in self.chat_connections:
            if websocket in self.chat_connections[session_id]:
                self.chat_connections[session_id].remove(websocket)
            if not self.chat_connections[session_id]:
                del self.chat_connections[session_id]
                logger.info(f"Chat session {session_id} is now empty.")
            else:
                logger.info(f"WebSocket left chat session {session_id}. Connections remaining: {len(self.chat_connections[session_id])}")

    async def broadcast_chat_message(self, session_id: int, payload: dict, exclude_websocket: WebSocket = None):
        """Broadcasts messages, code edits, or system notifications to all users in a chat session."""
        if session_id in self.chat_connections:
            for ws in self.chat_connections[session_id]:
                if ws == exclude_websocket:
                    continue
                try:
                    await ws.send_json(payload)
                except Exception as e:
                    logger.error(f"Failed to broadcast WS packet in session {session_id}: {e}")

presence_manager = PresenceManager()
