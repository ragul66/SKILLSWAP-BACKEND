import os
import json
import datetime
from typing import List, Optional
from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect, BackgroundTasks, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database import engine, Base, get_db, SessionLocal
import models
from presence import presence_manager
from gemini_service import classify_error_to_tag, generate_bug_fix_recipe
from auth import (
    create_access_token, 
    get_current_user, 
    verify_token,
    exchange_google_code, 
    exchange_github_code,
    GOOGLE_CLIENT_ID, 
    GITHUB_CLIENT_ID,
    FRONTEND_URL, 
    BACKEND_URL
)

# Initialize FastAPI App
app = FastAPI(title="Real-Time Developer Skill-Swap Platform API")

# Configure CORS (Support local frontend port 5173 and 5174)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL, "http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:5174", "http://127.0.0.1:5174", "http://localhost:5175", "http://127.0.0.1:5175"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Create Database tables on startup
Base.metadata.create_all(bind=engine)

# Seed Initial Data (Tags and Dev Users for matching tests)
def seed_database():
    db = SessionLocal()
    try:
        # 0. Ensure user columns exist (runs ALTER TABLE to support existing databases)
        from sqlalchemy import text
        for query in [
            "ALTER TABLE users ADD COLUMN onboarded BOOLEAN DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN role VARCHAR(20) DEFAULT 'both'",
            "ALTER TABLE users ADD COLUMN tagline VARCHAR(255) DEFAULT ''",
            "ALTER TABLE users ADD COLUMN github_username VARCHAR(100) DEFAULT ''",
            "ALTER TABLE users ADD COLUMN is_verified BOOLEAN DEFAULT FALSE"
        ]:
            try:
                db.execute(text(query))
                db.commit()
            except Exception:
                db.rollback()

        # 1. Seed Tags
        tags_to_seed = [
            {"name": "React", "category": "Frontend"},
            {"name": "Python", "category": "Backend"},
            {"name": "PostgreSQL", "category": "Database"},
            {"name": "FastAPI", "category": "Backend"},
            {"name": "Docker", "category": "DevOps"},
            {"name": "General", "category": "General"}
        ]
        db_tags = []
        for tag_data in tags_to_seed:
            existing_tag = db.query(models.Tag).filter(models.Tag.name == tag_data["name"]).first()
            if not existing_tag:
                tag = models.Tag(**tag_data)
                db.add(tag)
                db_tags.append(tag)
            else:
                db_tags.append(existing_tag)
        db.commit()

        # 2. Seed Users
        users_to_seed = [
            {"username": "seeker_dev", "email": "seeker@skillswap.com", "reputation_points": 5, "github_id": "mock_seeker_dev"},
            {"username": "helper_python", "email": "python_expert@skillswap.com", "reputation_points": 5, "github_id": "mock_helper_python"},
            {"username": "helper_react", "email": "react_expert@skillswap.com", "reputation_points": 5, "github_id": "mock_helper_react"}
        ]
        db_users = {}
        for user_data in users_to_seed:
            existing_user = db.query(models.User).filter(models.User.username == user_data["username"]).first()
            if not existing_user:
                user = models.User(**user_data)
                db.add(user)
                db.flush()
                db_users[user.username] = user
            else:
                db_users[existing_user.username] = existing_user
        db.commit()

        # 3. Seed User Tags (Specialized skills for helpers)
        tag_map = {tag.name: tag.tag_id for tag in db_tags}
        
        helper_py = db_users.get("helper_python")
        if helper_py:
            for t_name in ["Python", "FastAPI"]:
                t_id = tag_map.get(t_name)
                if t_id and not db.query(models.UserTag).filter_by(user_id=helper_py.user_id, tag_id=t_id).first():
                    db.add(models.UserTag(user_id=helper_py.user_id, tag_id=t_id))

        helper_re = db_users.get("helper_react")
        if helper_re:
            t_id = tag_map.get("React")
            if t_id and not db.query(models.UserTag).filter_by(user_id=helper_re.user_id, tag_id=t_id).first():
                db.add(models.UserTag(user_id=helper_re.user_id, tag_id=t_id))
                
        db.commit()
    except Exception as e:
        print(f"Error seeding data: {e}")
        db.rollback()
    finally:
        db.close()

