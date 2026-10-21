# Audit trail

| Table or view | One row per |
|---|---|
| `audit.decisions` | proposal, confirmation, or refusal |
| `audit.testbed_calls` | start or outcome of a call that changes the testbed |
| `audit.timeline` | row of either table, as one line |

`request_id` links a call to the decision that allowed it. Run `\d+ audit.decisions` or `\d+ audit.testbed_calls` in `psql` for each column.

One user:

```sql
SELECT at, entry, actor, what, said FROM audit.timeline
 WHERE owner_id = 'chameleon:alice' ORDER BY at;
```

One request:

```sql
SELECT at, entry, actor, what, said FROM audit.timeline
 WHERE request_id = '<request-id>' ORDER BY at;
```
