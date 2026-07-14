import sys
from fastapi.testclient import TestClient
from main import app, get_db, seed_database
import models
from database import SessionLocal, Base, engine
from auth import create_access_token

# Force recreate tables for test
Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)
seed_database()

client = TestClient(app)

def test_swap_flow():
    # 1. Setup mock database users and tags (already seeded on startup, but let's confirm)
    db = SessionLocal()
    seeker = db.query(models.User).filter_by(username="seeker_dev").first()
    helper = db.query(models.User).filter_by(username="helper_react").first()
    tag = db.query(models.Tag).filter_by(name="React").first()
    
    print(f"Seeker: {seeker.username} (Reputation: {seeker.reputation_points})")
    print(f"Helper: {helper.username} (Reputation: {helper.reputation_points})")
    print(f"Tag: {tag.name}")
    
    # Generate JWT Tokens
    seeker_token = create_access_token({"sub": str(seeker.user_id), "username": seeker.username, "email": seeker.email})
    helper_token = create_access_token({"sub": str(helper.user_id), "username": helper.username, "email": helper.email})
    
    seeker_headers = {"Authorization": f"Bearer {seeker_token}"}
    helper_headers = {"Authorization": f"Bearer {helper_token}"}
    
    # 2. Open presence WebSocket for helper
    print("\n--- Step 1: Connecting Helper Presence WebSocket ---")
    with client.websocket_connect(f"/ws/presence/{helper.user_id}?token={helper_token}") as helper_ws:
        print("Helper presence WebSocket connected.")
        
        # 3. Seeker creates a help request
        print("\n--- Step 2: Seeker posting a help request ---")
        response = client.post(
            "/sessions/", 
            json={
                "seeker_id": seeker.user_id,
                "error_log": "React hook useEffect has a missing dependency array causing infinite rerender loop.",
                "tag_name": "React"
            },
            headers=seeker_headers
        )
        assert response.status_code == 200, f"Failed: {response.text}"
        session_data = response.json()
        session_id = session_data["session_id"]
        print(f"Help request created! Session ID: {session_id}, Tag ID assigned: {session_data['tag_id']}")
        
        # 4. Helper should receive match_request notification
        print("\n--- Step 3: Verifying helper receives match alert ---")
        notification = helper_ws.receive_json()
        print("Helper received WebSocket message:", notification)
        assert notification["type"] == "match_request"
        assert notification["session_id"] == session_id
        assert notification["tag_name"] == "React"
        
        # 5. Helper accepts the matchmaking request
        print("\n--- Step 4: Helper accepting the request ---")
        accept_resp = client.post(
            f"/sessions/{session_id}/accept",
            headers=helper_headers
        )
        assert accept_resp.status_code == 200, f"Failed: {accept_resp.text}"
        print("Session accepted! Response status:", accept_resp.json()["status"])
        
        # Helper websocket should receive match_accepted redirect notification
        accept_notification = helper_ws.receive_json()
        print("Helper presence WS received:", accept_notification)
        assert accept_notification["type"] == "match_accepted"
        
        # 6. Connecting to Chat Room WebSocket
        print("\n--- Step 5: Seeker and Helper joining chat room ---")
        with client.websocket_connect(f"/ws/chat/{session_id}/{seeker.user_id}?token={seeker_token}") as seeker_chat_ws:
            with client.websocket_connect(f"/ws/chat/{session_id}/{helper.user_id}?token={helper_token}") as helper_chat_ws:
                # Both receive chat history (history message is sent on connection)
                seeker_history = seeker_chat_ws.receive_json()
                helper_history = helper_chat_ws.receive_json()
                print("Seeker received history message type:", seeker_history["type"])
                print("Helper received history message type:", helper_history["type"])
                
                # Seeker sends chat message
                print("\n--- Step 6: Seeker sends chat message ---")
                seeker_chat_ws.send_json({
                    "type": "message",
                    "content": "Hey! Thanks for joining. My useEffect is loop rerendering.",
                    "msg_type": "text"
                })
                
                # Helper should receive the seeker's message
                helper_received = helper_chat_ws.receive_json()
                print("Helper received chat message:", helper_received)
                assert helper_received["content"] == "Hey! Thanks for joining. My useEffect is loop rerendering."
                assert helper_received["sender_name"] == "seeker_dev"
                
                # Helper sends code updates
                print("\n--- Step 7: Helper sends code updates ---")
                helper_chat_ws.send_json({
                    "type": "code_sync",
                    "content": "useEffect(() => {\n  fetchData();\n}, []); // <-- Add empty array here"
                })
                
                # Seeker should receive the code update
                seeker_received = seeker_chat_ws.receive_json()
                print("Seeker received code update:", seeker_received)
                assert seeker_received["type"] == "code_sync"
                assert "fetchData" in seeker_received["content"]
        
        # 7. Seeker resolves the session
        print("\n--- Step 8: Seeker resolving session and updating reputation ---")
        resolve_resp = client.post(
            f"/sessions/{session_id}/resolve",
            headers=seeker_headers
        )
        assert resolve_resp.status_code == 200, f"Failed: {resolve_resp.text}"
        print("Session resolved successfully!")
        
        # Check reputation score update
        db.close()
        db = SessionLocal()
        seeker_updated = db.query(models.User).filter_by(username="seeker_dev").first()
        helper_updated = db.query(models.User).filter_by(username="helper_react").first()
        print(f"Seeker reputation points: {seeker_updated.reputation_points} (expected: 4)")
        print(f"Helper reputation points: {helper_updated.reputation_points} (expected: 6)")
        assert seeker_updated.reputation_points == 4
        assert helper_updated.reputation_points == 6
        
        # Helper skill linkage verified
        helper_skill = db.query(models.UserTag).filter_by(user_id=helper.user_id, tag_id=tag.tag_id).first()
        assert helper_skill is not None
        print("Helper skill linkage verified successfully!")
        
        print("\n=== SUCCESS: Real-Time Matching & Swap flow verified! ===")

if __name__ == "__main__":
    test_swap_flow()
