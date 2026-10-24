from fastapi import HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from argon2 import PasswordHasher
from app.config import settings
from app.db import get_db
from app.models import User

ph = PasswordHasher()
signer = URLSafeTimedSerializer(settings.adaptiveai_secret_key, salt='adaptiveai-auth-v1')
bearer = HTTPBearer(auto_error=False)


def token_for(user):
    return signer.dumps({'user_id': user.id})


def current_user(auth: HTTPAuthorizationCredentials | None = Depends(bearer), db: Session = Depends(get_db)):
    if not auth:
        raise HTTPException(401, 'Please log in')
    try:
        user_id = signer.loads(auth.credentials, max_age=60*60*24*7)['user_id']
    except (BadSignature, SignatureExpired, KeyError, TypeError):
        raise HTTPException(401, 'Invalid or expired session') from None
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(401, 'Account not found')
    return user


def owned(db, model, item_id, user_id):
    item = db.get(model, item_id)
    if not item or item.owner_id != user_id:
        raise HTTPException(404, 'Not found')
    return item
