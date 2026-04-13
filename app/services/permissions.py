from app.models.bug import Bug
from app.models.user import User


def can_view_bug(user: User, bug: Bug) -> bool:
    return user.role == "admin" or bug.reporter_id == user.email or bug.assignee_id == user.email


def can_update_bug(user: User, bug: Bug) -> bool:
    return can_view_bug(user, bug)


def can_assign_bug(user: User, bug: Bug) -> bool:
    return user.role == "admin" or bug.reporter_id == user.email


def can_close_bug(user: User, bug: Bug) -> bool:
    return can_update_bug(user, bug)


def can_reopen_bug(user: User, bug: Bug) -> bool:
    return can_update_bug(user, bug)
