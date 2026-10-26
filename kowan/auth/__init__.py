"""kowan.auth — password hashing, JWT issuance/validation, API keys, and the
aiohttp middleware used to authenticate and authorize requests to the control
plane API.

Roles are ``admin`` / ``operator`` / ``viewer``.
"""

from .password import hash_password, verify_password
from .jwt import JWT, Claims, new_jwt
from .apikey import (
    APIKeyVerifier,
    new_api_key_verifier,
    generate_api_key_secret,
    hash_api_key_secret,
    API_KEY_SECRET_PREFIX,
    API_KEY_PREFIX_LEN,
)
from .middleware import (
    CtxUser,
    auth_middleware,
    require_role,
    user_from,
    REQUEST_USER_KEY,
)

__all__ = [
    # password
    "hash_password",
    "verify_password",
    # jwt
    "JWT",
    "Claims",
    "new_jwt",
    # apikey
    "APIKeyVerifier",
    "new_api_key_verifier",
    "generate_api_key_secret",
    "hash_api_key_secret",
    "API_KEY_SECRET_PREFIX",
    "API_KEY_PREFIX_LEN",
    # middleware
    "CtxUser",
    "auth_middleware",
    "require_role",
    "user_from",
    "REQUEST_USER_KEY",
]
