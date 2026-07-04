# OpenAI to Claude API Proxy

A local proxy server that translates between OpenAI-compatible API format and Claude (Anthropic) API format, allowing Claude Code and other Claude-based tools to use OpenAI-compatible backends.

## Features

- ✅ **Dual API support**: Both Claude API format and OpenAI API format
- ✅ **Translation mode**: Claude API requests → OpenAI backend (with translation)
- ✅ **Pass-through mode**: OpenAI API requests → OpenAI backend (no translation)
- ✅ **Multi-user support**: API key authentication for multiple users
- ✅ **Usage tracking**: Track token usage per API key
- ✅ **Admin endpoints**: User and API key management
- ✅ **Web-based Admin UI**: Beautiful interface for managing users and viewing usage
- ✅ Handles system messages properly
- ✅ Supports multi-turn conversations
- ✅ Handles Claude's content blocks
- ✅ **Full tool/function calling support**
- ✅ Special support for reasoning models (like glm-4.6)
- ✅ Comprehensive test suite
- ✅ Easy configuration via environment variables

## Repository Layout

InfiniProxy is intentionally runnable as an independent service module. The most important files are:

```text
modules/infiniproxy/
├── proxy_server.py              # FastAPI server and proxy endpoints
├── phantom_infiniproxy/cli.py   # service lifecycle CLI used by systemd/user
├── admin_auth.py                # admin authentication helpers
├── user_manager.py              # user and API key database management
├── translator.py                # Claude/OpenAI payload translation
├── static/                      # admin UI assets
├── k8s/                         # Kubernetes deployment templates
├── tests/                       # pytest-style tests
└── test_*.py                    # compatibility and smoke test scripts
```

Documentation is grouped by topic:

| Topic | Start here |
| --- | --- |
| Quick start | `QUICK_START.md`, `QUICKSTART.md` |
| Client setup | `PROXY_CLIENT_SETUP.md`, `CLIENT_COMPATIBILITY.md`, `WRAPPER_CLIENTS_GUIDE.md` |
| Migration and compatibility | `MIGRATION_GUIDE.md`, `BACKWARD_COMPATIBILITY.md` |
| Deployment | `DEPLOYMENT.md`, `DOCKER.md`, `API_KEYS_SETUP.md`, `CONFIG_UPDATE.md` |
| Integrations | `ELEVENLABS_INTEGRATION.md`, `FIRECRAWL_INTEGRATION.md`, `SERPAPI_INTEGRATION.md`, `TAVILY_INTEGRATION.md`, `AGNO_INTEGRATION_GUIDE.md` |
| Reports and summaries | `TESTING_SUMMARY.md`, `TEST_RESULTS.md`, `FINAL_TEST_RESULTS.md`, `FINAL_PROJECT_SUMMARY.md` |

## Architecture

The proxy supports two modes:

### Translation Mode (Claude API → OpenAI Backend)
```
Claude Code → HTTP (Claude Format) → Proxy Server → HTTP (OpenAI Format) → OpenAI-Compatible Backend
                                      /v1/messages
                                           ↓ (translate)
Claude Code ← HTTP (Claude Format) ← Proxy Server ← HTTP (OpenAI Format) ← OpenAI-Compatible Backend
```

### Pass-Through Mode (OpenAI API → OpenAI Backend)
```
OpenAI Client → HTTP (OpenAI Format) → Proxy Server → HTTP (OpenAI Format) → OpenAI-Compatible Backend
                                        /v1/chat/completions
                                             ↓ (pass-through)
OpenAI Client ← HTTP (OpenAI Format) ← Proxy Server ← HTTP (OpenAI Format) ← OpenAI-Compatible Backend
```

## Installation

1. Clone or download this repository
2. Create a virtual environment and install dependencies:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

3. Configure the environment variables:

```bash
# Copy the example .env file
cp .env.example .env

# Edit .env with your settings
nano .env
```

## Configuration

Edit the `.env` file with your settings:

