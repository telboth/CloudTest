from pydantic import BaseModel, EmailStr


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str
    password: str
    role: str


class UserRead(BaseModel):
    email: EmailStr
    full_name: str
    role: str

    model_config = {"from_attributes": True}