seed_database()

# Pydantic Schemas
class MockLoginRequest(BaseModel):
    username: str
    email: str

class SessionCreate(BaseModel):
    error_log: str
    tag_name: Optional[str] = None

class SessionResponse(BaseModel):
    session_id: int
    seeker_id: int
    helper_id: Optional[int] = None
    tag_id: int
    problem_description: str
    status: str
    created_at: datetime.datetime
    class Config:
        from_attributes = True

class UserResponse(BaseModel):
    user_id: int
    username: str
    email: str
    reputation_points: int
    created_at: datetime.datetime
    onboarded: Optional[bool] = False
    role: Optional[str] = "both"
    tagline: Optional[str] = ""
    github_username: Optional[str] = ""
    is_verified: Optional[bool] = False
    class Config:
        from_attributes = True

class TagResponse(BaseModel):
    tag_id: int
    name: str
    category: str
    class Config:
        from_attributes = True

class RecipeResponse(BaseModel):
    id: int
    session_id: int
    title: str
    problem: str
    solution: str
    created_at: datetime.datetime
    class Config:
        from_attributes = True


# --- Authentication & OAuth Routes ---

@app.post("/auth/mock-login")
def mock_login(req: MockLoginRequest, db: Session = Depends(get_db)):
    """Developer helper route to login instantly without registering actual OAuth credentials."""
    user = db.query(models.User).filter(
        (models.User.username == req.username) | (models.User.email == req.email)
    ).first()
    
    if not user:
        # Enforce non-nullable github_id
        github_id = f"mock_{req.username.lower()}"
        user = models.User(username=req.username, email=req.email, reputation_points=5, github_id=github_id)
        db.add(user)
        db.commit()
        db.refresh(user)
        
    token = create_access_token({"sub": str(user.user_id), "username": user.username, "email": user.email})
    return {"access_token": token, "token_type": "bearer", "user": user}

@app.get("/auth/login/google")
def login_google():
    """Redirects the client to Google's OAuth consent screen."""
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=500, detail="Google OAuth Client ID is not configured on Backend.")
        
    redirect_uri = f"{BACKEND_URL}/auth/callback/google"
    google_url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={redirect_uri}"
        "&response_type=code"
        "&scope=openid%20profile%20email"
    )
    return RedirectResponse(google_url)

@app.get("/auth/callback/google")
async def callback_google(code: str, db: Session = Depends(get_db)):
    """Handles Google's auth callback redirect, registers user, and redirects to frontend with JWT."""
    try:
        user_info = await exchange_google_code(code)
        email = user_info.get("email")
        name = user_info.get("name", email.split("@")[0])
        
        # Check or create user
        user = db.query(models.User).filter(models.User.email == email).first()
        if not user:
            # Enforce unique username
            username = name.replace(" ", "_").lower()
            existing_user = db.query(models.User).filter(models.User.username == username).first()
            if existing_user:
                username = f"{username}_{int(datetime.datetime.utcnow().timestamp())}"
            github_id = f"google_{username}"
            user = models.User(username=username, email=email, reputation_points=5, github_id=github_id)
            db.add(user)
            db.commit()
            db.refresh(user)
            
        token = create_access_token({"sub": str(user.user_id), "username": user.username, "email": user.email})
        return RedirectResponse(f"{FRONTEND_URL}/auth/callback?token={token}")
    except Exception as e:
        print(f"Google Callback Error: {e}")
        return RedirectResponse(f"{FRONTEND_URL}/login?error=google_auth_failed")

@app.get("/auth/login/github")
def login_github():
    """Redirects the client to GitHub's OAuth login screen."""
    if not GITHUB_CLIENT_ID:
        raise HTTPException(status_code=500, detail="GitHub OAuth Client ID is not configured on Backend.")
        
    redirect_uri = f"{BACKEND_URL}/auth/callback/github"
    github_url = (
        "https://github.com/login/oauth/authorize"
        f"?client_id={GITHUB_CLIENT_ID}"
        f"&redirect_uri={redirect_uri}"
        "&scope=user:email"
    )
    return RedirectResponse(github_url)