```bash
# OpenAI-compatible backend configuration
OPENAI_BASE_URL=https://cloud.infini-ai.com/maas/v1/chat/completions
OPENAI_API_KEY=your-api-key-here
OPENAI_MODEL=glm-4.6

# Proxy server configuration
PROXY_HOST=localhost
PROXY_PORT=8000

# Optional settings
TIMEOUT=300
DEBUG=false
```

## Usage

### Starting the Proxy Server

```bash
# Activate virtual environment
source venv/bin/activate

# Run the proxy server
python proxy_server.py
```

The server will start on `http://localhost:8000` (or your configured port).

### Web-Based Admin Interface

The easiest way to manage users and API keys is through the web-based admin interface:

**Access the Admin UI:**
```
http://localhost:8000/admin
```

The admin interface provides:
- 👥 **Users Tab**: Create and view all users
- 🔑 **API Keys Tab**: Generate, view, and deactivate API keys
- 📊 **Usage Statistics Tab**: View token usage per API key

**Features:**
- Beautiful, responsive interface
- Real-time data updates
- Secure API key generation (shown only once)
- Usage tracking visualization
- Filter API keys by user
- One-click key deactivation

### Command-Line User and API Key Management

You can also manage users and keys via command-line:

Before you can use the proxy, you need to create a user and API key:

**1. Create a user:**
```bash
curl -X POST "http://localhost:8000/admin/users?username=alice&email=alice@example.com"
```

Response:
```json
{
  "success": true,
  "user_id": 1,
  "username": "alice",
  "message": "User alice created successfully"
}
```

**2. Create an API key for the user:**
```bash
curl -X POST "http://localhost:8000/admin/api-keys?user_id=1&name=my-key"
```

Response:
```json
{
  "success": true,
  "api_key": "sk-abc123...",
  "user_id": 1,
  "name": "my-key",
  "warning": "Save this API key! It will not be shown again."
}
```

**Important:** Save the API key immediately - it cannot be retrieved later!

**3. List all users:**
```bash
curl http://localhost:8000/admin/users
```

**4. List API keys:**
```bash
curl http://localhost:8000/admin/api-keys
# Or for a specific user:
curl "http://localhost:8000/admin/api-keys?user_id=1"
```

**5. Deactivate an API key:**
```bash
curl -X DELETE http://localhost:8000/admin/api-keys/1
```

### Using with Claude Code

Configure Claude Code to use the proxy as its API endpoint:

1. Set the API endpoint to: `http://localhost:8000`
2. Set the API key to your generated key (from the API key creation step above)
3. The proxy accepts Claude API format at `/v1/messages`

### Using with OpenAI-Compatible Clients

You can use the proxy with any OpenAI-compatible client or library:

**Python (OpenAI SDK):**
```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="sk-abc123..."  # Your API key from the key creation step
)

response = client.chat.completions.create(
    model="glm-4.6",
    messages=[
        {"role": "user", "content": "Hello!"}
    ]
)
```

**JavaScript/TypeScript (OpenAI SDK):**
```javascript
import OpenAI from 'openai';

const client = new OpenAI({
    baseURL: 'http://localhost:8000/v1',
    apiKey: 'sk-abc123...'  // Your API key
});

const response = await client.chat.completions.create({
    model: 'glm-4.6',
    messages: [{ role: 'user', content: 'Hello!' }]
});
```

**cURL:**
```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-abc123..." \
  -d '{
    "model": "glm-4.6",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### API Endpoints

#### `GET /`
Returns server information and available endpoints.

```bash
curl http://localhost:8000/
```

#### `GET /health`
Health check endpoint.

```bash
curl http://localhost:8000/health
```

#### `POST /v1/messages`
Claude API endpoint (with translation to OpenAI format).

This endpoint accepts Claude API format requests, translates them to OpenAI format,
and returns responses in Claude format.

**Requires authentication via Bearer token.**

```bash
curl -X POST http://localhost:8000/v1/messages \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-abc123..." \
  -d '{
    "model": "claude-3-5-sonnet-20241022",
    "max_tokens": 1024,
    "messages": [
      {
        "role": "user",
        "content": "Hello!"
      }
    ]
  }'
