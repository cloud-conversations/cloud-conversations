# Credentials

`ccx` reads credentials from `clouds.yaml` or the environment. The chat page uses each user's login instead.

Run `uv run ccauth clouds-yaml --all-sites --project-id <project-id> --output ~/.config/openstack/clouds.yaml` to write `clouds.yaml`, then `ccx check --site <site>`, and approve the URL it prints. Put an application credential in an entry to skip the approval. Give each site its own entry, with the site name in `region_name`. Set `OS_CLOUD` to use one entry only.

Precedence, highest first:

| Source | Variables |
|---|---|
| `clouds.yaml` from [ccauth](https://github.com/ChameleonCloud/ccauth) | `OS_CLOUD` |
| Application credential | `OS_APPLICATION_CREDENTIAL_ID`, `OS_APPLICATION_CREDENTIAL_SECRET` |
| Password | `OS_USERNAME`, `OS_PASSWORD`, `OS_PROJECT_NAME`, `OS_PROJECT_DOMAIN_NAME`, `OS_USER_DOMAIN_NAME` |

The `OS_*` variables apply to every configured site. Unset every other `OS_*` variable.
