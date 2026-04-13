from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_admin
from app.core.database import get_db
from app.core.security import get_password_hash
from app.models.user import User
from app.schemas.user import UserCreate, UserRead

router = APIRouter()


@router.get("/users", response_model=list[UserRead])
def list_users(
    admin_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[User]:
    del admin_user
    return list(db.scalars(select(User).order_by(User.email)).all())


@router.post("/users", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    admin_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> User:
    del admin_user
    email = str(payload.email)
    if db.get(User, email):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="User already exists")

    user = User(
        email=email,
        full_name=payload.full_name,
        password_hash=get_password_hash(payload.password),
        role=payload.role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user