```

#### `POST /v1/chat/completions`
OpenAI API endpoint (pass-through mode).

This endpoint accepts OpenAI API format requests and passes them directly to the
backend without translation, returning OpenAI format responses.

**Requires authentication via Bearer token.**

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-abc123..." \
  -d '{
    "model": "glm-4.6",
    "max_tokens": 1024,
    "messages": [
      {
        "role": "user",
        "content": "Hello!"
      }
    ]
  }'
```

### Usage Tracking Endpoints

#### `GET /usage/me`
Get usage statistics for the authenticated user.

```bash
curl http://localhost:8000/usage/me \
  -H "Authorization: Bearer sk-abc123..."

# With date range:
curl "http://localhost:8000/usage/me?start_date=2024-01-01T00:00:00&end_date=2024-12-31T23:59:59" \
  -H "Authorization: Bearer sk-abc123..."
```

Response:
```json
{
  "user_id": 1,
  "username": "alice",
  "total_requests": 42,
  "total_input_tokens": 1500,
  "total_output_tokens": 3000,
  "total_tokens": 4500,
  "usage_by_endpoint": [
    {
      "endpoint": "/v1/chat/completions",
      "model": "glm-4.6",
      "total_requests": 25,
      "total_input_tokens": 900,
      "total_output_tokens": 1800,
      "total_tokens": 2700
    }
  ]
}
```

### Per-API-Key Model Settings

Each API key can have its own backend model setting, allowing different users to use different models through the same proxy server.

#### `GET /settings/model`
Query the model setting for your API key.

```bash
curl http://localhost:8000/settings/model \
  -H "Authorization: Bearer sk-abc123..."
```

Response:
```json
{
  "api_key_id": 1,
  "api_key_name": "Production Key",
  "model_name": "gpt-4",
  "using_default": false
}
```

#### `PUT /settings/model`
Set or unset the model for your API key.

```bash
# Set a specific model
curl -X PUT http://localhost:8000/settings/model \
  -H "Authorization: Bearer sk-abc123..." \
  -H "Content-Type: application/json" \
  -d '{"model_name": "gpt-4"}'

# Unset to use default global model
curl -X PUT http://localhost:8000/settings/model \
  -H "Authorization: Bearer sk-abc123..." \
  -H "Content-Type: application/json" \
  -d '{"model_name": null}'
```

Response:
```json
{
  "success": true,
  "api_key_id": 1,
  "model_name": "gpt-4",
  "message": "Model set to gpt-4"
}
```

#### Client Script: `set_model.py`

A convenient client script is provided to manage model settings from the command line:

```bash
# Install python-dotenv if not already installed
pip install python-dotenv

# Check current model setting
python set_model.py --check

# Set model to gpt-4
python set_model.py gpt-4

# Set model to claude-3-5-sonnet
python set_model.py claude-3-5-sonnet-20241022

# Unset model (use default)
python set_model.py --unset

# Use different .env file
python set_model.py --env-file .env.production gpt-4

# Use different base URL
python set_model.py --url https://different-url.com gpt-4
```

The script reads your API key from a `.env` file:
```bash
# .env file
OPENAI_API_KEY=sk-abc123...
```

### Admin Endpoints

#### `POST /admin/users`
Create a new user (shown above in User Management section).

#### `POST /admin/api-keys`
Create an API key for a user (shown above in User Management section).

#### `GET /admin/users`
List all users.

#### `GET /admin/api-keys`
List all API keys or keys for a specific user.

#### `DELETE /admin/api-keys/{api_key_id}`
Deactivate an API key.

#### `GET /usage/api-key/{api_key_id}`
Get usage statistics for a specific API key.

## Testing

### Unit Tests

Run the comprehensive test suite:

```bash
source venv/bin/activate
pytest tests/ -v
```

### End-to-End Tests

Test the proxy with real API calls:

```bash
# Make sure the proxy server is running first
python proxy_server.py

# In another terminal:
source venv/bin/activate
python test_e2e.py
```

### Manual Testing

You can also use the test script to verify the OpenAI endpoint works:

```bash
source venv/bin/activate
python test_api.py
```

