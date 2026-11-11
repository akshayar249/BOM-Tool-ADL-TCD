from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from backend.auth.store import SESSION_COOKIE, authenticate, create_session, delete_session

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


@router.post("/login")
def login(body: LoginRequest):
    user = authenticate(body.username, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="That username or password is not recognized.")
    token = create_session(user["id"])
    return {"token": token, "user": user}


@router.get("/me")
def me(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required.")
    return user


@router.post("/logout")
def logout(request: Request):
    delete_session(request.cookies.get(SESSION_COOKIE))
    return {"ok": True}
