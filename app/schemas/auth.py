from pydantic import BaseModel


class AdminLoginRequest(BaseModel):
    username: str
    password: str


class AdminLoginResponse(BaseModel):
    success: bool = True
    access_token: str
    token_type: str = "bearer"
    expires_in_minutes: int
    role: str
