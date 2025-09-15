# FaultMaven User Guide

Welcome to FaultMaven! This guide will help you get the open-source version of the FaultMaven server up and running on your local machine.

## 1. System Requirements

* **Python:** Version 3.10 or higher
* **Pip:** Python's package installer
* **Docker & Docker Compose:** For running backing services easily

## 2. Installation

Follow these steps to set up the FaultMaven application.

1.  **Clone the Repository**
    Open your terminal and clone the official FaultMaven repository:
    ```bash
    git clone [https://github.com/your-org/faultmaven.git](https://github.com/your-org/faultmaven.git)
    cd faultmaven
    ```

2.  **Set Up a Virtual Environment**
    It's highly recommended to use a Python virtual environment to manage dependencies.
    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows, use `venv\Scripts\activate`
    ```

3.  **Install Dependencies**
    Install all the required Python packages using the `requirements.txt` file.
    ```bash
    pip install -r requirements.txt
    ```

4.  **Configure Environment Variables**
    You will need to configure API keys for the LLM provider you intend to use. Copy the example environment file and edit it with your keys.
    ```bash
    cp config/settings.py.example config/settings.py
    # Now, open config/settings.py and add your API keys
    ```

## 3. Running the Server

The easiest way to run the server and its dependencies is with Docker Compose.

* **Start the Application:**
    This command will build the necessary Docker images and start the FaultMaven FastAPI server, which will be accessible at `http://localhost:8000`.
    ```bash
    docker-compose up --build
    ```

## 4. Quickstart: How to Use FaultMaven

You can interact with the FaultMaven server using any API client (like `curl`, Postman, or a custom script). The server has two main endpoints: `/data` and `/query`.

### Step 1: Submit Data

First, let's create a new session or resume an existing one. FaultMaven now supports **multiple concurrent sessions per user** and **session resumption** across browser restarts using client-based session management.

```bash
curl -X POST -F "file=@/path/to/your/logfile.log" http://localhost:8000/data
```
**Create New Session:**
```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -d '{"timeout_minutes": 60, "session_type": "troubleshooting"}' \
  http://localhost:8000/api/v1/sessions
```

**Resume Session with Client ID:**
```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -d '{"client_id": "my-device-123", "timeout_minutes": 60, "session_type": "troubleshooting"}' \
  http://localhost:8000/api/v1/sessions
```

Response: You'll get back a JSON object with `session_id`, and if resuming, `session_resumed: true`. The response indicates whether a new session was created or an existing session was resumed.

### Step 2: Ask a Question

Now, upload data to your session and then ask questions. With multi-session support, you can maintain multiple troubleshooting contexts simultaneously.

**Upload Data to Session:**
```bash
curl -X POST \
  -F "file=@/path/to/your/logfile.log" \
  -F "session_id=<your-session-id>" \
  http://localhost:8000/api/v1/data/upload
```

**Ask Questions:** Use the session-id you received to ask questions about the data.

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -H "X-Session-ID: <your-session-id-from-step-1>" \
  -d '{"query": "What are the most common errors in the data I just uploaded?"}' \
  http://localhost:8000/query
```
Response: FaultMaven will provide a detailed answer based on its analysis of the log file in the context of your session.

**Multi-Session Benefits:**
- **Session Resumption**: Use the same `client_id` to resume sessions across browser restarts
- **Multiple Contexts**: Maintain separate troubleshooting sessions for different issues
- **Device Continuity**: Access your sessions from multiple devices
- **Collaborative Tabs**: Multiple browser tabs can share the same session using the same `client_id`