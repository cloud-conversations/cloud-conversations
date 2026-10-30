# Configuration

## Precedence

Highest first:

1. CLI flags
2. `[adapters.<name>.sites."<site>"]`
3. `[adapters.<name>.kinds.<kind>]`
4. `[adapters.<name>]`
5. `[defaults]`
6. built-in defaults

Set any `[defaults]` key in an adapter, kind, or site table to override it there.

## Testbeds and sites

| Table | Holds |
|---|---|
| `[adapters.<name>]` | One testbed. Only `chameleon` has an adapter. `label` names it on the page. `admins` lists the usernames there that get the Admin page. `site_index_url` is the testbed's public list of sites. |
| `[adapters.<name>.kinds.<kind>]` | A reservation kind, as in `baremetal` or `virtual`. |
| `[adapters.<name>.sites."<site>"]` | One site. `kinds` lists its kinds, in the order the catalog shows them. |
| `[adapters.<name>.images]` | How the testbed names its images. See [Images](#images). |
| `[adapters.<name>.login]` | Present if users log in through the testbed. See [login.md](login.md). |

```toml
[adapters.chameleon]
label = "Chameleon"
admins = ["alice"]
site_index_url = "https://api.chameleoncloud.org/sites"

[adapters.chameleon.kinds.baremetal]
reservation_kind = "physical_host"
lease_active_s = 1800

[adapters.chameleon.kinds.virtual]
reservation_kind = "flavor_instance"
uses_security_groups = true

[adapters.chameleon.sites."CHI@TACC"]
kinds = ["virtual", "baremetal"]
```

### Kind fields

| Field | Default |
|---|---|
| `reservation_kind` | required: `physical_host` or `flavor_instance` |
| `uses_security_groups` | false |

### Site fields

| Field | Default |
|---|---|
| `kinds` | required |
| `instance_network` | none. The network instances boot on. `etc/config.toml` sets `sharednet1` for Chameleon. |
| `floating_ip_pool` | none. The external network public addresses come from. `etc/config.toml` sets `public` for Chameleon. |
| `dns_probe_host` | `example.com` |
| `auth_url`, `node_catalog_url` | read from the adapter's `site_index_url` |
| `identity_provider`, `federation_protocol`, `project_domain` | none. Keystone federates a logged-in user's token with these. |

See [retiring-sites.md](retiring-sites.md) to retire a site.

### Images

| Field | Default | Sets |
|---|---|---|
| `prefix` | empty | the start of every supported image's name, before `<Distro><Version>` |
| `arch_markers` | none | name markers for each architecture other than `default_arch`, as in `{ aarch64 = ["arm64"] }` |
| `default_arch` | `x86_64` | the architecture of an image whose name has no marker |
| `supported_properties` | none | Glance properties that mark an image the testbed supports. Supported images win when a description names only an OS release. |

## Bundle

Prompts and testbed guidance are in `bundle/`. Set `bundle_dir` under `[service]` to use another copy.

| Path | Holds |
|---|---|
| `agents/<agent>/<name>.md` | a prompt. `{testbed}` becomes the testbed's `label`, and `{about}` the text of `testbeds/<testbed>/about.md`. |
| `testbeds/<testbed>/about.md` | one line that names and describes the testbed |
| `testbeds/<testbed>/guidance/hardware.md`, `image.md`, `software.md` | rules the describe agent gets for that stage |

## Model

Precedence, highest first:

| Level | Model | Temperature |
|---|---|---|
| a flag | `--model` | `--temperature` |
| the environment | `CC_LLM_MODEL` | `CC_LLM_TEMPERATURE` |
| `[defaults]` | `llm_model` | `llm_temperature` |

Set `agent_policy` under `[defaults]` to add rules to both agents' instructions. Set `answer_policy` under `[knowledge]` to add rules to the docs chatbot's.

```toml
agent_policy = """
Decline anything that would run longer than a working day.
"""
```

## `[service]`

| Setting | Default | Sets |
|---|---|---|
| `host`, `port` | `127.0.0.1`, 8000 | the address `ccx api` binds. `--host` and `--port` override them. |
| `broker_url` | `redis://localhost:6379/0` | Celery's broker. `CC_BROKER_URL` overrides it. |
| `job_timeout_s` | 5400 | seconds a job may run. A chat build must have time to start its lease, boot, reach SSH and install its software within this. |
| `route_history` | 6 | recent messages in the chat that the router reads |
| `thread_title_chars` | 60 | characters of the first message to use as a chat's title |
| `oidc_scope` | `openid profile email` | scopes to request from the login provider at login |
| `oidc_s` | 10 | timeout for each call to the login provider, in seconds |
| `login_s` | 600 | seconds a user has to finish logging in at the login provider |
| `session_days` | 30 | the longest a login lasts |
| `login_check_s` | 300 | seconds between rechecks of a login with the login provider |
| `restore_days` | 30 | days a deleted chat stays in the trash |
| `feedback_page_sizes` | `[5, 25, 100]` | choices of feedback per page on the Admin page. The first is the default. |
| `user_page_sizes` | `[25, 100]` | choices of users per page on the Admin page. The first is the default. |
| `max_messages_per_day` | 200 | messages each user can send in 24 hours |
| `max_builds_per_user` | 2 | builds each user can run at once |
| `max_active_experiments_per_user` | 4 | running builds plus runs with live hardware, for each user |
| `local_login` | false | whether the login page offers local test users a password form |
| `local_users_testbed` | none | the testbed local test users belong to. Required with `local_login`, and by the three local-user commands. |

## Environment

| Variable | Default | Sets |
|---|---|---|
| `CC_CONFIG_FILE` | `etc/config.toml` | the config file. `--config` overrides it. |
| `DATABASE_URL` | `postgresql://dev:dev@localhost:5432/cloudconversations` | the PostgreSQL DSN |
| `CC_OWNER_ID` | `<testbed>:cli` if one testbed is configured | the account `ccx` acts as, as `<testbed>:<name>` |
| `CC_OIDC_<TESTBED>_ISSUER`, `_CLIENT_ID`, `_CLIENT_SECRET`, `CC_PUBLIC_URL`, `CC_ADMINS` | unset | the chat page's login. See the [README](../README.md#running-the-chat-page). |
| `CC_SECRET_KEY` | unset | the Fernet key that encrypts refresh tokens and the system key. Builds at Chameleon and the chat page need it. See [login.md](login.md) to generate it. |
| `CC_LLM_BASE_URL` | `llm_base_url` | the model endpoint. It must be OpenAI-compatible. |
| `CC_LLM_MODEL` | `llm_model` | the model at that endpoint. `--model` overrides it. |
| `CC_LLM_TEMPERATURE` | `llm_temperature` | sampling temperature, 0 to 2. `--temperature` overrides it. |
| `CC_LLM_API_KEY` | unset | the endpoint's key. Set it in the environment, not the config file. |
| `CC_LOG_LEVEL` | unset | `INFO` to log resource creation and deletion. `DEBUG` to add every provider call and its latency. Pass celery's `--loglevel` to a worker on the host. |
| `CC_RUNS_CONCURRENCY` | 2 | builds and releases the compose runs worker carries out at once, across all users. Pass celery's `-c` to a worker on the host. |
| `CC_BROKER_URL` | `broker_url` | Celery's broker, for `ccx api` and the workers |
| `OS_CLOUD` and the other `OS_*` variables | unset | `ccx`'s login at each site. See [credentials.md](credentials.md). |
| `HF_HOME` | `~/.cache/huggingface` | where the knowledge base's models download |
| `VITE_API_BASE_URL` | `http://localhost:8000` | the API that `npm run dev` sends `/api` to |
| `CC_TEST_DATABASE_URL` | `postgresql://dev:dev@localhost:5432/cloudconversations_test` | the database the test suite wipes |
