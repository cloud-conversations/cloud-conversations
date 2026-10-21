# Cloud Conversations

Cloud Conversations builds experiment environments on a research testbed from a request in English, and answers questions from the testbed's documentation. The first release supports Chameleon, for both the testbed and its documentation. Use it from the `ccx` CLI or the chat page.

## Requirements

| For | Needed |
|---|---|
| Everything | Python 3.12+, [uv](https://docs.astral.sh/uv/), PostgreSQL with [pgvector](https://github.com/pgvector/pgvector) |
| Chameleon | The `integration` extra, [credentials](docs/credentials.md), `CC_SECRET_KEY` ([generate it](docs/login.md)), and a keypair at the site or a key imported with `ccx import-key` |
| `ccx describe`, `ccx build`, `ccx inventory`, `ccx ask`, the chat | A [model endpoint](#model-endpoint) |
| `ccx ask`, `ccx index` | The `knowledge` extra |
| The chat page | The `service` extra, Redis, Node 22 |

## Install

```bash
docker compose up -d db
uv sync --extra dev --extra integration --extra knowledge
uv run alembic upgrade head
```

Set `DATABASE_URL` to use another PostgreSQL server.

## Model endpoint

The endpoint must be OpenAI-compatible. Put `CC_LLM_BASE_URL` and `CC_LLM_API_KEY` in `.env`, and run commands with `uv run --env-file .env`. Set `llm_model` in `etc/config.toml`, or `CC_LLM_MODEL`, to pick the model.

## Running `ccx`

Without a testbed, on the in-memory adapter:

```bash
export CC_OWNER_ID=chameleon:alice
uv run ccx run --fixture baremetal --teardown
```

At Chameleon:

```bash
uv run ccauth clouds-yaml --all-sites --project-id <project-id> --output ~/.config/openstack/clouds.yaml
export OS_CLOUD=tacc CC_OWNER_ID=chameleon:alice CC_SECRET_KEY=<key>

uv run ccx check --site CHI@TACC --keypair <keypair-name> --node-type compute_zen3

uv run ccx describe-fixture --name baremetal --site CHI@TACC \
    --node-type compute_zen3 --hours 1

uv run ccx provision --description-id <printed-id> --keypair <keypair-name>
uv run ccx teardown --run-id <printed-run-id>
```

For a VM, pass `--name vm` and `--flavor <flavor>` in place of `--node-type`.

To describe from a prompt, and let the agent recover a failed build:

```bash
uv run --env-file .env ccx describe "a compute_zen3 node at CHI@TACC for an hour"
uv run --env-file .env ccx build --description-id <printed-id> --keypair <keypair-name>
```

- Release a failed run's resources with `ccx teardown`.

See [docs/cli.md](docs/cli.md) for every command and its flags.

## Docs chatbot and knowledge base

`ccx ask` and the chat answer questions from an index of Chameleon's documentation. Build the index before the first question, and rebuild it after changing a source or `embedding_model`.

```bash
uv run ccx index                    # fetch every source, rebuild
uv run ccx index --use-cache        # rebuild from the cache
uv run ccx index --refresh blog     # fetch one source
```

Set `enabled = false` under `[knowledge]` to turn it off. See [docs/knowledge-base.md](docs/knowledge-base.md) to add a source or tune the search.

## Running the chat page

Set these in `.env` first.

| Variable | Value |
|---|---|
| `CC_OIDC_CHAMELEON_ISSUER` | the Keycloak realm users log in through |
| `CC_OIDC_CHAMELEON_CLIENT_ID` | the Keycloak client |
| `CC_OIDC_CHAMELEON_CLIENT_SECRET` | that client's secret |
| `CC_PUBLIC_URL` | where the page is served |
| `CC_SECRET_KEY` | a Fernet key that encrypts refresh tokens and the system key |
| `CC_ADMINS` | comma-separated `<testbed>:<username>` entries that get the Admin page, as in `chameleon:alice`. Overrides each testbed's `admins`. Name a local test user `chameleon:local:<name>`. |

See [docs/login.md](docs/login.md) to generate `CC_SECRET_KEY`, register the Keycloak redirect URI, or add another testbed's login.

```bash
docker compose up -d --build                    # http://localhost:8080
docker compose run --rm api index               # build the knowledge base
```

On the host instead:

```bash
docker compose up -d db redis
uv sync --extra dev --extra service --extra integration --extra knowledge
uv run alembic upgrade head
uv run --env-file .env ccx index
npm --prefix web install

uv run --env-file .env ccx api                                   # :8000
uv run --env-file .env celery -A cloud_conversations.worker.app worker -Q chat -c 1
uv run --env-file .env celery -A cloud_conversations.worker.app worker -Q runs -c 2
cd web && VITE_API_BASE_URL=http://localhost:8000 npm run dev    # :5173
```

## Configuration

Settings are in `etc/config.toml`. Set `CC_CONFIG_FILE` or pass `--config` to use another file. Under compose, run `docker compose up -d --build` after editing it.

The agents' prompts and each testbed's guidance are in `bundle/`. See [docs/configuration.md](docs/configuration.md#bundle) for a description of the layout.

## Testing

```bash
uv sync --extra dev --extra service --extra integration --extra knowledge
uv run pytest -q
uv run ruff check src/ tests/
uv run mypy src/ tests/

cd web && npm install && npm run build && npm run lint && npm run test
```

The suite needs PostgreSQL with pgvector. Set `CC_TEST_DATABASE_URL` to a database the suite may wipe. It defaults to `cloudconversations_test` on `localhost`.
