# Login setup

Add a `[adapters.<name>.login]` table for each testbed users log in through. Set its `CC_OIDC_<NAME>_ISSUER`, `CC_OIDC_<NAME>_CLIENT_ID` and `CC_OIDC_<NAME>_CLIENT_SECRET`, with the testbed's name in capitals.

| Field | Default | Sets |
|---|---|---|
| `username_claim` | `preferred_username` | the userinfo claim with the user's name |
| `projects_claim` | `projects` | the userinfo claim with the user's projects |
| `email_claim` | `email` | the userinfo claim with the user's email. The client must grant the `email` scope. |

Generate `CC_SECRET_KEY` once and keep it.

```bash
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Register `<CC_PUBLIC_URL>/api/auth/callback` as a redirect URI on the Keycloak client. Under `npm run dev`, set `CC_PUBLIC_URL` to Vite's address.