@app.get("/auth/callback/github")
async def callback_github(code: str, db: Session = Depends(get_db)):
    """Handles GitHub's auth callback redirect, registers user, and redirects to frontend with JWT."""
    try:
        user_info = await exchange_github_code(code)
        email = user_info.get("email")
        login = user_info.get("login")
        github_uid = str(user_info.get("id", f"gh_{login}"))
        
        # Check or create user
        user = db.query(models.User).filter(models.User.email == email).first()
        if not user:
            existing_user = db.query(models.User).filter(models.User.username == login).first()
            username = login
            if existing_user:
                username = f"{login}_{int(datetime.datetime.utcnow().timestamp())}"
            user = models.User(username=username, email=email, reputation_points=5, github_id=github_uid)
            db.add(user)
            db.commit()
            db.refresh(user)
            
        token = create_access_token({"sub": str(user.user_id), "username": user.username, "email": user.email})
        return RedirectResponse(f"{FRONTEND_URL}/auth/callback?token={token}")
    except Exception as e:
        print(f"GitHub Callback Error: {e}")
        return RedirectResponse(f"{FRONTEND_URL}/login?error=github_auth_failed")


# --- Core Skill-Swap API Routes ---

@app.get("/users/me", response_model=UserResponse)
def get_me(current_user: models.User = Depends(get_current_user)):
    """Returns the authenticated user details."""
    return current_user

@app.get("/users/", response_model=List[UserResponse])
def get_users(db: Session = Depends(get_db)):
    """Returns all users."""
    return db.query(models.User).all()

@app.get("/tags/", response_model=List[TagResponse])
def get_tags(db: Session = Depends(get_db)):
    """Returns all skill tags."""
    return db.query(models.Tag).all()

