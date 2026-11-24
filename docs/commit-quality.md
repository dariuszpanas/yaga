# Commit quality advisory

`yaga commit quality` asks a model whether a commit message explains the change clearly.
The provider receives the selected text. Use `yaga commit check` to enforce your repository's
deterministic body, scope, footer and spelling rules.

## Use a local server

Run Ollama with a model you have installed. With Ollama listening at its usual loopback
address, this example asks it to review a synthetic message. Substitute the model name
shown by your server.

```bash
yaga commit quality --provider openai-compatible --model llama3.2 \
  --message "fix(parser): reject duplicate configuration keys"
```

YAGA sends a non-streaming Chat Completions request to
`http://127.0.0.1:11434/v1/chat/completions`. An ordinary Ollama installation accepts it
without an API key. Your server owns model installation, weights, hardware selection and
inference. YAGA only needs the API.

Ollama can also serve supported Hugging Face models. Use the exact model name registered
with the server. YAGA requests JSON-object output. The decision belongs in the final
assistant message, for example `{"decision":"pass","reason":"Names the behavior change"}`.
A server that returns a single `PASS` or `FLAG` line can supply a short reason there as well.

For another server, set `YAGA_OPENAI_COMPATIBLE_BASE_URL` to its API base, including any
required prefix such as `/v1`. Use HTTPS for a remote server. Plain HTTP is available on
loopback. A server that requires authentication can use `YAGA_OPENAI_COMPATIBLE_API_KEY`.
This provider uses its own credential, independently of any OpenAI account key.

## Select a hosted provider

Select a provider in the command or in a trusted quality configuration. A key in the
environment alone does not enable model feedback. For cloud providers, choose a model
your account can access and decide whether to send the full message or only its title.

| Provider | Service | Credential |
| --- | --- | --- |
| `openai` | OpenAI Responses | `OPENAI_API_KEY` |
| `anthropic` | Anthropic Messages | `ANTHROPIC_API_KEY` |
| `gemini` | Gemini generateContent | `GEMINI_API_KEY` |
| `bedrock` | Amazon Bedrock Converse | AWS SDK credential chain and region |
| `openai-compatible` | A server you operate or select | Optional `YAGA_OPENAI_COMPATIBLE_API_KEY` |

```bash
yaga commit quality --provider anthropic --model "$MODEL_ID" --input-mode title --commit HEAD
yaga commit quality --provider bedrock --region us-east-1 --file .git/COMMIT_EDITMSG
```

OpenAI, Anthropic, Gemini and compatible servers require a model identifier. Bedrock keeps
the existing `amazon.nova-micro-v1:0` default. Configure credentials through the provider
environment or the AWS SDK. Policy files and command arguments have no credential field.

The standalone release and zero-dependency Python wheel both include these native Rust API
clients directly. No extra Python packages are required for Bedrock or HTTP providers.

## Configuration and migration

Set defaults in `[commit.quality]` in `.yaga.toml`, or `[tool.yaga.commit.quality]` in
`pyproject.toml`. CLI options override configured values. To select one explicit policy
use `--config`.

```toml
config-version = 1

[commit.quality]
provider = "openai-compatible"
model = "llama3.2"
input-mode = "title"
max-tokens = 64
```

Older schema-v1 policies with Hugging Face settings still load for ordinary checks,
`config show` and policy validation. Running `commit quality` with the old local
provider or its implicit default exits 2 with migration instructions. To resume advisory
checks, choose a provider, set its model and remove the local model revision, classifier
threshold and tokenizer settings from the quality table. The API returns a decision and
a reason. Old classifier scores and thresholds have no equivalent in this mode.

Existing install scripts can keep the Python extra `quality` or Cargo feature
`quality-models` while migrating. These compatibility names install no model dependencies.

For operations that require zero network calls, use `commit check`. The `--offline`
flag prevents a quality API request, including one to a loopback server.

## Inputs, reports and exits

Select one of `--message`, `--file`, `--stdin`, `--commit` or `--range`. Without an
explicit source, YAGA checks `HEAD`. File, stdin and direct message inputs avoid Git.
Revision and range selections use local Git history. Select `--input-mode title` to send
only the first line. The default `message` includes the body. The provider input
is bounded to 12,000 characters per message, with at most 256 messages.

Use `--format json` for the versioned report, `--format github` for annotations and
`--quiet` for exit status alone. Reports identify the selected provider and model, include
the decision and bounded reason, and fingerprint the selected text. Exit 0 means every
message passed, exit 1 means the provider flagged at least one, and exit 2 means YAGA
could not complete the check. Review provider feedback alongside the original message.
