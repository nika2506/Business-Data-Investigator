# AI workflow

This file documents **how AI tools were used to develop and validate the submission**, rather than describing only the final application. The initial OpenCode-generated implementation and the final application differ substantially. The Git history is the source of truth for code evolution.

## Development tools and roles

- **OpenCode:** Used for the initial implementation and the first AI-workflow documentation. The original manifest recorded `openai/gpt-6.1-terra` for the documentation session. The precise OpenCode version, complete session logs, and model parameters are not available here. Do not interpret that single recorded model as proof that all earlier OpenCode sessions used it.
- **ChatGPT:** Used in later iterations to discuss architecture, generate and review targeted code changes, implement Replay, write tests, debug failures, and prepare documentation. Exact model settings and full chat transcripts are not included.
- **Application LLM:** Separate from the coding assistants. The final application uses LangChain with OpenAI (`ChatOpenAI`) for model-backed SQL investigations. The final model identifier and inference parameters should be read from the actual checked-in code and environment; credentials are not committed.

No exercise-specific MCP servers, hooks, plugins, or subagents are documented as used. The starter pack included `skills/generate-assignment-data/SKILL.md`, but its use has not been established.

## Implementation evolution

1. **Initial implementation:** An OpenCode-generated SQL investigation application, originally documented as a single-file `app.py` using local Ollama. This description is historical, not the final runtime configuration.
2. **Revised application:** Moved to a modular FastAPI application (`app/main.py`, `app/agent.py`, etc.) with LangChain/OpenAI model mode, a deterministic Offline mode, SQL evidence, and optional context from the preceding investigation.
3. **Replay mode:** Added import of an exported real model investigation. Replay re-executes the recorded SQL against the current database and compares evidence, without making new LLM calls. It can report matched or mismatched evidence. The recorded final narrative is reused, not regenerated.
4. **Validation and debugging:** Iteratively tested and refined the AI-generated application, identifying and fixing issues with SQL query generation, data aggregation, and investigation logic. Verified results against expected business metrics, improved handling of incorrect or incomplete queries, and added automated tests using temporary SQLite fixtures and mocked/no-LLM execution paths.

## Reproduction and checks

- **Offline:** Run the FastAPI app following the root `README.md` and select Offline; no OpenAI API call is required.
- **Model:** Configure a valid OpenAI API key and use Model mode; results depend on the model and database state.
- **Replay:** In the UI, export a genuine Model investigation as JSON, then import that JSON in Replay mode. The app reruns the saved SQL and compares results without contacting OpenAI. To check mismatch reporting, alter the database or the expected saved evidence and replay again. Replay does not regenerate the original response.
- **Tests:** Run `python -m unittest -v tests.test_investigator` if `tests` is importable as a package; otherwise run the test file from the project root using your test runner. No claim is made here that the entire suite passed on the final checkout; record the actual result before submission.

## Transparency and limitations

The original documentation described an Ollama-based `app.py`, stated that no real responses were available to replay, and only claimed documentation inspection. Those claims refer to the early snapshot and must not be presented as properties of the final application. The final app has Replay and uses OpenAI through LangChain. Historical prompt logs and full AI assistant configurations are not reconstructed or invented. Secrets, API keys, and private configuration values must not be committed. Development time was not tracked reliably.
