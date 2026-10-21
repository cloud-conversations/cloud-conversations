"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-10-19
"""
from alembic import op

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA auth")
    op.execute("CREATE SCHEMA chat")
    op.execute("CREATE SCHEMA experiments")
    op.execute("CREATE SCHEMA knowledge")
    op.execute("CREATE SCHEMA audit")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute("""
        CREATE TABLE auth.users (
            user_id                         text        PRIMARY KEY,
            username                        text        NOT NULL,
            email                           text,
            projects                        jsonb       NOT NULL DEFAULT '[]',
            created_at                      timestamptz NOT NULL DEFAULT now(),
            last_login_at                   timestamptz,
            default_project_id              text,
            default_ssh_key_ref             text,
            password_hash                   text,
            first_login_at                  timestamptz,
            checked_at                      timestamptz,
            disabled_at                     timestamptz,
            disabled_reason                 text,
            default_vm_site                 text,
            default_baremetal_site          text,
            default_site_keypairs           jsonb,
            max_messages_per_day            int         CHECK (max_messages_per_day > 0),
            max_builds_per_user             int         CHECK (max_builds_per_user > 0),
            max_active_experiments_per_user int
                                            CHECK (max_active_experiments_per_user > 0)
        )
    """)
    op.execute("""
        CREATE TABLE auth.sessions (
            token_hash  text        PRIMARY KEY,
            user_id     text        NOT NULL REFERENCES auth.users ON DELETE CASCADE,
            created_at  timestamptz NOT NULL DEFAULT now(),
            expires_at  timestamptz NOT NULL
        )
    """)
    op.execute("CREATE INDEX ON auth.sessions (user_id)")
    op.execute("""
        CREATE TABLE auth.refresh_tokens (
            user_id        text        PRIMARY KEY REFERENCES auth.users ON DELETE CASCADE,
            refresh_token  text        NOT NULL,
            updated_at     timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE auth.system_keys (
            fingerprint  text        PRIMARY KEY,
            private_key  text        NOT NULL,
            created_at   timestamptz NOT NULL DEFAULT now(),
            retired_at   timestamptz
        )
    """)
    # At most one current key.
    op.execute("""
        CREATE UNIQUE INDEX system_keys_current
          ON auth.system_keys ((retired_at IS NULL))
          WHERE retired_at IS NULL
    """)
    op.execute("""
        CREATE TABLE auth.user_keys (
            owner_id      text        NOT NULL
                          CONSTRAINT owner_is_a_user
                          REFERENCES auth.users (user_id) ON DELETE CASCADE,
            name          text        NOT NULL,
            keypair_name  text        NOT NULL,
            public_key    text        NOT NULL,
            created_at    timestamptz NOT NULL DEFAULT now(),
            updated_at    timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (owner_id, name),
            UNIQUE (owner_id, keypair_name)
        )
    """)

    op.execute("""
        CREATE TABLE experiments.experiment_descriptions (
            description_id   uuid        NOT NULL,
            version          int         NOT NULL,
            parent_version   int,
            revision_reason  text,
            owner_id         text        NOT NULL
                             CONSTRAINT owner_is_a_user
                             REFERENCES auth.users (user_id) ON DELETE CASCADE,
            conversation_id  text        NOT NULL,
            requirements     jsonb       NOT NULL,
            rubric           jsonb       NOT NULL,
            unsupported      jsonb       NOT NULL DEFAULT '[]',
            open_questions   jsonb       NOT NULL DEFAULT '[]',
            created_at       timestamptz NOT NULL DEFAULT now(),
            deleted_at       timestamptz,
            PRIMARY KEY (description_id, version),
            CONSTRAINT descriptions_version_owner UNIQUE (description_id, version, owner_id)
        )
    """)
    op.execute(
        "CREATE INDEX ON experiments.experiment_descriptions (owner_id, conversation_id)"
    )
    op.execute("""
        CREATE TABLE experiments.runs (
            run_id              uuid        PRIMARY KEY,
            description_id      uuid        NOT NULL,
            description_version int         NOT NULL,
            owner_id            text        NOT NULL
                                CONSTRAINT owner_is_a_user
                                REFERENCES auth.users (user_id) ON DELETE CASCADE,
            project_id          text,
            state               text        NOT NULL CHECK (state IN (
                                    'planning', 'provisioning', 'configuring', 'validating',
                                    'passed', 'failed', 'abandoned'
                                )),
            resume_count        int         NOT NULL DEFAULT 0,
            plan                jsonb,
            results             jsonb,
            connection          jsonb,
            failure             jsonb,
            created_at          timestamptz NOT NULL DEFAULT now(),
            updated_at          timestamptz NOT NULL DEFAULT now(),
            heartbeat_at        timestamptz NOT NULL DEFAULT now(),
            deleted_at          timestamptz,
            FOREIGN KEY (description_id, description_version)
                REFERENCES experiments.experiment_descriptions (description_id, version),
            CONSTRAINT run_shares_its_descriptions_owner
                FOREIGN KEY (description_id, description_version, owner_id)
                REFERENCES experiments.experiment_descriptions
                    (description_id, version, owner_id)
        )
    """)
    op.execute("CREATE INDEX ON experiments.runs (owner_id, created_at DESC)")
    op.execute("CREATE INDEX ON experiments.runs (description_id, description_version)")
    op.execute("""
        CREATE TABLE experiments.run_resources (
            kind            text        NOT NULL,
            provider_id     text        NOT NULL,
            run_id          uuid        NOT NULL
                            REFERENCES experiments.runs (run_id) ON DELETE CASCADE,
            site            text        NOT NULL,
            idempotency_key text,
            expires_at      timestamptz,
            created_at      timestamptz NOT NULL DEFAULT now(),
            released_at     timestamptz,
            PRIMARY KEY (kind, provider_id)
        )
    """)
    op.execute(
        "CREATE INDEX ON experiments.run_resources (run_id) WHERE released_at IS NULL"
    )
    op.execute("""
        CREATE UNIQUE INDEX run_resources_live_idempotency_key_uidx
            ON experiments.run_resources (idempotency_key)
         WHERE idempotency_key IS NOT NULL AND released_at IS NULL
    """)
    op.execute("""
        CREATE INDEX run_resources_expires_at_idx
            ON experiments.run_resources (expires_at)
         WHERE released_at IS NULL
    """)
    op.execute("""
        CREATE TABLE experiments.run_events (
            run_id       uuid        NOT NULL
                         REFERENCES experiments.runs (run_id) ON DELETE CASCADE,
            seq          int         NOT NULL,
            at           timestamptz NOT NULL DEFAULT now(),
            actor        text        NOT NULL,
            kind         text        NOT NULL,
            payload      jsonb       NOT NULL,
            model_input  jsonb,
            PRIMARY KEY (run_id, seq)
        )
    """)

    op.execute("""
        CREATE TABLE chat.conversations (
            conversation_id     uuid        PRIMARY KEY,
            owner_id            text        NOT NULL
                                CONSTRAINT owner_is_a_user
                                REFERENCES auth.users (user_id) ON DELETE CASCADE,
            prompt              text        NOT NULL,
            transcript          jsonb       NOT NULL,
            description_id      uuid,
            description_version int,
            created_at          timestamptz NOT NULL DEFAULT now(),
            deleted_at          timestamptz,
            FOREIGN KEY (description_id, description_version)
                REFERENCES experiments.experiment_descriptions (description_id, version),
            CONSTRAINT conversation_shares_its_descriptions_owner
                FOREIGN KEY (description_id, description_version, owner_id)
                REFERENCES experiments.experiment_descriptions
                    (description_id, version, owner_id)
        )
    """)
    op.execute("CREATE INDEX ON chat.conversations (owner_id, created_at DESC)")
    op.execute("CREATE INDEX ON chat.conversations (description_id)")
    op.execute("""
        CREATE TABLE chat.threads (
            thread_id      uuid        PRIMARY KEY,
            owner_id       text        NOT NULL
                           CONSTRAINT owner_is_a_user
                           REFERENCES auth.users (user_id) ON DELETE CASCADE,
            title          text        NOT NULL,
            ssh_key_ref    text,
            description_id uuid,
            created_at     timestamptz NOT NULL DEFAULT now(),
            updated_at     timestamptz NOT NULL DEFAULT now(),
            project_id     text,
            deleted_at     timestamptz,
            site_keypairs  jsonb,
            CONSTRAINT threads_owner UNIQUE (thread_id, owner_id)
        )
    """)
    op.execute("CREATE INDEX ON chat.threads (owner_id, updated_at DESC)")
    op.execute("""
        CREATE TABLE chat.messages (
            message_id          uuid        PRIMARY KEY,
            thread_id           uuid        NOT NULL REFERENCES chat.threads (thread_id)
                                            ON DELETE CASCADE,
            seq                 int         NOT NULL,
            role                text        NOT NULL CHECK (role IN ('user', 'assistant')),
            kind                text        NOT NULL CHECK (kind IN (
                                    'user', 'answer', 'description', 'run', 'notice',
                                    'holdings', 'release'
                                )),
            text                text        NOT NULL,
            payload             jsonb       NOT NULL DEFAULT '{}',
            conversation_id     uuid,
            description_id      uuid,
            description_version int,
            created_at          timestamptz NOT NULL DEFAULT now(),
            UNIQUE (thread_id, seq)
        )
    """)
    op.execute(
        "CREATE INDEX ON chat.messages (description_id) WHERE description_id IS NOT NULL"
    )
    op.execute("""
        CREATE TABLE chat.jobs (
            job_id      uuid        PRIMARY KEY,
            thread_id   uuid        NOT NULL REFERENCES chat.threads (thread_id)
                                    ON DELETE CASCADE,
            owner_id    text        NOT NULL
                        CONSTRAINT owner_is_a_user
                        REFERENCES auth.users (user_id) ON DELETE CASCADE,
            kind        text        NOT NULL CHECK (kind IN ('exchange', 'build', 'teardown')),
            payload     jsonb       NOT NULL DEFAULT '{}',
            state       text        NOT NULL
                                    CHECK (state IN ('queued', 'running', 'done', 'error')),
            route       text        CHECK (route IN (
                                    'describe', 'revise', 'build', 'inventory', 'ask'
                                )),
            error       text,
            started_at  timestamptz,
            finished_at timestamptz,
            created_at  timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT job_shares_its_threads_owner
                FOREIGN KEY (thread_id, owner_id)
                REFERENCES chat.threads (thread_id, owner_id) ON DELETE CASCADE
        )
    """)
    op.execute("CREATE INDEX ON chat.jobs (thread_id, created_at DESC)")
    op.execute("""
        CREATE UNIQUE INDEX jobs_one_unfinished_per_kind
            ON chat.jobs (thread_id, kind)
         WHERE state IN ('queued', 'running')
    """)
    op.execute(
        "CREATE INDEX ON chat.jobs (owner_id, kind) WHERE state IN ('queued', 'running')"
    )
    op.execute("""
        CREATE TABLE chat.announcement (
            only_one   boolean     PRIMARY KEY DEFAULT true CHECK (only_one),
            text       text        NOT NULL,
            posted_at  timestamptz NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        CREATE TABLE chat.feedback (
            feedback_id  uuid        PRIMARY KEY,
            owner_id     text        NOT NULL
                         CONSTRAINT owner_is_a_user
                         REFERENCES auth.users (user_id) ON DELETE CASCADE,
            thread_id    uuid        NOT NULL,
            message_id   uuid        UNIQUE REFERENCES chat.messages ON DELETE CASCADE,
            run_id       uuid        UNIQUE REFERENCES experiments.runs ON DELETE CASCADE,
            rating       text        CHECK (rating IN ('up', 'down')),
            comment      text,
            created_at   timestamptz NOT NULL DEFAULT now(),
            updated_at   timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT feedback_shares_its_threads_owner
                FOREIGN KEY (thread_id, owner_id)
                REFERENCES chat.threads (thread_id, owner_id) ON DELETE CASCADE,
            CONSTRAINT feedback_on_one_thing CHECK (message_id IS NULL OR run_id IS NULL),
            CONSTRAINT feedback_says_something CHECK (rating IS NOT NULL OR comment IS NOT NULL)
        )
    """)
    op.execute("CREATE INDEX ON chat.feedback (thread_id)")
    op.execute("""
        CREATE VIEW chat.feedback_context AS
        SELECT f.feedback_id, f.created_at, f.updated_at,
               f.owner_id, u.username, u.email,
               f.thread_id, t.title AS thread_title, t.project_id,
               f.rating, f.comment,
               f.message_id, m.kind AS message_kind, m.text AS message_text,
               m.payload AS message_payload,
               c.conversation_id, c.prompt, c.transcript,
               routing.conversation_id AS routing_conversation_id,
               routing.transcript AS routing_transcript,
               j.job_id, j.error AS job_error,
               d.description_id, d.version AS description_version,
               d.requirements, d.rubric,
               f.run_id, r.state AS run_state, r.failure AS run_failure
          FROM chat.feedback f
          JOIN auth.users u ON u.user_id = f.owner_id
          JOIN chat.threads t ON t.thread_id = f.thread_id
          LEFT JOIN chat.messages m ON m.message_id = f.message_id
          LEFT JOIN chat.conversations c ON c.conversation_id = m.conversation_id
          LEFT JOIN chat.conversations routing
                 ON routing.conversation_id = (m.payload->>'routing_conversation_id')::uuid
          LEFT JOIN chat.jobs j ON j.job_id = (m.payload->>'job_id')::uuid
          LEFT JOIN experiments.runs r ON r.run_id = f.run_id
          LEFT JOIN experiments.experiment_descriptions d
                 ON (d.description_id, d.version)
                    = (coalesce(r.description_id, m.description_id),
                       coalesce(r.description_version, m.description_version))
    """)
    op.execute(
        "COMMENT ON VIEW chat.feedback_context IS "
        "'Each piece of feedback, with what it was about.'"
    )

    op.execute("""
        CREATE TABLE knowledge.builds (
            build_id        uuid        PRIMARY KEY,
            embedding_model text        NOT NULL,
            dimensions      int         NOT NULL,
            documents       int         NOT NULL,
            parents         int         NOT NULL,
            chunks          int         NOT NULL,
            built_at        timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ON knowledge.builds (built_at DESC)")
    op.execute("""
        CREATE TABLE knowledge.documents (
            doc_id     uuid        PRIMARY KEY,
            source     text        NOT NULL,
            url        text        NOT NULL,
            title      text        NOT NULL,
            content    text        NOT NULL,
            fetched_at timestamptz NOT NULL,
            UNIQUE (source, url)
        )
    """)
    op.execute("""
        CREATE TABLE knowledge.parents (
            parent_id uuid NOT NULL PRIMARY KEY,
            doc_id    uuid NOT NULL
                      REFERENCES knowledge.documents (doc_id) ON DELETE CASCADE,
            ordinal   int  NOT NULL,
            content   text NOT NULL,
            UNIQUE (doc_id, ordinal)
        )
    """)
    op.execute("CREATE INDEX ON knowledge.parents (doc_id)")
    op.execute("""
        CREATE TABLE knowledge.chunks (
            chunk_id  uuid   NOT NULL PRIMARY KEY,
            parent_id uuid   NOT NULL
                      REFERENCES knowledge.parents (parent_id) ON DELETE CASCADE,
            ordinal   int    NOT NULL,
            content   text   NOT NULL,
            embedding vector NOT NULL,
            UNIQUE (parent_id, ordinal)
        )
    """)
    op.execute("CREATE INDEX ON knowledge.chunks (parent_id)")

    op.execute("""
        CREATE TABLE audit.decisions (
            seq            bigserial   PRIMARY KEY,
            at             timestamptz NOT NULL DEFAULT clock_timestamp(),
            request_id     uuid        NOT NULL,
            owner_id       text        NOT NULL,
            step           text        NOT NULL CHECK (step IN (
                               'proposed', 'confirmed', 'declined'
                           )),
            actor          text        NOT NULL CHECK (actor IN ('user', 'agent')),
            via            text        CHECK (via IN ('web', 'cli')),
            action         text        NOT NULL CHECK (action IN (
                               'build', 'resume', 'release', 'release_and_build',
                               'delete_keypair'
                           )),
            run_id         uuid,
            thread_id      uuid,
            description_id uuid,
            version        int,
            resources      jsonb       NOT NULL DEFAULT '[]',
            shown          text,
            reason         text,
            detail         jsonb       NOT NULL DEFAULT '{}'
        )
    """)
    op.execute("CREATE INDEX ON audit.decisions (request_id)")
    op.execute("CREATE INDEX ON audit.decisions (owner_id, run_id)")
    op.execute(
        "COMMENT ON TABLE audit.decisions IS "
        "$$Proposals to change the testbed, and the user's answers. One row per step.$$"
    )
    for column, says in {
        "seq": "Insertion order.",
        "at": "Time of the step.",
        "request_id": "The request. audit.testbed_calls.request_id refers to it.",
        "owner_id": "The account the request acts in.",
        "step": "proposed, confirmed, or declined. A build or resume has only a confirmed row.",
        "actor": "user or agent.",
        "via": "web or cli.",
        "action": "The change to the testbed.",
        "run_id": "The run released or resumed.",
        "thread_id": "The chat.",
        "description_id": "The description built, or the released run's description.",
        "version": "The description version to build.",
        "resources": "The resources to release or delete, as kind, provider_id, and site.",
        "shown": "The text of the agent's proposal or the CLI prompt.",
        "reason": "The actor's reason.",
        "detail": "The agent's step, the run's state and failure, and a revision's change.",
    }.items():
        op.execute(f"COMMENT ON COLUMN audit.decisions.{column} IS $${says}$$")

    op.execute("""
        CREATE TABLE audit.testbed_calls (
            seq         bigserial   PRIMARY KEY,
            at          timestamptz NOT NULL DEFAULT clock_timestamp(),
            call_id     uuid        NOT NULL,
            owner_id    text        NOT NULL,
            site        text        NOT NULL,
            operation   text        NOT NULL CHECK (operation IN (
                            'create_lease', 'launch_instance', 'ensure_security_group',
                            'allocate_floating_ip', 'attach_floating_ip', 'import_keypair',
                            'ssh_exec', 'delete_lease', 'delete_instance',
                            'delete_security_group', 'delete_floating_ip', 'delete_keypair'
                        )),
            kind        text        NOT NULL,
            provider_id text,
            outcome     text        NOT NULL CHECK (outcome IN ('started', 'ok', 'failed')),
            error       text,
            request_id  uuid        NOT NULL,
            run_id      uuid,
            reason      text,
            arguments   jsonb       NOT NULL DEFAULT '{}'
        )
    """)
    op.execute("CREATE INDEX ON audit.testbed_calls (request_id)")
    op.execute("CREATE INDEX ON audit.testbed_calls (owner_id, run_id)")
    op.execute(
        "COMMENT ON TABLE audit.testbed_calls IS "
        "$$Calls that change the testbed. One started row and one ok or failed row per call.$$"
    )
    for column, says in {
        "seq": "Insertion order.",
        "at": "Time of the row.",
        "call_id": "The call.",
        "owner_id": "The account that made the call.",
        "site": "The site called.",
        "operation": "The adapter method. ssh_exec runs a command on an instance.",
        "kind": "The resource kind.",
        "provider_id": "The testbed's ID for the resource. Null if a create failed first.",
        "outcome": "started, ok, or failed.",
        "error": "The exception raised.",
        "request_id": "The decision in audit.decisions that allowed the call.",
        "run_id": "The run the call was for.",
        "reason": "The runner's reason for a cleanup.",
        "arguments": "The call's arguments, except site.",
    }.items():
        op.execute(f"COMMENT ON COLUMN audit.testbed_calls.{column} IS $${says}$$")

    op.execute("""
        CREATE FUNCTION audit.refuse_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
        END
        $$
    """)
    for table in ("decisions", "testbed_calls"):
        op.execute(f"""
            CREATE TRIGGER {table}_append_only
                BEFORE UPDATE OR DELETE ON audit.{table}
                FOR EACH ROW EXECUTE FUNCTION audit.refuse_change()
        """)

    op.execute("""
        CREATE VIEW audit.timeline AS
        SELECT at, owner_id, request_id, run_id, 'decision' AS entry, actor,
               step || ' ' || action AS what,
               coalesce(shown, reason) AS said
          FROM audit.decisions
        UNION ALL
        SELECT at, owner_id, request_id, run_id, 'testbed call' AS entry, NULL AS actor,
               operation || ' ' || kind || coalesce(' ' || provider_id, '')
                   || ' at ' || site || ': ' || outcome AS what,
               coalesce(error, reason) AS said
          FROM audit.testbed_calls
         ORDER BY at
    """)
    op.execute(
        "COMMENT ON VIEW audit.timeline IS 'Both audit tables, one line per row.'"
    )


def downgrade() -> None:
    op.execute("DROP SCHEMA audit, knowledge, chat, experiments, auth CASCADE")