#### Quick Chat Testing: `test_chat.py`

A convenient test client for sending single messages:

```bash
# Simple message (Claude format)
python test_chat.py "Hello, how are you?"

# OpenAI format
python test_chat.py "Hello!" --format openai

# Custom model
python test_chat.py "Explain quantum computing" --model gpt-4

# More tokens for longer responses
python test_chat.py "Write a short story" --max-tokens 2000

# Streaming response
python test_chat.py "Count to 10" --stream

# Test against local server
python test_chat.py "Test message" --url http://localhost:8000
```

**Features:**
- Reads API key from `.env` file automatically
- Supports both Claude and OpenAI API formats
- Streaming and non-streaming modes
- Shows token usage and response time
- Pretty formatted output

## API Translation Details

### Request Translation (Claude → OpenAI)

The proxy translates Claude API requests to OpenAI format:

- **System messages**: Extracted from `system` field and added as first message with `role: "system"`
- **Content blocks**: Claude's content blocks (e.g., `[{"type": "text", "text": "..."}]`) are converted to simple strings
- **Parameters**: `max_tokens`, `temperature`, `top_p`, `stop_sequences` are mapped appropriately

### Response Translation (OpenAI → Claude)

OpenAI responses are translated back to Claude format:

- **Message structure**: Wrapped in Claude's content block format
- **Stop reasons**: Mapped from OpenAI's `finish_reason` to Claude's `stop_reason`
  - `stop` → `end_turn`
  - `length` → `max_tokens`
  - `content_filter` → `content_filtered`
- **Usage tokens**: Mapped from `prompt_tokens`/`completion_tokens` to `input_tokens`/`output_tokens`
- **Reasoning content**: Special handling for models with `reasoning_content` (like glm-4.6)

## Project Structure

```
infiniproxy/
├── config.py              # Configuration management
├── translator.py          # Request/response translation logic
├── openai_client.py       # OpenAI API client
├── user_manager.py        # User and API key management
├── proxy_server.py        # Main proxy server (FastAPI)
├── proxy_users.db         # SQLite database (not in git)
├── requirements.txt       # Python dependencies
├── .env                   # Configuration (not in git)
├── .gitignore            # Git ignore patterns
├── DESIGN.md             # Architecture and design document
├── README.md             # This file
├── test_api.py           # API verification script
├── test_e2e.py           # End-to-end tests
├── static/               # Admin UI assets
│   ├── admin.html        # Admin interface HTML
│   └── admin.js          # Admin interface JavaScript
└── tests/                # Unit tests
    ├── __init__.py
    ├── test_translator.py
    └── test_proxy_server.py
```

## Limitations

- **Streaming**: Currently not fully implemented. Streaming requests fall back to non-streaming responses.
- **Images**: Image content blocks are not yet supported in translation.

## Troubleshooting

### Connection Refused
- Make sure the proxy server is running
- Check that the port (default 8000) is not in use by another application
- Verify firewall settings allow connections to localhost

### API Key Errors
- Verify your `OPENAI_API_KEY` in the `.env` file is correct
- Check that the OpenAI-compatible endpoint is accessible

### Translation Errors
- Enable debug logging by setting `DEBUG=true` in `.env`
- Check the server logs for detailed error messages
- Verify the request format matches Claude API specifications

### Tests Failing
- Ensure all dependencies are installed: `pip install -r requirements.txt`
- For end-to-end tests, make sure the proxy server is running
- Check that the OpenAI backend endpoint is accessible

## Development

### Running in Development Mode

```bash
# Enable debug logging
export DEBUG=true

# Run with auto-reload
uvicorn proxy_server:app --reload --host localhost --port 8000
```

### Adding New Features

1. Update `translator.py` for translation logic changes
2. Update `openai_client.py` for client changes
3. Add tests in `tests/` directory
4. Update documentation

## License

This project is provided as-is for educational and development purposes.

## Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Add tests for new functionality
4. Ensure all tests pass
5. Submit a pull request

## Support

For issues or questions:

1. Check the troubleshooting section
2. Review the DESIGN.md for architecture details
3. Open an issue on the repository
