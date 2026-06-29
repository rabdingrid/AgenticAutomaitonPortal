from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext

SECRET_KEY = os.getenv("JWT_SECRET", "change-me-in-production-use-a-long-random-string")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 480

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")

USERS_FILE = os.path.join(os.path.dirname(__file__), "users.json")


def load_users() -> dict[str, Any]:
    if not os.path.exists(USERS_FILE):
        return {}
    with open(USERS_FILE, encoding="utf-8") as f:
        return json.load(f)


def get_user_by_email(email: str) -> Optional[tuple[str, dict[str, Any]]]:
    """Return (account_key, user_record) for a matching email (case-insensitive)."""
    normalized = email.strip().lower()
    for account_key, user in load_users().items():
        if user.get("email", "").strip().lower() == normalized:
            return account_key, user
    return None


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def authenticate_user(email: str, password: str) -> Optional[dict[str, Any]]:
    found = get_user_by_email(email)
    if not found:
        return None
    account_key, user = found
    if not verify_password(password, user["hashed_password"]):
        return None
    return {
        **user,
        "account_key": account_key,
        "email": user.get("email", email).strip().lower(),
    }


def create_access_token(data: dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


async def get_current_user(token: str = Depends(oauth2_scheme)) -> dict[str, Any]:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str | None = payload.get("sub")
        if email is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    found = get_user_by_email(email)
    if found is None:
        raise credentials_exception

    account_key, user = found
    return {
        "email": user.get("email", email).strip().lower(),
        "display_name": user.get("display_name", email),
        "role": user.get("role", "developer"),
        "account_key": account_key,
    }
