from datetime import timedelta

from fastapi import APIRouter, HTTPException, status

from app.api.deps import SessionDep
from app.core.config import settings
from app.core.email import send_password_reset_email
from app.core.security import (
    create_access_token,
    verify_frontend_auth_token,
    verify_google_access_token,
    verify_google_identity_token,
)
from app.crud import (
    authenticate_user_with_password,
    confirm_password_reset,
    create_password_reset_request,
    create_user_with_password,
    get_or_create_user_from_api_key,
    upsert_user_from_identity,
)
from app.models import (
    AuthSessionCreate,
    EmailPasswordSessionCreate,
    GoogleAuthSessionCreate,
    PasswordResetConfirmCreate,
    PasswordResetRequestCreate,
    PasswordResetRequestPublic,
    Token,
    UserCreate,
    UserRegistrationCreate,
)

router = APIRouter()


def create_user_token(user_id) -> Token:
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return Token(
        access_token=create_access_token(user_id, expires_delta=access_token_expires),
        token_type="bearer",
    )


@router.post("/register", response_model=Token)
def register_with_password(
    session: SessionDep,
    user_in: UserRegistrationCreate,
) -> Token:
    user = create_user_with_password(session=session, user_create=user_in)
    return create_user_token(user.id)


@router.post("/login", response_model=Token)
def create_email_password_session(
    session: SessionDep,
    session_in: EmailPasswordSessionCreate,
) -> Token:
    user = authenticate_user_with_password(
        session=session,
        email=session_in.email,
        password=session_in.password,
    )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    return create_user_token(user.id)


@router.post("/password-reset/request", response_model=PasswordResetRequestPublic)
def request_password_reset(
    session: SessionDep,
    reset_request: PasswordResetRequestCreate,
) -> PasswordResetRequestPublic:
    reset_token = create_password_reset_request(session=session, reset_request=reset_request)
    email_sent = bool(
        reset_token and send_password_reset_email(str(reset_request.email), reset_token)
    )
    return PasswordResetRequestPublic(
        status="ok",
        email_sent=email_sent,
    )


@router.post("/password-reset/confirm", response_model=Token)
def reset_password(
    session: SessionDep,
    reset_confirm: PasswordResetConfirmCreate,
) -> Token:
    user = confirm_password_reset(session=session, reset_confirm=reset_confirm)
    return create_user_token(user.id)


@router.post("/session", response_model=Token)
def create_session(
    session: SessionDep,
    session_in: AuthSessionCreate,
) -> Token:
    if not verify_frontend_auth_token(session_in.auth_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid auth token",
        )

    user_in = UserCreate.model_validate(session_in.model_dump(exclude={"auth_token"}))
    user = get_or_create_user_from_api_key(session=session, user_create=user_in)
    return create_user_token(user.id)


@router.post("/google", response_model=Token)
def create_google_session(
    session: SessionDep,
    session_in: GoogleAuthSessionCreate,
) -> Token:
    user_in = (
        verify_google_identity_token(session_in.credential)
        if session_in.credential
        else None
    )
    if not user_in and session_in.access_token:
        user_in = verify_google_access_token(session_in.access_token)

    if not user_in:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Google credential",
        )

    user = upsert_user_from_identity(session=session, user_create=user_in)
    return create_user_token(user.id)