@app.post("/users/me/skills/")
def update_my_skills(tag_ids: List[int], current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Updates the skills of the authenticated user."""
    # Clear existing tags
    db.query(models.UserTag).filter(models.UserTag.user_id == current_user.user_id).delete()
    
    # Add new tags
    for tag_id in tag_ids:
        tag = db.query(models.Tag).filter(models.Tag.tag_id == tag_id).first()
        if tag:
            db.add(models.UserTag(user_id=current_user.user_id, tag_id=tag_id))
    db.commit()
    return {"message": "Skills updated successfully"}

@app.get("/users/me/skills/", response_model=List[TagResponse])
def get_my_skills(current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Gets the skills of the authenticated user."""
    user_tags = db.query(models.UserTag).filter(models.UserTag.user_id == current_user.user_id).all()
    tag_ids = [ut.tag_id for ut in user_tags]
    return db.query(models.Tag).filter(models.Tag.tag_id.in_(tag_ids)).all() if tag_ids else []

class ProfileUpdate(BaseModel):
    username: Optional[str] = None
    tagline: Optional[str] = None
    role: Optional[str] = None
    github_username: Optional[str] = None
    onboarded: Optional[bool] = None

@app.patch("/users/me/profile", response_model=UserResponse)
def update_profile(
    data: ProfileUpdate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Updates the authenticated user's profile information."""
    if data.username is not None:
        new_username = data.username.strip()
        if not new_username or len(new_username) < 3:
            raise HTTPException(status_code=400, detail="Username must be at least 3 characters.")
        existing = db.query(models.User).filter(
            models.User.username == new_username,
            models.User.user_id != current_user.user_id
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="Username already taken by another user.")
        current_user.username = new_username

    if data.tagline is not None:
        current_user.tagline = data.tagline.strip()

    if data.role is not None:
        if data.role in ["seeker", "helper", "both"]:
            current_user.role = data.role

    if data.github_username is not None:
        current_user.github_username = data.github_username.strip()

    if data.onboarded is not None:
        current_user.onboarded = data.onboarded

    db.commit()
    db.refresh(current_user)
    return current_user

@app.post("/users/me/verify", response_model=UserResponse)
def verify_profile(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Verifies the user profile using their linked GitHub profile.
    """
    if not current_user.github_username:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="A GitHub username must be linked to your profile to complete verification."
        )
    
    current_user.is_verified = True
    db.commit()
    db.refresh(current_user)
    return current_user

class UsernameUpdate(BaseModel):
    username: str

@app.patch("/users/me/username", response_model=UserResponse)
def update_username(
    data: UsernameUpdate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Updates the authenticated user's username."""
    new_username = data.username.strip()
    if not new_username or len(new_username) < 3:
        raise HTTPException(status_code=400, detail="Username must be at least 3 characters.")
    existing = db.query(models.User).filter(
        models.User.username == new_username,
        models.User.user_id != current_user.user_id
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Username already taken by another user.")
    current_user.username = new_username
    db.commit()
    db.refresh(current_user)
    return current_user


class SessionHistoryResponse(BaseModel):
    session_id: int
    seeker_id: int
    helper_id: Optional[int] = None
    tag_name: Optional[str] = None
    status: str
    created_at: datetime.datetime
    class Config:
        from_attributes = True


@app.get("/sessions/history", response_model=List[SessionHistoryResponse])
def get_session_history(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Returns all sessions where the user was either a seeker or a helper."""
    sessions = db.query(models.Session).filter(
        (models.Session.seeker_id == current_user.user_id) |
        (models.Session.helper_id == current_user.user_id)
    ).order_by(models.Session.created_at.desc()).all()
    result = []
    for s in sessions:
        tag = db.query(models.Tag).filter(models.Tag.tag_id == s.tag_id).first()
        result.append(SessionHistoryResponse(
            session_id=s.session_id,
            seeker_id=s.seeker_id,
            helper_id=s.helper_id,
            tag_name=tag.name if tag else "General",
            status=s.status,
            created_at=s.created_at
        ))
    return result


@app.post("/sessions/", response_model=SessionResponse)
async def create_session(
    session_data: SessionCreate, 
    current_user: models.User = Depends(get_current_user), 
    db: Session = Depends(get_db)
):
    """
    Creates a new help request session.
    Invokes Gemini to automatically classify error messages to tags if not provided.
    Pings online helpers with the matching tag via Websocket Presence.
    """
    if current_user.reputation_points <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="Insufficient reputation points. You need to help someone first to earn points!"
        )

    # Check for active seeker session
    existing_session = db.query(models.Session).filter(
        models.Session.seeker_id == current_user.user_id,
        models.Session.status.in_(["pending", "active"])
    ).first()
    if existing_session:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="You already have an open debugging session. Resolve or cancel it first."
        )

    # Resolve skill tag
    tags = db.query(models.Tag).all()
    tag_names = [t.name for t in tags]
    
    resolved_tag_name = session_data.tag_name
    if not resolved_tag_name or resolved_tag_name not in tag_names:
        resolved_tag_name = classify_error_to_tag(session_data.error_log, tag_names)
        
    tag = db.query(models.Tag).filter(models.Tag.name == resolved_tag_name).first()
    if not tag:
        tag = db.query(models.Tag).filter(models.Tag.name == "General").first()

    # Create session
    db_session = models.Session(
        seeker_id=current_user.user_id,
        tag_id=tag.tag_id,
        problem_description=session_data.error_log,
        status="pending"
    )
    db.add(db_session)
    db.commit()
    db.refresh(db_session)

    # Broadcast match alerts to online helpers
    await presence_manager.send_match_notification(
        session_id=db_session.session_id,
        seeker_id=current_user.user_id,
        seeker_username=current_user.username,
        tag_id=tag.tag_id,
        tag_name=tag.name,
        error_log=db_session.problem_description
    )
    
    return db_session

@app.get("/sessions/pending/", response_model=List[SessionResponse])
def get_pending_sessions(db: Session = Depends(get_db)):
    """Lists all pending help tickets."""
    return db.query(models.Session).filter(models.Session.status == "pending").all()

@app.post("/sessions/{session_id}/accept", response_model=SessionResponse)
async def accept_session(
    session_id: int, 
    current_user: models.User = Depends(get_current_user), 
    db: Session = Depends(get_db)
):
    """Called when an online helper accepts an incoming match request."""
    session = db.query(models.Session).filter(models.Session.session_id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
        
    if session.seeker_id == current_user.user_id:
        raise HTTPException(status_code=400, detail="You cannot accept your own matchmaking ticket.")
        
    if session.status != "pending":
        raise HTTPException(status_code=400, detail="This session is already active or completed.")

    # Accept match
    session.helper_id = current_user.user_id
    session.status = "active"
    db.commit()
    db.refresh(session)

    # Notify seeker and helper of successful matching
    notification = {
        "type": "match_accepted",
        "session_id": session.session_id,
        "helper_id": current_user.user_id,
        "helper_username": current_user.username,
        "seeker_id": session.seeker_id
    }
    
    await presence_manager.broadcast_chat_message(session.session_id, notification)
    
    # Notify seeker's presence channel to trigger frontend workspace redirect
    seeker_ws = presence_manager.presence_connections.get(session.seeker_id)
    if seeker_ws:
        try:
            await seeker_ws.send_json(notification)
        except Exception:
            pass

    return session

def process_recipe_generation(session_id: int, seeker_username: str, helper_username: str, error_log: str):
    """Background thread to process transcripts and compile the public Bug Recipe via Gemini."""
    db = SessionLocal()
    try:
        messages = db.query(models.Message).filter(models.Message.session_id == session_id).order_by(models.Message.created_at.asc()).all()
        chat_transcript = ""
        for msg in messages:
            sender = db.query(models.User).filter(models.User.user_id == msg.sender_id).first()
            username = sender.username if sender else "Unknown"
            chat_transcript += f"[{username}]: {msg.content}\n"
            
        recipe_data = generate_bug_fix_recipe(seeker_username, helper_username, error_log, chat_transcript)
        
        db_recipe = models.Recipe(
            session_id=session_id,
            title=recipe_data["title"],
            problem=recipe_data["problem"],
            solution=recipe_data["solution"]
        )
        db.add(db_recipe)
        db.commit()
        print(f"AI Bug-Fix Recipe successfully compiled for session {session_id}.")
    except Exception as e:
        print(f"Background recipe processor failed: {e}")
        db.rollback()
    finally:
        db.close()

@app.post("/sessions/{session_id}/resolve", response_model=SessionResponse)
async def resolve_session(
    session_id: int, 
    background_tasks: BackgroundTasks, 
    current_user: models.User = Depends(get_current_user), 
    db: Session = Depends(get_db)
):
    """Resolves an active session. Transfers points and launches AI recipe generation in background."""
    session = db.query(models.Session).filter(models.Session.session_id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
        
    if session.status != "active":
        raise HTTPException(status_code=400, detail="Only active sessions can be marked resolved.")
        
    if session.seeker_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="Only the seeker who opened the session can resolve it.")

    # 1. Update session status
    session.status = "completed"

    # 2. Transact Reputation Points
    seeker = db.query(models.User).filter(models.User.user_id == session.seeker_id).first()
    helper = db.query(models.User).filter(models.User.user_id == session.helper_id).first()
    
    if seeker and helper:
        seeker.reputation_points = max(0, seeker.reputation_points - 1)
        helper.reputation_points += 1
        
        # Link helper tag if not linked
        user_tag = db.query(models.UserTag).filter_by(user_id=helper.user_id, tag_id=session.tag_id).first()
        if not user_tag:
            db.add(models.UserTag(user_id=helper.user_id, tag_id=session.tag_id))
            
    db.commit()
    db.refresh(session)

    # 3. Inform clients in workspace
    await presence_manager.broadcast_chat_message(session.session_id, {"type": "session_resolved", "session_id": session.session_id})

    # 4. Trigger Gemini Documentation
    background_tasks.add_task(
        process_recipe_generation,
        session_id=session.session_id,
        seeker_username=seeker.username if seeker else "seeker",
        helper_username=helper.username if helper else "helper",
        error_log=session.problem_description
    )

    return session

@app.get("/sessions/active", response_model=Optional[SessionResponse])
def get_my_active_session(current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Retrieves the active ongoing session for the logged-in user, if any."""
    return db.query(models.Session).filter(
        (models.Session.seeker_id == current_user.user_id) | (models.Session.helper_id == current_user.user_id),
        models.Session.status == "active"
    ).first()

@app.get("/recipes/", response_model=List[RecipeResponse])
def get_recipes(db: Session = Depends(get_db)):
    """Returns the library of solved bug-fix recipes."""
    return db.query(models.Recipe).order_by(models.Recipe.created_at.desc()).all()


# --- Real-Time Secure WebSocket Handlers ---

@app.websocket("/ws/presence/{user_id}")
async def ws_presence(websocket: WebSocket, user_id: int, token: Optional[str] = None):
    """
    WebSocket for active helper presence matching.
    Verifies user's token query param, registers matching tag skills, and listens.
    """
    if not token:
        print(f"WS Presence Reject: User {user_id} - Token missing")
        await websocket.close(code=4001, reason="Authentication token missing")
        return
        
    payload = verify_token(token)
    if not payload:
        print(f"WS Presence Reject: User {user_id} - Token verify failed")
        await websocket.close(code=4002, reason="Authentication failed")
        return
        
    if str(payload.get("sub")) != str(user_id):
        print(f"WS Presence Reject: User {user_id} - sub mismatch (sub={payload.get('sub')}, user_id={user_id})")
        await websocket.close(code=4002, reason="Authentication failed")
        return

    with SessionLocal() as db:
        user_tags = db.query(models.UserTag).filter(models.UserTag.user_id == user_id).all()
        tag_ids = [ut.tag_id for ut in user_tags]
        presence_manager.register_user_skills(user_id, tag_ids)

    await presence_manager.connect_presence(user_id, websocket)
    try:
        while True:
            # Maintain ping-pong connection
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        presence_manager.disconnect_presence(user_id)

@app.websocket("/ws/chat/{session_id}/{user_id}")
async def ws_chat(websocket: WebSocket, session_id: int, user_id: int, token: Optional[str] = None):
    """
    WebSocket connection to exchange chat and code edits within matched debugging rooms.
    Sends full message history on successful socket handshake.
    """
    if not token:
        print(f"WS Chat Reject: User {user_id} - Token missing")
        await websocket.close(code=4001, reason="Authentication token missing")
        return
        
    payload = verify_token(token)
    if not payload:
        print(f"WS Chat Reject: User {user_id} - Token verify failed")
        await websocket.close(code=4002, reason="Authentication failed")
        return
        
    if str(payload.get("sub")) != str(user_id):
        print(f"WS Chat Reject: User {user_id} - sub mismatch (sub={payload.get('sub')}, user_id={user_id})")
        await websocket.close(code=4002, reason="Authentication failed")
        return

    await presence_manager.connect_chat(session_id, websocket)

    with SessionLocal() as db:
        user = db.query(models.User).filter(models.User.user_id == user_id).first()
        username = user.username if user else f"User {user_id}"

        # Send full chat history to newly connected client
        messages = db.query(models.Message).filter(models.Message.session_id == session_id).order_by(models.Message.created_at.asc()).all()
        history = []
        for msg in messages:
            sender = db.query(models.User).filter(models.User.user_id == msg.sender_id).first()
            sender_name = sender.username if sender else "Unknown"
            history.append({
                "type": "message",
                "sender_id": msg.sender_id,
                "sender_name": sender_name,
                "content": msg.content,
                "msg_type": msg.type,
                "created_at": msg.created_at.isoformat()
            })
        await websocket.send_json({"type": "history", "messages": history})

    try:
        while True:
            data = await websocket.receive_text()
            payload = json.loads(data)
            payload_type = payload.get("type")

            if payload_type == "message":
                content = payload.get("content")
                msg_type = payload.get("msg_type", "text")

                with SessionLocal() as db:
                    db_message = models.Message(
                        session_id=session_id,
                        sender_id=user_id,
                        content=content,
                        type=msg_type
                    )
                    db.add(db_message)
                    db.commit()
                    db.refresh(db_message)

                    broadcast_payload = {
                        "type": "message",
                        "sender_id": user_id,
                        "sender_name": username,
                        "content": content,
                        "msg_type": msg_type,
                        "created_at": db_message.created_at.isoformat()
                    }
                    await presence_manager.broadcast_chat_message(session_id, broadcast_payload, exclude_websocket=websocket)

            elif payload_type == "code_sync":
                content = payload.get("content")
                broadcast_payload = {
                    "type": "code_sync",
                    "sender_id": user_id,
                    "content": content
                }
                await presence_manager.broadcast_chat_message(session_id, broadcast_payload, exclude_websocket=websocket)

            elif payload_type == "voice_call":
                # Broadcast voice call signaling to peer in the room
                await presence_manager.broadcast_chat_message(session_id, payload, exclude_websocket=websocket)

    except WebSocketDisconnect:
        pass
    finally:
        await presence_manager.disconnect_chat(session_id, websocket)
