"""FastAPI dependencies for DI."""
from typing import Generator

from fastapi import Depends, Request, Header, Cookie, HTTPException, status
from sqlalchemy.orm import Session

from .auth import Principal, verify_service_key, decode_session_cookie
from .config import Settings
from .orm import User


def get_settings(request: Request) -> Settings:
    """Get settings from app state."""
    return request.app.state.settings


def get_db(request: Request) -> Generator[Session, None, None]:
    """Get database session from app state."""
    session_factory = request.app.state.session_factory
    db = session_factory()
    try:
        yield db
    finally:
        db.close()


def get_read_db(request: Request) -> Generator[Session, None, None]:
    """Database session for handlers that only read (deferred transactions, never hold the write lock)."""
    db = request.app.state.read_session_factory()
    try:
        yield db
    finally:
        db.close()


def get_principal(
    request: Request,
    x_service_key: str | None = Header(None, alias="X-Service-Key"),
    resolvix_session: str | None = Cookie(None),
) -> Principal:
    """Extract and verify authentication (service key or session cookie)."""
    settings = get_settings(request)
    
    # Try service key first
    if verify_service_key(x_service_key, settings):
        return Principal(kind="service")
    
    # Try session cookie
    if resolvix_session:
        session_data = decode_session_cookie(resolvix_session, settings)
        if session_data:
            # the database is the truth: role and department are re-read, and a logged-out cookie is rejected.
            # A short session of its own, closed here: authenticating must never keep a transaction (or the write
            # lock) open for the rest of the request, e.g. while publish waits for the voice runtime.
            with request.app.state.read_session_factory() as db:
                user = db.query(User).filter(User.email == session_data.get("email")).first()
            if user is not None and session_data.get("sv") == user.session_version:
                return Principal(kind="user", email=user.email, role=user.role, department=user.department)
    
    # No valid auth
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required"
    )


def system_principal(principal: Principal = Depends(get_principal)) -> Principal:
    """For the endpoints the voice runtime (A) calls: the service key, or an admin session. A staff session gets 403."""
    if principal.kind == "service" or (principal.kind == "user" and principal.role == "admin"):
        return principal
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This endpoint is for the voice runtime or an admin")


def actor_of(principal: Principal) -> str:
    """Audit/event actor: the user's email, or "service" for the X-Service-Key principal."""
    if principal.kind == "user" and principal.email:
        return principal.email
    return "service"


def require_admin(principal: Principal = None) -> Principal:
    """Require admin role. Use as Depends(require_admin) which chains get_principal."""
    from fastapi import Depends
    
    # This is called with principal from get_principal via Depends chain
    if principal is None:
        raise HTTPException(status_code=500, detail="Principal not injected")
    
    if principal.kind != "user" or principal.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    return principal
