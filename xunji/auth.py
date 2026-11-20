import os
from pathlib import Path

from fastmcp.server.auth.providers.github import GitHubProvider
from key_value.aio.stores.filetree import FileTreeStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper


def identity_allowed(token, login, user_id):
    return (
        token is not None
        and str(token.claims.get("sub", "")) == str(user_id)
        and str(token.claims.get("login", "")).casefold() == login.casefold()
    )


class OwnerGitHubProvider(GitHubProvider):
    def __init__(self, *, allowed_login, allowed_id, **kwargs):
        self.allowed_login, self.allowed_id = allowed_login, allowed_id
        super().__init__(**kwargs)

    async def load_access_token(self, token):
        validated = await super().load_access_token(token)
        return validated if identity_allowed(validated, self.allowed_login, self.allowed_id) else None


def required(name, minimum=1):
    value = os.environ.get(name, "")
    if len(value) < minimum:
        raise RuntimeError(f"Missing or too short environment setting: {name}")
    return value


def make_auth():
    directory = Path(required("DATA_DIR")) / "oauth"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = FileTreeStore(data_directory=directory)
    encrypted = FernetEncryptionWrapper(
        store, source_material=required("STORAGE_ENCRYPTION_SECRET", 48), salt="xunji-mcp-oauth-storage-v1"
    )
    provider = OwnerGitHubProvider(
        allowed_login=required("ALLOWED_GITHUB_LOGIN"),
        allowed_id=required("ALLOWED_GITHUB_ID"),
        client_id=required("GITHUB_CLIENT_ID"),
        client_secret=required("GITHUB_CLIENT_SECRET"),
        base_url=required("BASE_URL"),
        required_scopes=["read:user"],
        jwt_signing_key=required("JWT_SIGNING_KEY", 48),
        client_storage=encrypted,
        require_authorization_consent=True,
        cache_ttl_seconds=60,
    )
    return provider, store
