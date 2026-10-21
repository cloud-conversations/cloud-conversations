# Knowledge base

Set these under `[knowledge]` in the config file.

## Sources

Add a table under `[knowledge.sources]` for each source.

| Field | Default | Meaning |
|---|---|---|
| `kind` | required | `sphinx` for the pages one index page links to. `blog` for a paginated listing. |
| `url` | required | Where the source starts. |
| `label` | required | Shown to the model alongside a passage. |
| `primary` | false | Official documentation. Its passages go to the model first. |
| `min_chars` | 0 | Skip pages shorter than this, in characters. |
| `index` | `contents.html` | `sphinx` only. The page whose links list the site's pages. |
| `exclude` | empty | `sphinx` only. Pages to leave out, relative to `url`. |
| `categories` | empty | `blog` only. The categories to keep. Leave it empty to keep every post. |

Run `ccx index` after changing a source. Set `cache_dir` to where `ccx index` keeps the fetched pages. It defaults to `var/knowledge`.

## Search

Embedding and reranking run locally. Set `HF_HOME` to where the two models should download, a few gigabytes in all.

| Setting | Default | Sets |
|---|---|---|
| `embedding_model` | `BAAI/bge-large-en-v1.5` | the model that embeds chunks and questions |
| `reranker_model` | `BAAI/bge-reranker-base` | the model that scores each candidate against the question |
| `embedding_device`, `reranker_device` | `cpu` | where the two models run |
| `embed_batch` | 32 | chunks to embed in one call |
| `reranker_max_tokens` | 512 | the longest question and candidate pair the reranker reads, in tokens |
| `query_instruction` | the bge instruction | text added before each question |
| `query_prefix` | empty | text added after the instruction |
| `min_reranker_score` | 0.5 | the lowest reranker score to keep, from 0 to 1 |
| `parent_chars`, `parent_overlap` | 2000, 200 | size and overlap of the passages given to the model, in characters |
| `child_chars`, `child_overlap` | 400, 50 | size and overlap of the chunks the search matches, in characters |
| `fetch_candidates` | 50 | chunks to fetch from the vector search |
| `pool_candidates` | 20 | chunks to rerank, split across sources |
| `passages` | 6 | passages to give the model, at most one per page |
| `max_history_exchanges` | 3 | earlier questions and answers to include with a follow-up |
