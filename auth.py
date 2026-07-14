import os
import datetime
from typing import Optional
from jose import jwt, JWTError
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
import httpx
from dotenv import load_dotenv

load_dotenv()

from database import get_db
import models

# Environment Configuration
JWT_SECRET = os.getenv("JWT_SECRET", "skillswap-dev-secret-key-123456789")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 hours for development convenience

# OAuth Credentials (to be loaded from Render environment / local .env)
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "")

FRONTEND_URL = os.getenv("https://skillswap-sigma-self.vercel.app", "http://localhost:5173")
BACKEND_URL = os.getenv("https://skillswap-backend-tpd8.onrender.com", "http://localhost:8002")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/token", auto_error=False)

def create_access_token(data: dict, expires_delta: Optional[datetime.timedelta] = None) -> str:
    """Generates a secure JWT token for a user session."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.datetime.utcnow() + expires_delta
    else:
        expire = datetime.datetime.utcnow() + datetime.timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET, algorithm=ALGORITHM)
    return encoded_jwt

def verify_token(token: str) -> Optional[dict]:
    """Decodes and validates a JWT token."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        return payload
    except JWTError as e:
        print(f"JWT Verification Error: {e}, JWT_SECRET length: {len(JWT_SECRET)}")
        return None

def get_current_user(token: Optional[str] = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> models.User:
    """FastAPI dependency to retrieve the logged-in user from the Authorization header."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise credentials_exception
        
    payload = verify_token(token)
    if payload is None:
        raise credentials_exception
        
    try:
        user_id = int(payload.get("sub"))
    except (ValueError, TypeError):
        raise credentials_exception
        
    user = db.query(models.User).filter(models.User.user_id == user_id).first()
    if user is None:
        raise credentials_exception
    return user

async def exchange_google_code(code: str) -> dict:
    """Exchanges an OAuth code for user details from Google."""
    token_url = "https://oauth2.googleapis.com/token"
    data = {
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": f"{BACKEND_URL}/auth/callback/google"
    }
    
    async with httpx.AsyncClient() as client:
        # Request access token
        token_response = await client.post(token_url, data=data)
        if token_response.status_code != 200:
            raise HTTPException(status_code=400, detail=f"Google token exchange failed: {token_response.text}")
        
        tokens = token_response.json()
        access_token = tokens.get("access_token")
        
        # Request user profile details
        userinfo_url = "https://www.googleapis.com/oauth2/v3/userinfo"
        headers = {"Authorization": f"Bearer {access_token}"}
        userinfo_response = await client.get(userinfo_url, headers=headers)
        if userinfo_response.status_code != 200:
            raise HTTPException(status_code=400, detail="Google failed to retrieve user profile.")
            
        return userinfo_response.json()

async def exchange_github_code(code: str) -> dict:
    """Exchanges an OAuth code for user details from GitHub."""
    token_url = "https://github.com/login/oauth/access_token"
    headers = {"Accept": "application/json"}
    data = {
        "client_id": GITHUB_CLIENT_ID,
        "client_secret": GITHUB_CLIENT_SECRET,
        "code": code,
        "redirect_uri": f"{BACKEND_URL}/auth/callback/github"
    }
    
    async with httpx.AsyncClient() as client:
        # Request access token
        token_response = await client.post(token_url, headers=headers, data=data)
        if token_response.status_code != 200:
            raise HTTPException(status_code=400, detail=f"GitHub token exchange failed: {token_response.text}")
            
        tokens = token_response.json()
        access_token = tokens.get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail=f"GitHub did not return access token: {tokens}")
            
        # Request user details
        userinfo_url = "https://api.github.com/user"
        user_headers = {
            "Authorization": f"token {access_token}",
            "User-Agent": "SkillSwap-Backend"
        }
        userinfo_response = await client.get(userinfo_url, headers=user_headers)
        if userinfo_response.status_code != 200:
            raise HTTPException(status_code=400, detail="GitHub failed to retrieve user profile.")
            
        user_data = userinfo_response.json()
        
        # GitHub emails can be private; fetch emails separately if email is empty
        if not user_data.get("email"):
            email_url = "https://api.github.com/user/emails"
            email_response = await client.get(email_url, headers=user_headers)
            if email_response.status_code == 200:
                emails = email_response.json()
                primary_email = next((e["email"] for e in emails if e["primary"] and e["verified"]), None)
                if not primary_email and emails:
                    primary_email = emails[0]["email"]
                user_data["email"] = primary_email or f"{user_data['login']}@github-mock-email.com"
        
        return user_data
