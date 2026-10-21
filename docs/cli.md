# CLI

## Commands

| Command | Description |
|---|---|
| `ccx check --site <site>` | Run read-only checks against a site. |
| `ccx describe "<prompt>"` | Turn a prompt into a description, using a model. |
| `ccx inventory "<request>"` | List the leases, instances and images your project has. |
| `ccx ask "<question>"` | Answer a question from the documentation. Leave out the question to ask at a prompt until Ctrl-D. |
| `ccx index` | Build the knowledge base from the configured sources. |
| `ccx knowledge` | Show what the live knowledge index holds. |
| `ccx show-conversation --conversation-id <uuid>` | Print a stored conversation. |
| `ccx describe-fixture --name <fixture>` | Write a description and print its id and rubric. |
| `ccx provision --description-id <uuid>` | Provision against an existing description. |
| `ccx build --description-id <uuid>` | Provision, and let an agent recover a failed attempt. |
| `ccx run --fixture <fixture>` | `describe-fixture` and `provision` in one call. |
| `ccx show-run --run-id <uuid> --events` | Run state and event stream, or `--json`. |
| `ccx show-build --run-id <uuid>` | A whole build: both agents, every run, in order. |
| `ccx resume --run-id <uuid>` | Resume an incomplete or failed run. |
| `ccx teardown --run-id <uuid>` | Release a run's resources. |
| `ccx capabilities` | Show the configured sites. Add `--site <site>` for that site's node types. |
| `ccx import-key --name <name> --public-key <file>` | Import a public key. Pass the keypair name it prints as `--keypair`. |
| `ccx show-keys` | List the imported keys and their keypair names. |
| `ccx add-local-user --name <name>`, `ccx set-local-password --name <name>`, `ccx delete-local-user --name <name>` | Manage local test users. Type the password at the prompt or pipe it in. |
| `ccx show-system-key` | List the SSH keys Cloud Conversations logs in to instances with, or `--private <fingerprint>` for one key's private half. |
| `ccx rotate-system-key` | Replace the current system key. |
| `ccx api` | Serve the chat page's API. |

## Flags

Run `ccx <command> --help` for one command's flags.

| Command | Flags |
|---|---|
| Every command except `show-system-key` and `rotate-system-key` | `--config` |
| `provision`, `build`, `run`, `resume` | `--ssh-user` |
| `describe`, `build`, `ask`, `inventory` | `--model`, `--temperature` |
| `describe-fixture` | `--name`, `--description-id`, `--conversation-id`, `--site`, `--node-type`, `--flavor`, `--hours`, `--start-at`, `--image-name`, `--os-release`, `--min-cpus` |
| `describe` | `--site`, `--description-id`, `--version`, `--verbose` |
| `check` | `--site`, `--keypair`, `--node-type`, `--flavor`, `--image-name`, `--os-release`, `--hours` |
| `provision` | `--description-id`, `--version`, `--site`, `--project-id`, `--keypair`, `--teardown`, `--yes` |
| `build` | `--description-id`, `--version`, `--site`, `--run-id`, `--project-id`, `--keypair`, `--teardown`, `--yes`, `--verbose` |
| `run` | `--fixture`, `--site`, `--keypair`, `--image-name`, `--project-id`, `--teardown`, `--yes` |
| `show-run` | `--run-id`, `--events`, `--json` |
| `show-conversation` | `--conversation-id` |
| `teardown` | `--run-id`, `--yes` |
| `resume` | `--run-id` |
| `show-build` | `--run-id`, or `--description-id` for its most recent run |
| `inventory`, `capabilities` | `--site` |
| `ask` | `--verbose` |
| `index` | `--refresh`, `--use-cache` |
| `api` | `--host`, `--port` |
| `import-key` | `--name`, `--public-key` |
| `add-local-user`, `set-local-password`, `delete-local-user` | `--name` |
| `show-system-key` | `--private` |

Pass `--start-at` an ISO 8601 time. Without an offset, it is UTC. Leave it out to start the lease as soon as the calendar allows.

## Releases

Pass `--yes` to release without confirming. It does not confirm releases the experiment agent proposes.

Each decision and each change to the testbed is in the [audit trail](audit.md).
