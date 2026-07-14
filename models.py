import datetime
from sqlalchemy import Column, Integer, String, DateTime, Text, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from database import Base

class User(Base):
    __tablename__ = "users"

    user_id = Column(Integer, primary_key=True, index=True)
    github_id = Column(String(100), nullable=False, default="")
    username = Column(String(50), nullable=False)
    email = Column(String(255), nullable=False)
    reputation_points = Column(Integer, default=5)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    onboarded = Column(Boolean, default=False)
    role = Column(String(20), default="both")
    tagline = Column(String(255), default="")
    github_username = Column(String(100), default="")
    is_verified = Column(Boolean, default=False)

    # Relationships
    seeker_sessions = relationship("Session", foreign_keys="Session.seeker_id", back_populates="seeker")
    helper_sessions = relationship("Session", foreign_keys="Session.helper_id", back_populates="helper")
    messages = relationship("Message", back_populates="sender")
    tags = relationship("UserTag", back_populates="user")

class Tag(Base):
    __tablename__ = "tags"

    tag_id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50), nullable=False)
    category = Column(String(50), nullable=False)

    # Relationships
    sessions = relationship("Session", back_populates="tag")
    users = relationship("UserTag", back_populates="tag")

class UserTag(Base):
    __tablename__ = "user_tags"

    user_id = Column(Integer, ForeignKey("users.user_id"), primary_key=True)
    tag_id = Column(Integer, ForeignKey("tags.tag_id"), primary_key=True)

    # Relationships
    user = relationship("User", back_populates="tags")
    tag = relationship("Tag", back_populates="users")

class Session(Base):
    __tablename__ = "sessions"

    session_id = Column(Integer, primary_key=True, index=True)
    seeker_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    helper_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    tag_id = Column(Integer, ForeignKey("tags.tag_id"), nullable=True)
    problem_description = Column(Text, nullable=False)
    status = Column(String(20), default="pending")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # Relationships
    seeker = relationship("User", foreign_keys=[seeker_id], back_populates="seeker_sessions")
    helper = relationship("User", foreign_keys=[helper_id], back_populates="helper_sessions")
    tag = relationship("Tag", back_populates="sessions")
    messages = relationship("Message", back_populates="session", cascade="all, delete-orphan")
    recipe = relationship("Recipe", uselist=False, back_populates="session", cascade="all, delete-orphan")

class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("sessions.session_id"), nullable=False)
    sender_id = Column(Integer, ForeignKey("users.user_id"), nullable=False)
    content = Column(Text, nullable=False)
    type = Column(String, default="text")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # Relationships
    session = relationship("Session", back_populates="messages")
    sender = relationship("User", back_populates="messages")

class Recipe(Base):
    __tablename__ = "recipes"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("sessions.session_id"), unique=True, nullable=False)
    title = Column(String, nullable=False)
    problem = Column(Text, nullable=False)
    solution = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # Relationships
    session = relationship("Session", back_populates="recipe")
