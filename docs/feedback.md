# Feedback

| Table or view | One row per |
|---|---|
| `chat.feedback` | rating or comment |
| `chat.feedback_context` | piece of feedback, with the user, message, conversation, description and run it is about |

The Admin page lists feedback newest first. Open an ID there to see its full context. Use the queries below to search or count across feedback.

One piece of feedback:

```sql
SELECT * FROM chat.feedback_context WHERE feedback_id = '<feedback-id>';
```

What the model was sent and what it replied, one row per round. Each round's `sent` holds the messages sent since the previous reply. Read `routing_transcript` the same way for the routing step.

```sql
SELECT r->>'round' AS round, r->'sent' AS sent, r->>'text' AS reply,
       r->>'model' AS model, r->>'served_model' AS served_model,
       r->>'temperature' AS temperature, r->>'bundle_hash' AS bundle_hash
  FROM chat.feedback_context, jsonb_array_elements(transcript) r
 WHERE feedback_id = '<feedback-id>';
```

A rated run's build-agent turns:

```sql
SELECT e.seq, e.payload->>'action' AS action, e.payload->>'text' AS reply, e.model_input
  FROM chat.feedback f JOIN experiments.run_events e USING (run_id)
 WHERE f.feedback_id = '<feedback-id>' AND e.kind = 'model_turn'
 ORDER BY e.seq;
```

A rated run's audit trail is in [audit.md](audit.md). Look it up by `run_id`.

Ratings by prompt version:

```sql
SELECT transcript->0->>'bundle_hash' AS bundle_hash, rating, count(*)
  FROM chat.feedback_context
 WHERE transcript IS NOT NULL
 GROUP BY 1, 2 ORDER BY 1, 2;
```
