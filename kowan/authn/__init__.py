"""kowan.authn — external authentication (single sign-on).

* :mod:`kowan.authn.jose`      JWS parsing, JWK -> public key, RS/PS/ES/EdDSA
                              signature verification (on ``cryptography``).
* :mod:`kowan.authn.oidc`      OpenID Connect client: discovery, JWKS cache with
                              key rotation, Authorization Code + PKCE, ID token
                              validation, userinfo.
* :mod:`kowan.authn.github`    GitHub OAuth2 (not OIDC) mapped onto OIDC-like
                              claims through the GitHub user API.
* :mod:`kowan.authn.mapping`   claim -> role / team mapping.
* :mod:`kowan.authn.provision` just-in-time provisioning and account linking.
* :mod:`kowan.authn.state`     the sealed transient login-state cookie.

No authlib / pyjwt: everything is built on httpx + cryptography. See docs/sso.md.
"""
